"""Démarrage d'une session de bureau à distance, avec gestion de la session 0.

LE POINT DÉLICAT — capture/injection depuis un service SYSTEM
--------------------------------------------------------------
Un service Windows tourne dans la **session 0**, isolée et *headless* : elle ne
« voit » pas le bureau interactif de l'utilisateur connecté. Si l'on appelle
``mss`` (capture) ou ``SendInput`` (injection) depuis la session 0, on capture
un bureau vide et l'injection n'atteint pas l'utilisateur.

La solution standard (et celle retenue ici) :

  1. Détecter qu'on est dans la session 0 (``ProcessIdToSessionId``).
  2. Trouver la session console interactive active
     (``WTSGetActiveConsoleSessionId``).
  3. Récupérer le **jeton de l'utilisateur** de cette session
     (``WTSQueryUserToken``), le **dupliquer** en jeton primaire
     (``DuplicateTokenEx``).
  4. Lancer un **helper** dans cette session avec ce jeton
     (``CreateProcessAsUser``), en relançant le même exécutable avec la
     sous-commande ``remote-helper --token <t> --ws-url <u>``.

Le helper, lui, tourne dans la session de l'utilisateur : sa capture et son
injection portent sur le vrai bureau. Il ouvre lui-même la WebSocket vers le
relais (pas de pipe intermédiaire).

Si l'on n'est PAS en session 0 (mode console / agent lancé par l'utilisateur),
on exécute directement la session dans le process courant — aucun helper requis.

⚠️ À VALIDER SUR MACHINE RÉELLE : le chemin ``CreateProcessAsUser`` exige les
privilèges SYSTEM (``SeTcbPrivilege``), un utilisateur réellement connecté à la
console, et l'exécutable figé (.exe). Il ne peut pas être testé hors d'un vrai
poste Windows avec session interactive. Voir les notes en fin de fichier.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

from .. import config as cfg
from . import session as session_mod

_logger = logging.getLogger("truesight.remote.launcher")

# Imports pywin32 tolérants (le module doit s'importer hors Windows).
try:
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32process  # type: ignore
    import win32security  # type: ignore
    import win32ts  # type: ignore
    import win32profile  # type: ignore
    _PYWIN32_AVAILABLE = True
except Exception:  # noqa: BLE001 - pywin32 absent / hors Windows.
    win32api = win32con = win32process = None  # type: ignore
    win32security = win32ts = win32profile = None  # type: ignore
    _PYWIN32_AVAILABLE = False


def is_session_zero() -> bool:
    """True si le process courant tourne dans la session 0 (service SYSTEM).

    On interroge ``ProcessIdToSessionId`` ; en cas d'échec (API absente), on
    suppose prudemment qu'on n'est PAS en session 0 (chemin console direct).
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        pid = ctypes.windll.kernel32.GetCurrentProcessId()  # type: ignore[attr-defined]
        sid = wintypes.DWORD()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid))  # type: ignore[attr-defined]
        if not ok:
            return False
        return sid.value == 0
    except Exception as exc:  # noqa: BLE001
        _logger.debug("Détection session 0 impossible (%s), supposé session utilisateur.", exc)
        return False


def _helper_command(token: str, ws_url: str) -> list[str]:
    """Construit la ligne de commande du helper à lancer dans la session active.

    - Exécutable figé (.exe) : ``truesight-agent.exe remote-helper --token .. --ws-url ..``
    - Mode développement (python -m) : ``python -m truesight_agent remote-helper ...``
    """
    args = ["remote-helper", "--token", token, "--ws-url", ws_url]
    if cfg.is_frozen():
        return [sys.executable, *args]
    # Dev : relancer l'interpréteur sur le paquet.
    return [sys.executable, "-m", "truesight_agent", *args]


