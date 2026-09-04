"""Bandeau « prise en main en cours » affiché sur le poste distant.

Exigence de CONFIDENTIALITÉ : la personne assise devant le poste doit voir,
sans ambiguïté et pendant toute la durée de la session, qu'un administrateur
regarde son écran — et qui.

Contraintes de conception :
  - **jamais bloquant** : la fenêtre est cliquable-à-travers (``WS_EX_TRANSPARENT``
    + couche alpha) et ne prend jamais le focus (``WS_EX_NOACTIVATE``), donc elle
    ne gêne pas le travail en cours ;
  - **pas refermable par mégarde** : aucune bordure, aucun bouton, absente de
    Alt+Tab (``WS_EX_TOOLWINDOW``) ; elle disparaît avec la session, point ;
  - **visible aussi par l'admin** : on ne l'exclut PAS de la capture (contrairement
    à ``privacy.py``), ce qui donne à l'admin la confirmation qu'elle s'affiche
    bien côté utilisateur ;
  - **reste au-dessus** : un ``SetWindowPos`` périodique réaffirme le rang
    topmost, qu'une autre fenêtre topmost (lecteur vidéo, plein écran…) aurait
    pu ravir.

Même structure que ``privacy.py`` : création de la fenêtre ET boucle de messages
sur un thread dédié (l'API Win32 l'exige), tolérance totale aux échecs — sans
bandeau, la session continue.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

_logger = logging.getLogger("truesight.remote.notice")

# Instances PRIVÉES (WinDLL) et non ``ctypes.windll.user32``, qui est un objet
# PARTAGÉ et mis en cache : y déclarer des ``argtypes`` les impose à tout le
# process. ``privacy.py`` déclare les siennes sur ce même objet — les deux
# modules s'écrasaient donc mutuellement, et l'un des deux échouait sur
# « expected LP__WNDCLASS instance » (structures de types différents).
# Avec une instance à nous, nos signatures n'engagent que nous.
try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)  # type: ignore[attr-defined]
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    _WIN = True
except (AttributeError, OSError):  # pragma: no cover - hors Windows.
    _user32 = _gdi32 = _kernel32 = None  # type: ignore
    _WIN = False

# -- Constantes Win32 --------------------------------------------------------
_WS_POPUP = 0x80000000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_SW_SHOWNOACTIVATE = 4
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_PAINT = 0x000F
_WM_TIMER = 0x0113
_LWA_ALPHA = 0x00000002
_SM_CXSCREEN = 0
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_HWND_TOPMOST = -1
_DT_CENTER = 0x00000001
_DT_VCENTER = 0x00000004
_DT_SINGLELINE = 0x00000020
_DT_END_ELLIPSIS = 0x00008000
_TRANSPARENT_BK = 1
_DEFAULT_CHARSET = 1
_FW_SEMIBOLD = 600
_TOPMOST_TIMER_ID = 1
_TOPMOST_TIMER_MS = 3000

# Ambre soutenu : lisible, non alarmiste. COLORREF = 0x00BBGGRR.
_BANNER_BGR = 0x00184EC8   # ~ #C84E18
_TEXT_BGR = 0x00FFFFFF     # blanc

_BANNER_WIDTH = 620
_BANNER_HEIGHT = 46
_BANNER_ALPHA = 235       # léger fondu : on voit qu'il est en surcouche

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


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


# Signatures explicites : sans elles, ctypes suppose un retour ``c_int`` et
# TRONQUE les handles 64 bits (même piège que dans privacy.py).
if _WIN:
    try:
        _user32.DefWindowProcW.restype = ctypes.c_ssize_t
        _user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
        _user32.CreateWindowExW.restype = wintypes.HWND
        # argtypes OBLIGATOIRES ici : sans elles, ctypes déduit les types des
        # valeurs et tente de faire tenir hInstance (handle 64 bits) — et le style
        # WS_POPUP = 0x80000000 — dans un ``c_int`` signé 32 bits :
        # « OverflowError: int too long to convert », fenêtre jamais créée.
        _user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASS)]
        _user32.BeginPaint.restype = wintypes.HDC
        _user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_PAINTSTRUCT)]
        _user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_PAINTSTRUCT)]
        _user32.FillRect.argtypes = [
            wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH
        ]
        _user32.DrawTextW.argtypes = [
            wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
            ctypes.POINTER(wintypes.RECT), wintypes.UINT,
        ]
        _user32.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD
        ]
        _user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        _user32.SetTimer.argtypes = [
            wintypes.HWND, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p
        ]
        _gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        _gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
        _gdi32.CreateFontW.restype = wintypes.HANDLE
        _gdi32.SelectObject.restype = wintypes.HGDIOBJ
        _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        _gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
        _gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        _kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        _user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
        ]
        _user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
        ]
    except Exception as _exc:  # noqa: BLE001
        _logger.warning("Signatures Win32 (bandeau) indisponibles : %s", _exc)
        _WIN = False

_CLASS_NAME = "TrueSightNoticeWnd"
_class_registered = False
_wndproc_ref = None   # garde la WNDPROC vivante (sinon GC → crash au dispatch)
_brush = None         # pinceau de fond, conservé pour la durée du process
_font = None          # police, idem

# Texte courant : lu par le WM_PAINT, qui tourne sur le thread de la fenêtre.
_text_lock = threading.Lock()
_current_text = ""


def _paint(hwnd) -> None:
    """Remplit le bandeau et y dessine le texte, centré."""
    ps = _PAINTSTRUCT()
    hdc = _user32.BeginPaint(hwnd, ctypes.byref(ps))
    if not hdc:
        return
    try:
        rect = wintypes.RECT()
        _user32.GetClientRect(hwnd, ctypes.byref(rect))
        _user32.FillRect(hdc, ctypes.byref(rect), _brush)
        _gdi32.SetBkMode(hdc, _TRANSPARENT_BK)
        _gdi32.SetTextColor(hdc, _TEXT_BGR)
        if _font:
            _gdi32.SelectObject(hdc, _font)
        with _text_lock:
            text = _current_text
        _user32.DrawTextW(
            hdc, text, -1, ctypes.byref(rect),
            _DT_CENTER | _DT_VCENTER | _DT_SINGLELINE | _DT_END_ELLIPSIS,
        )
    finally:
        _user32.EndPaint(hwnd, ctypes.byref(ps))


def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == _WM_PAINT:
        _paint(hwnd)
        return 0
    if msg == _WM_TIMER and wparam == _TOPMOST_TIMER_ID:
        # Réaffirme le rang topmost : une autre fenêtre topmost (plein écran,
        # lecteur vidéo) peut être passée devant depuis le dernier réveil.
        try:
            _user32.SetWindowPos(
                hwnd, wintypes.HWND(_HWND_TOPMOST), 0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
            )
        except Exception:  # noqa: BLE001
            pass
        return 0
    if msg == _WM_DESTROY:
        _user32.PostQuitMessage(0)
        return 0
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _ensure_class(hinst) -> str:
    """Enregistre (une seule fois) la classe fenêtre du bandeau."""
    global _class_registered, _wndproc_ref, _brush, _font
    if _class_registered:
        return _CLASS_NAME
    _wndproc_ref = _WNDPROC(_wnd_proc)
    _brush = _gdi32.CreateSolidBrush(_BANNER_BGR)
    _font = _gdi32.CreateFontW(
        -18, 0, 0, 0, _FW_SEMIBOLD, 0, 0, 0,
        _DEFAULT_CHARSET, 0, 0, 0, 0, "Segoe UI",
    )
    wc = _WNDCLASS()
    wc.style = 0
    wc.lpfnWndProc = _wndproc_ref
    wc.cbClsExtra = 0
    wc.cbWndExtra = 0
    wc.hInstance = hinst
    wc.hIcon = None
    wc.hCursor = None
    wc.hbrBackground = _brush
    wc.lpszMenuName = None
    wc.lpszClassName = _CLASS_NAME
    _user32.RegisterClassW(ctypes.byref(wc))  # ATOM 0 si déjà enregistrée → toléré
    _class_registered = True
    return _CLASS_NAME


def build_text(operator: str = "") -> str:
    """Libellé du bandeau. Nomme l'opérateur quand le serveur l'a transmis."""
    who = (operator or "").strip()
    if who:
        return f"Assistance à distance en cours — {who} voit votre écran"
    return "Assistance à distance en cours — votre écran est partagé"


class SessionNotice:
    """Bandeau persistant signalant la prise en main (toggle start/stop)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._ready = threading.Event()
        self._ok = False

    def start(self) -> bool:
        """Affiche le bandeau. Renvoie True s'il est bien à l'écran."""
        if not _WIN:
            return False
        if self._thread and self._thread.is_alive():
            return self._ok
        global _current_text
        with _text_lock:
            _current_text = self._text
        self._ready.clear()
        self._ok = False
        self._thread = threading.Thread(
            target=self._run, name="truesight-notice", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self._ok

    def stop(self) -> None:
        """Retire le bandeau (la fenêtre se ferme, le thread se termine)."""
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
        except Exception as exc:  # noqa: BLE001 - jamais fatal pour la session.
            _logger.error("Bandeau de prise en main en échec : %s", exc)
        finally:
            self._hwnd = None
            self._ok = False
            self._ready.set()

    def _create_and_loop(self) -> None:
        hinst = _kernel32.GetModuleHandleW(None)
        cls = _ensure_class(hinst)

        # Haut de l'écran PRINCIPAL, centré : l'endroit le plus visible sans
        # recouvrir la zone de travail habituelle.
        screen_w = _user32.GetSystemMetrics(_SM_CXSCREEN) or 1920
        left = max(0, (int(screen_w) - _BANNER_WIDTH) // 2)

        hwnd = _user32.CreateWindowExW(
            _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
            | _WS_EX_LAYERED | _WS_EX_TRANSPARENT,
            cls, "TrueSight", _WS_POPUP,
            left, 0, _BANNER_WIDTH, _BANNER_HEIGHT,
            None, None, hinst, None,
        )
        if not hwnd:
            _logger.error("CreateWindowEx (bandeau) a échoué.")
            self._ready.set()
            return

        # Couche alpha : rend aussi la fenêtre réellement cliquable-à-travers
        # (WS_EX_TRANSPARENT seul ne suffit pas sur une fenêtre non layered).
        try:
            _user32.SetLayeredWindowAttributes(hwnd, 0, _BANNER_ALPHA, _LWA_ALPHA)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Couche alpha indisponible (%s) : bandeau opaque.", exc)

        self._hwnd = hwnd
        self._ok = True
        _user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
        try:
            _user32.SetTimer(hwnd, _TOPMOST_TIMER_ID, _TOPMOST_TIMER_MS, None)
        except Exception:  # noqa: BLE001
            pass
        self._ready.set()

        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
