"""Bureaux Windows (window-station / desktop) pour la prise de main NON-ASSISTÉE.

Pour capturer/injecter sur l'écran de connexion ou de verrouillage, le thread
doit être attaché au **bureau d'entrée actif** (``OpenInputDesktop`` →
``SetThreadDesktop``), qui vaut ``Default`` quand un utilisateur est connecté et
``Winlogon`` (bureau sécurisé) à l'écran de connexion / UAC. Seul un process
**SYSTEM** peut s'attacher au bureau sécurisé.

On passe par ``ctypes`` (user32) plutôt que pywin32 : ces fonctions ne sont pas
exposées de façon fiable par toutes les versions de pywin32, alors que l'ABI
ctypes est stable. Tolérant : tout échec renvoie None, ne lève jamais.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

_logger = logging.getLogger("truesight.remote.desktop")

try:
    _user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    _WIN = True
except (AttributeError, OSError):  # pragma: no cover - hors Windows.
    _user32 = None  # type: ignore
    _WIN = False

# Accès maximal autorisé sur le bureau (SYSTEM l'obtient en totalité).
_MAXIMUM_ALLOWED = 0x02000000
_UOI_NAME = 2

if _WIN:
    try:
        _user32.OpenInputDesktop.restype = wintypes.HANDLE
        _user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        _user32.SetThreadDesktop.restype = wintypes.BOOL
        _user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
        _user32.CloseDesktop.restype = wintypes.BOOL
        _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        _user32.GetUserObjectInformationW.restype = wintypes.BOOL
        _user32.GetUserObjectInformationW.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Signatures user32 (desktops) indisponibles : %s", exc)
        _WIN = False

# Handle du bureau attaché PAR THREAD : on le garde vivant tant qu'il est le
# bureau du thread (le fermer romprait l'association).
_attached = threading.local()


def _desktop_name(hdesk) -> str | None:
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD(0)
        ok = _user32.GetUserObjectInformationW(
            hdesk, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)
        )
        return buf.value if ok else None
    except Exception:  # noqa: BLE001
        return None


def current_input_desktop_name() -> str | None:
    """Nom du bureau d'entrée actif ('Default' / 'Winlogon'), ou None."""
    if not _WIN:
        return None
    hdesk = None
    try:
        hdesk = _user32.OpenInputDesktop(0, False, _MAXIMUM_ALLOWED)
        if not hdesk:
            return None
        return _desktop_name(hdesk)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("OpenInputDesktop (lecture) échoué : %s", exc)
        return None
    finally:
        if hdesk:
            try:
                _user32.CloseDesktop(hdesk)
            except Exception:  # noqa: BLE001
                pass


def attach_thread_to_input_desktop() -> str | None:
    """Attache le THREAD courant au bureau d'entrée actif. Renvoie son nom ou None.

    À n'appeler que depuis un thread SANS fenêtre ni hook (sinon SetThreadDesktop
    échoue). Conserve le handle vivant dans un stockage thread-local.
    """
    if not _WIN:
        return None
    try:
        hdesk = _user32.OpenInputDesktop(0, False, _MAXIMUM_ALLOWED)
        if not hdesk:
            return None
        if not _user32.SetThreadDesktop(hdesk):
            try:
                _user32.CloseDesktop(hdesk)
            except Exception:  # noqa: BLE001
                pass
            return None
        name = _desktop_name(hdesk)
        # Ferme l'ancien handle APRÈS avoir basculé sur le nouveau.
        old = getattr(_attached, "hdesk", None)
        _attached.hdesk = hdesk
        if old:
            try:
                _user32.CloseDesktop(old)
            except Exception:  # noqa: BLE001
                pass
        return name
    except Exception as exc:  # noqa: BLE001
        _logger.debug("attach_thread_to_input_desktop échoué : %s", exc)
        return None