def _launch_in_active_session(token: str, ws_url: str) -> bool:
    """Lance le helper dans la session console active via CreateProcessAsUser.

    Renvoie True si le process a été créé. Tolérant : journalise et renvoie
    False en cas d'échec (pas de session ouverte, privilèges insuffisants, …).
    """
    if not _PYWIN32_AVAILABLE:
        _logger.error("pywin32 absent : impossible de lancer le helper en session active.")
        return False

    user_token = None
    primary_token = None
    environment = None
    try:
        # 1. Session console interactive active.
        console_session_id = win32ts.WTSGetActiveConsoleSessionId()
        # 0xFFFFFFFF (-1) signifie « aucune session console attachée ».
        if console_session_id in (0xFFFFFFFF, None):
            _logger.warning("Aucune session console active : helper non lancé.")
            return False

        # 2. Jeton de l'utilisateur de cette session (exige SeTcbPrivilege / SYSTEM).
        user_token = win32ts.WTSQueryUserToken(console_session_id)

        # 3. Duplication en jeton primaire (requis par CreateProcessAsUser).
        primary_token = win32security.DuplicateTokenEx(
            user_token,
            win32security.SecurityImpersonation,
            win32con.MAXIMUM_ALLOWED,
            win32security.TokenPrimary,
            None,
        )

        # Bloc d'environnement de l'utilisateur (sinon variables manquantes).
        try:
            environment = win32profile.CreateEnvironmentBlock(primary_token, False)
        except Exception as exc:  # noqa: BLE001 - non bloquant.
            _logger.debug("CreateEnvironmentBlock indisponible (%s), env par défaut.", exc)
            environment = None

        cmdline = _helper_command(token, ws_url)
        # On reconstruit une ligne de commande citée correctement.
        cmdline_str = subprocess.list2cmdline(cmdline)

        startup = win32process.STARTUPINFO()
        # Bureau interactif de l'utilisateur (input desktop).
        startup.lpDesktop = "winsta0\\default"

        creation_flags = win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_NO_WINDOW

        # 4. Création du process dans la session de l'utilisateur.
        win32process.CreateProcessAsUser(
            primary_token,
            None,            # application name (déduite de la ligne de commande)
            cmdline_str,     # command line
            None,            # process attributes
            None,            # thread attributes
            False,           # inherit handles
            creation_flags,
            environment,
            None,            # current directory
            startup,
        )
        _logger.info("Helper de bureau à distance lancé dans la session console %s.",
                     console_session_id)
        return True
    except Exception as exc:  # noqa: BLE001 - jamais bloquant.
        _logger.error("Lancement du helper en session active impossible : %s", exc)
        return False
    finally:
        # Libération des handles/jetons.
        try:
            if environment is not None:
                win32profile.DestroyEnvironmentBlock(environment)
        except Exception:  # noqa: BLE001
            pass
        for token_handle in (primary_token, user_token):
            try:
                if token_handle is not None:
                    win32api.CloseHandle(token_handle)
            except Exception:  # noqa: BLE001
                pass


def _run_session_inline(token: str, ws_url: str, verify_tls: bool) -> None:
    """Exécute la session dans un thread du process courant (mode console)."""
    def _target() -> None:
        try:
            session_mod.run(token, ws_url, verify_tls=verify_tls)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Session inline interrompue : %s", exc)

    thread = threading.Thread(target=_target, name="truesight-remote-session", daemon=True)
    thread.start()


def start_session(token: str, ws_url: str, verify_tls: bool = True) -> bool:
    """Démarre une session de bureau à distance.

    - En session 0 (service SYSTEM) : lance un helper dans la session console
      active (CreateProcessAsUser). La capture/injection s'y déroule.
    - Sinon (mode console / session utilisateur) : exécute la session
      directement dans un thread du process courant.

    Renvoie True si le démarrage a été initié (le helper a été lancé ou la
    session inline a démarré), False sinon. Ne bloque jamais l'appelant et ne
    lève jamais.
    """
    if not token or not ws_url:
        _logger.error("Démarrage de session impossible : token ou ws_url manquant.")
        return False

    try:
        if is_session_zero():
            _logger.info("Service en session 0 : lancement d'un helper dans la session active.")
            return _launch_in_active_session(token, ws_url)
        _logger.info("Session utilisateur : exécution directe de la session de bureau.")
        _run_session_inline(token, ws_url, verify_tls)
        return True
    except Exception as exc:  # noqa: BLE001 - filet ultime.
        _logger.error("Démarrage de la session de bureau à distance échoué : %s", exc)
        return False


# ----------------------------------------------------------------------------
# NOTES DE VALIDATION (machine réelle requise)
# ----------------------------------------------------------------------------
# 1. CreateProcessAsUser exige que le service tourne en SYSTEM avec le
#    privilège SeTcbPrivilege (cas par défaut d'un service LocalSystem).
# 2. WTSQueryUserToken échoue s'il n'y a aucune session console interactive
#    (poste verrouillé sans utilisateur, RDP only, écran de login) → le helper
#    n'est pas lancé et la session reste « requested » côté serveur jusqu'à
#    expiration (60 s). Comportement attendu et sûr.
# 3. lpDesktop = "winsta0\\default" cible le bureau interactif ; sur l'écran de
#    verrouillage (Winlogon), c'est un autre bureau (capture vide) — limitation
#    connue, à documenter pour l'admin.
# 4. Ces chemins ne sont PAS testables hors d'un vrai poste Windows avec une
#    session utilisateur ouverte ; à valider en recette sur un poste pilote.
