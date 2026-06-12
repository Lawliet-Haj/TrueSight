"""Injection souris/clavier via ``SendInput`` (Win32, ctypes).

Le viewer envoie des entrées normalisées (CONTRAT REMOTE) :
  - souris : coordonnées x/y dans 0..1 (relatives au moniteur capturé) ;
  - boutons gauche/droit/milieu, molette ;
  - clavier : ``vk`` (Virtual-Key code) et éventuellement ``unicode`` (caractère).

On traduit cela en structures ``INPUT`` Win32 envoyées par ``SendInput`` :
  - souris en **coordonnées absolues** (0..65535) à l'échelle de l'écran
    virtuel (MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_ABSOLUTE), de sorte qu'un
    point d'un moniteur secondaire soit correctement adressé ;
  - clavier par VK (KEYEVENTF_KEYUP au relâchement) ou par caractère Unicode
    (KEYEVENTF_UNICODE), ce qui évite toute dépendance à la disposition clavier.

Tolérant aux erreurs : une entrée invalide est journalisée et ignorée, sans
jamais lever vers la boucle de session.

NB session 0 : ``SendInput`` injecte dans la **session de bureau du process
appelant**. Lancé en SYSTEM/session 0, il n'atteint pas le bureau utilisateur.
C'est le helper lancé dans la session interactive (voir ``launcher.py``) qui
réalise l'injection effective.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

_logger = logging.getLogger("parcvue.remote.inject")

# ctypes.windll n'existe que sous Windows ; import tolérant pour la doc/CI.
try:
    _user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    _WIN_AVAILABLE = True
except (AttributeError, OSError):  # pragma: no cover - hors Windows.
    _user32 = None  # type: ignore
    _WIN_AVAILABLE = False

# -- Constantes Win32 --------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Drapeaux souris.
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

WHEEL_DELTA = 120  # un cran de molette standard.

# Drapeaux clavier.
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

# Indices SystemMetrics pour l'écran virtuel (multi-moniteurs).
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


# -- Structures Win32 (ctypes) ------------------------------------------------
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


def set_dpi_awareness() -> None:
    """Rend le process conscient du DPI (per-monitor) — À APPELER AU DÉMARRAGE.

    Sans cela, sur un écran à 125 %/150 %, Windows renvoie des métriques
    « logiques » (mises à l'échelle) alors que la capture mss est en pixels
    physiques : le curseur injecté tombe à côté du point cliqué dans le viewer.
    En se déclarant per-monitor DPI-aware, capture (mss) et coordonnées
    (GetSystemMetrics) sont cohérentes en pixels physiques.
    """
    if not _WIN_AVAILABLE:
        return
    try:
        # Windows 8.1+ : PROCESS_PER_MONITOR_DPI_AWARE = 2.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        # Repli Windows Vista+ : DPI-aware au niveau système.
        ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _send_input(inp: "_INPUT") -> bool:
    """Envoie une structure INPUT via SendInput ; renvoie True si acceptée."""
    if not _WIN_AVAILABLE or _user32 is None:
        return False
    try:
        sent = _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        return sent == 1
    except Exception as exc:  # noqa: BLE001 - jamais bloquant.
        _logger.debug("SendInput a échoué : %s", exc)
        return False


def _virtual_screen_rect() -> tuple[int, int, int, int]:
    """Renvoie (left, top, width, height) de l'écran virtuel (multi-moniteurs)."""
    if not _WIN_AVAILABLE or _user32 is None:
        return (0, 0, 1, 1)
    try:
        left = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        top = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or 1
        height = _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or 1
        return (int(left), int(top), int(width), int(height))
    except Exception as exc:  # noqa: BLE001
        _logger.debug("Lecture de l'écran virtuel impossible : %s", exc)
        return (0, 0, 1, 1)


class InputInjector:
    """Traduit les entrées normalisées du viewer en appels SendInput.

    ``monitor`` (optionnel) : géométrie ``{"left","top","width","height"}`` du
    moniteur capturé, pour convertir les coordonnées normalisées 0..1 en
    coordonnées écran absolues. Sans moniteur fourni, on retombe sur l'écran
    virtuel complet (cas mono-écran).
    """

    def __init__(self, monitor: dict | None = None) -> None:
        self._monitor = monitor

    def set_monitor(self, monitor: dict | None) -> None:
        """Met à jour le moniteur de référence (suite à un ``set_monitor`` viewer)."""
        self._monitor = monitor

    @property
    def available(self) -> bool:
        """True si l'injection Win32 est utilisable."""
        return _WIN_AVAILABLE

    # -- Souris ---------------------------------------------------------------
    def _to_absolute(self, x: float, y: float) -> tuple[int, int]:
        """Convertit (x, y) normalisés 0..1 en coordonnées absolues 0..65535.

        Le système absolu de SendInput couvre l'écran **virtuel** entier en
        0..65535. On positionne donc le point dans le moniteur cible puis on
        le ramène à l'échelle de l'écran virtuel.
        """
        nx = min(1.0, max(0.0, float(x)))
        ny = min(1.0, max(0.0, float(y)))

        vleft, vtop, vwidth, vheight = _virtual_screen_rect()

        if self._monitor:
            # Point en coordonnées écran (pixels) à l'intérieur du moniteur capturé.
            mon_left = int(self._monitor.get("left", vleft))
            mon_top = int(self._monitor.get("top", vtop))
            mon_w = int(self._monitor.get("width", vwidth)) or 1
            mon_h = int(self._monitor.get("height", vheight)) or 1
            px = mon_left + nx * mon_w
            py = mon_top + ny * mon_h
        else:
            px = vleft + nx * vwidth
            py = vtop + ny * vheight

        # Ramène (px, py) dans le repère virtuel 0..65535.
        abs_x = int(round((px - vleft) * 65535.0 / max(1, vwidth)))
        abs_y = int(round((py - vtop) * 65535.0 / max(1, vheight)))
        abs_x = min(65535, max(0, abs_x))
        abs_y = min(65535, max(0, abs_y))
        return abs_x, abs_y

    def _mouse_event(self, flags: int, x: float | None = None, y: float | None = None,
                     mouse_data: int = 0) -> None:
        """Construit et envoie un événement souris."""
        dx = dy = 0
        full_flags = flags
        if x is not None and y is not None:
            dx, dy = self._to_absolute(x, y)
            full_flags |= (MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE)
        mi = _MOUSEINPUT(
            dx=dx,
            dy=dy,
            # mouseData est signé pour la molette ; on le passe via DWORD (cast).
            mouseData=ctypes.c_uint32(mouse_data & 0xFFFFFFFF).value,
            dwFlags=full_flags,
            time=0,
            dwExtraInfo=None,
        )
        inp = _INPUT(type=INPUT_MOUSE, union=_INPUTUNION(mi=mi))
        _send_input(inp)

    def mouse_move(self, x: float, y: float) -> None:
        """Déplace le curseur à la position normalisée (x, y)."""
        self._mouse_event(0, x, y)

    def mouse_button(self, button: str, down: bool, x: float | None = None,
                     y: float | None = None) -> None:
        """Presse (``down=True``) ou relâche un bouton, éventuellement après déplacement."""
        flags = _BUTTON_FLAGS.get((button or "left").lower())
        if not flags:
            _logger.debug("Bouton souris inconnu ignoré : %s", button)
            return
        down_flag, up_flag = flags
        self._mouse_event(down_flag if down else up_flag, x, y)

    def wheel(self, dy: int) -> None:
        """Fait défiler la molette.

        Le viewer envoie ``dy`` selon la convention DOM (``deltaY`` positif =
        défilement vers le BAS). Or ``MOUSEEVENTF_WHEEL`` attend l'inverse
        (mouseData positif = vers le HAUT). On inverse donc le signe pour que le
        sens de défilement dans le navigateur corresponde au poste distant.
        """
        try:
            amount = -int(dy) * WHEEL_DELTA
        except (TypeError, ValueError):
            return
        self._mouse_event(MOUSEEVENTF_WHEEL, mouse_data=amount)

    # -- Clavier --------------------------------------------------------------
    def key(self, vk: int | None, down: bool, unicode_char: str | None = None) -> None:
        """Presse/relâche une touche.

        - Si ``unicode_char`` est fourni : injection Unicode (indépendante de la
          disposition clavier) via KEYEVENTF_UNICODE.
        - Sinon : injection par Virtual-Key code (``vk``).
        """
        if unicode_char:
            self._key_unicode(unicode_char, down)
            return
        if vk is None:
            return
        try:
            vk_code = int(vk) & 0xFFFF
        except (TypeError, ValueError):
            return
        flags = KEYEVENTF_KEYUP if not down else 0
        ki = _KEYBDINPUT(wVk=vk_code, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
        inp = _INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(ki=ki))
        _send_input(inp)

    def _key_unicode(self, char: str, down: bool) -> None:
        """Injecte un caractère Unicode (un ou plusieurs code units UTF-16)."""
        if not char:
            return
        # Un caractère hors BMP est composé de 2 code units UTF-16 ; on les
        # envoie séparément (chacun en KEYEVENTF_UNICODE).
        try:
            code_units = char.encode("utf-16-le")
        except Exception:  # noqa: BLE001
            return
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if not down else 0)
        for i in range(0, len(code_units), 2):
            scan = code_units[i] | (code_units[i + 1] << 8)
            ki = _KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
            inp = _INPUT(type=INPUT_KEYBOARD, union=_INPUTUNION(ki=ki))
            _send_input(inp)


