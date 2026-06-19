"""Écran de confidentialité : voile noir LOCAL invisible à la capture.

Quand l'admin prend la main, on peut masquer l'écran physique aux personnes
présentes devant le poste (contexte médical) SANS masquer la vue de l'admin.

Mécanisme : une fenêtre noire plein écran, topmost, marquée
``WDA_EXCLUDEFROMCAPTURE`` (Windows 10 2004+ / build 19041). Effet :
  - localement : la fenêtre s'affiche → écran noir pour les personnes physiques ;
  - à la capture (DXGI / mss) : la fenêtre est EXCLUE → l'admin voit le bureau réel.

Si ``WDA_EXCLUDEFROMCAPTURE`` n'est pas supporté (Windows trop ancien), on
ABANDONNE (sinon l'admin verrait du noir lui aussi). Tout est tolérant : un échec
laisse la confidentialité désactivée et la session continue.

La fenêtre vit sur un thread dédié (création + boucle de messages Win32 sur le
même thread, comme l'exige l'API). ``start()`` attend que la fenêtre soit prête.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

_logger = logging.getLogger("truesight.remote.privacy")

try:
    _user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    _gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    _WIN = True
except (AttributeError, OSError):  # pragma: no cover - hors Windows.
    _user32 = _gdi32 = _kernel32 = None  # type: ignore
    _WIN = False

# -- Constantes Win32 --------------------------------------------------------
_WS_POPUP = 0x80000000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_SW_SHOW = 5
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_BLACK_BRUSH = 4
_WDA_EXCLUDEFROMCAPTURE = 0x00000011
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

# LRESULT (WndProc) : pointeur-large 64 bits → c_ssize_t (PAS c_int).
_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# Signatures explicites : sans cela, ctypes suppose un retour ``c_int`` et
# TRONQUE les handles 64 bits → CreateWindowEx renverrait un HWND invalide.
if _WIN:
    try:
        _user32.DefWindowProcW.restype = ctypes.c_ssize_t
        _user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        _user32.CreateWindowExW.restype = wintypes.HWND
        _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
        _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        _gdi32.GetStockObject.restype = wintypes.HGDIOBJ
        _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        _user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        _user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
        ]
        _user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
    except Exception as _exc:  # noqa: BLE001
        _logger.warning("Signatures user32 (confidentialité) indisponibles : %s", _exc)
        _WIN = False

_CLASS_NAME = "TrueSightPrivacyWnd"
_class_registered = False
_wndproc_ref = None  # garde la WNDPROC vivante (sinon GC → crash au dispatch).


def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == _WM_DESTROY:
        _user32.PostQuitMessage(0)
        return 0
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _ensure_class(hinst) -> str:
    """Enregistre (une fois) la classe fenêtre au fond noir. Renvoie son nom."""
    global _class_registered, _wndproc_ref
    if _class_registered:
        return _CLASS_NAME
    _wndproc_ref = _WNDPROC(_wnd_proc)
    wc = _WNDCLASS()
    wc.style = 0
    wc.lpfnWndProc = _wndproc_ref
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinst
    wc.hIcon = None
    wc.hCursor = None
    wc.hbrBackground = ctypes.cast(_gdi32.GetStockObject(_BLACK_BRUSH), wintypes.HBRUSH)
    wc.lpszMenuName = None
    wc.lpszClassName = _CLASS_NAME
    _user32.RegisterClassW(ctypes.byref(wc))  # ATOM 0 si déjà enregistrée → toléré.
    _class_registered = True
    return _CLASS_NAME


class PrivacyScreen:
    """Voile noir plein écran exclu de la capture (toggle on/off)."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._ready = threading.Event()
        self._ok = False

    def start(self) -> bool:
        """Affiche le voile. Renvoie True si actif (et exclu de la capture)."""
        if not _WIN:
            return False
        if self._thread and self._thread.is_alive():
            return self._ok
        self._ready.clear()
        self._ok = False
        self._thread = threading.Thread(target=self._run, name="truesight-privacy", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self._ok

    def stop(self) -> None:
        """Retire le voile (ferme la fenêtre ; le thread se termine)."""
        hwnd = self._hwnd
        if hwnd:
            try:
                _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    @property
    def active(self) -> bool:
        return bool(self._ok and self._thread and self._thread.is_alive())

    def _run(self) -> None:
        try:
            self._create_and_loop()
        except Exception as exc:  # noqa: BLE001 - jamais fatal.
            _logger.error("Écran de confidentialité en échec : %s", exc)
        finally:
            self._hwnd = None
            self._ok = False
            self._ready.set()

    def _create_and_loop(self) -> None:
        # En mode non-assisté, suivre le bureau d'entrée (best effort).
        try:
            from . import desktop as desk
            desk.attach_thread_to_input_desktop()
        except Exception:  # noqa: BLE001
            pass

        hinst = _kernel32.GetModuleHandleW(None)
        cls = _ensure_class(hinst)
        left = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        top = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        width = _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN) or 1920
        height = _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN) or 1080

        hwnd = _user32.CreateWindowExW(
            _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE,
            cls, "TrueSight", _WS_POPUP,
            int(left), int(top), int(width), int(height),
            None, None, hinst, None,
        )
        if not hwnd:
            _logger.error("CreateWindowEx (confidentialité) a échoué.")
            self._ready.set()
            return

        # Exclure de la capture : SANS cela l'admin verrait du noir lui aussi.
        ok = False
        try:
            ok = bool(_user32.SetWindowDisplayAffinity(hwnd, _WDA_EXCLUDEFROMCAPTURE))
        except Exception as exc:  # noqa: BLE001
            _logger.info("SetWindowDisplayAffinity indisponible : %s", exc)
        if not ok:
            _logger.warning(
                "WDA_EXCLUDEFROMCAPTURE non supporté (Windows < 10 2004) : "
                "confidentialité désactivée."
            )
            try:
                _user32.DestroyWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
            self._ready.set()
            return

        self._hwnd = hwnd
        self._ok = True
        _user32.ShowWindow(hwnd, _SW_SHOW)
        self._ready.set()

        # Boucle de messages (sur CE thread : exigé par l'API fenêtre).
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