def apply_input_message(injector: "InputInjector", message: dict) -> None:
    """Applique un message d'entrée JSON (CONTRAT REMOTE) via l'injecteur.

    Les types de contrôle (set_quality, request_keyframe, set_monitor) ne sont
    PAS traités ici : ils relèvent de la capture et sont gérés par la session.
    Cette fonction ne couvre que les entrées effectives (souris/clavier).
    Tolérante : un message malformé est ignoré.
    """
    if not isinstance(message, dict):
        return
    msg_type = message.get("t")
    try:
        if msg_type == "mouse_move":
            injector.mouse_move(message.get("x", 0.0), message.get("y", 0.0))
        elif msg_type == "mouse_down":
            injector.mouse_button(message.get("button", "left"), True,
                                   message.get("x"), message.get("y"))
        elif msg_type == "mouse_up":
            injector.mouse_button(message.get("button", "left"), False,
                                   message.get("x"), message.get("y"))
        elif msg_type == "wheel":
            injector.wheel(message.get("dy", 0))
        elif msg_type == "key_down":
            injector.key(message.get("vk"), True, message.get("unicode"))
        elif msg_type == "key_up":
            injector.key(message.get("vk"), False, message.get("unicode"))
        # else : type de contrôle ou inconnu → ignoré ici.
    except Exception as exc:  # noqa: BLE001 - jamais bloquant.
        _logger.debug("Application d'une entrée échouée (%s) : %s", msg_type, exc)
