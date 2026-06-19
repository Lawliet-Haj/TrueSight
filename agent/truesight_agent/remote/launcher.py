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
# NB : les modules de session (remote/terminal) sont importés PARESSEUSEMENT dans
# les fonctions, pour ne pas tirer mss/Pillow quand seul le terminal est demandé.

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


def _helper_command(token: str, ws_url: str, kind: str = "remote",
                    shell: str = "powershell", unattended: bool = False) -> list[str]:
    """Construit la ligne de commande du helper à lancer dans la session active.

    - Exécutable figé (.exe) : ``truesight-agent.exe remote-helper --token .. --ws-url .. --kind ..``
    - Mode développement (python -m) : ``python -m truesight_agent remote-helper ...``

    ``kind`` vaut 'remote' (capture écran) ou 'terminal' (shell PTY) ; pour le
    terminal on transmet aussi ``--shell``. ``unattended`` ajoute ``--unattended``
    (le helper suit alors le bureau d'entrée actif : Default ↔ Winlogon).
    """
    args = ["remote-helper", "--token", token, "--ws-url", ws_url, "--kind", kind]
    if kind == "terminal":
        args += ["--shell", shell]
    if unattended:
        args += ["--unattended"]
    if cfg.is_frozen():
        return [sys.executable, *args]
    # Dev : relancer l'interpréteur sur le paquet.
    return [sys.executable, "-m", "truesight_agent", *args]


def _unattended_enabled() -> bool:
    """True si la prise de main non-assistée est autorisée (config, défaut True)."""
    try:
        return bool(cfg.load_config().remote_unattended)
    except Exception:  # noqa: BLE001 - config illisible → on autorise par défaut.
        return True


def _launch_in_active_session_as_system(token: str, ws_url: str,
                                        kind: str = "remote", shell: str = "powershell") -> bool:
    """Lance le helper en SYSTEM dans la session console, sur le bureau d'entrée actif.

    Sert la prise de main NON-ASSISTÉE : aucun utilisateur connecté (écran de
    connexion) ou bureau sécurisé. On part du jeton SYSTEM du service courant, on
    le duplique en jeton primaire, on le recible sur la session console active
    (SetTokenInformation TokenSessionId — exige SeTcbPrivilege, acquis par SYSTEM),
    puis on lance le helper avec ``lpDesktop`` = bureau d'entrée courant.

    Renvoie True si le process a été créé. Tolérant : journalise et renvoie False.
    """
    if not _PYWIN32_AVAILABLE:
        _logger.error("pywin32 absent : impossible de lancer le helper non-assisté.")
        return False

    dup_token = None
    environment = None
    try:
        console_session_id = win32ts.WTSGetActiveConsoleSessionId()
        if console_session_id in (0xFFFFFFFF, None):
            _logger.warning("Non-assisté : aucune session console active.")
            return False

        # Jeton SYSTEM du service courant → dupliqué en jeton primaire reciblable.
        access = (win32security.TOKEN_DUPLICATE | win32security.TOKEN_QUERY
                  | win32security.TOKEN_ASSIGN_PRIMARY | win32security.TOKEN_ADJUST_DEFAULT
                  | win32security.TOKEN_ADJUST_SESSIONID)
        proc_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), access)
        try:
            dup_token = win32security.DuplicateTokenEx(
                proc_token, win32security.SecurityImpersonation,
                win32con.MAXIMUM_ALLOWED, win32security.TokenPrimary, None,
            )
        finally:
            try:
                win32api.CloseHandle(proc_token)
            except Exception:  # noqa: BLE001
                pass

        # Recible le jeton sur la session console interactive (écran de connexion inclus).
        win32security.SetTokenInformation(
            dup_token, win32security.TokenSessionId, int(console_session_id)
        )

        try:
            environment = win32profile.CreateEnvironmentBlock(dup_token, False)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("CreateEnvironmentBlock indisponible (%s), env par défaut.", exc)
            environment = None

        # Bureau d'entrée courant : 'Default' (utilisateur) ou 'Winlogon' (sécurisé).
        from . import desktop as desk_mod
        desk_name = desk_mod.current_input_desktop_name() or "Default"

        cmdline = _helper_command(token, ws_url, kind, shell, unattended=True)
        cmdline_str = subprocess.list2cmdline(cmdline)

        startup = win32process.STARTUPINFO()
        startup.lpDesktop = "winsta0\\" + desk_name
        creation_flags = win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_NO_WINDOW

        win32process.CreateProcessAsUser(
            dup_token, None, cmdline_str, None, None, False,
            creation_flags, environment, None, startup,
        )
        _logger.info("Helper NON-ASSISTÉ (SYSTEM) lancé en session %s, bureau %s.",
                     console_session_id, desk_name)
        return True
    except Exception as exc:  # noqa: BLE001 - jamais bloquant.
        _logger.error("Lancement non-assisté (SYSTEM) impossible : %s", exc)
        return False
    finally:
        try:
            if environment is not None:
                win32profile.DestroyEnvironmentBlock(environment)
        except Exception:  # noqa: BLE001
            pass
        try:
            if dup_token is not None:
                win32api.CloseHandle(dup_token)
        except Exception:  # noqa: BLE001
            pass


def _launch_in_active_session(token: str, ws_url: str, kind: str = "remote", shell: str = "powershell") -> bool:
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

        cmdline = _helper_command(token, ws_url, kind, shell)
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
        _logger.info("Helper (%s) lancé dans la session console %s.",
                     kind, console_session_id)
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


def _run_session_inline(token: str, ws_url: str, verify_tls: bool,
                        kind: str = "remote", shell: str = "powershell") -> None:
    """Exécute la session dans un thread du process courant (agent en session user).

    Dispatche selon ``kind`` : 'remote' (capture/injection) ou 'terminal' (shell PTY).
    Les imports sont paresseux pour ne charger que ce qui est nécessaire.
    """
    def _target() -> None:
        try:
            if kind == "terminal":
                from ..terminal import session as terminal_session
                terminal_session.run(token, ws_url, shell=shell, verify_tls=verify_tls)
            else:
                from . import session as remote_session
                remote_session.run(token, ws_url, verify_tls=verify_tls)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Session inline interrompue : %s", exc)

    thread = threading.Thread(target=_target, name="truesight-remote-session", daemon=True)
    thread.start()


def start_session(token: str, ws_url: str, verify_tls: bool = True,
                  kind: str = "remote", shell: str = "powershell") -> bool:
    """Démarre une session distante (bureau à distance OU terminal).

    - En session 0 (service SYSTEM) : lance un helper dans la session console
      active (CreateProcessAsUser). C'est INDISPENSABLE non seulement pour la
      capture écran mais AUSSI pour le terminal : ConPTY/pywinpty n'est pas fiable
      dans la session 0 headless d'un service → le shell s'y lance dans la session
      interactive de l'utilisateur.
    - Sinon (mode console / agent en session utilisateur) : exécute directement
      dans un thread du process courant.

    Renvoie True si le démarrage a été initié. Ne bloque jamais, ne lève jamais.
    """
    if not token or not ws_url:
        _logger.error("Démarrage de session impossible : token ou ws_url manquant.")
        return False

    try:
        if is_session_zero():
            # 1) Compagnon en session utilisateur (fiable pour terminal ET bureau).
            from .. import companion
            payload = {"token": token, "ws_url": ws_url, "kind": kind,
                       "shell": shell, "verify_tls": verify_tls}
            if companion.send_session_request(payload):
                _logger.info("Service en session 0 : session %s confiée au compagnon utilisateur.", kind)
                return True
            # 2) Repli : helper CreateProcessAsUser dans la session de l'utilisateur
            #    connecté (bureau à distance OK ; terminal peu fiable).
            _logger.info("Compagnon indisponible : repli sur le helper utilisateur (kind=%s).", kind)
            if _launch_in_active_session(token, ws_url, kind, shell):
                return True
            # 3) Repli NON-ASSISTÉ : aucun utilisateur connecté (écran de connexion /
            #    verrouillage) → helper SYSTEM attaché au bureau d'entrée actif.
            #    Réservé au bureau à distance (le terminal non-assisté n'a pas de sens).
            if kind == "remote" and _unattended_enabled():
                _logger.info("Aucune session utilisateur : tentative de prise de main NON-ASSISTÉE.")
                return _launch_in_active_session_as_system(token, ws_url, kind, shell)
            _logger.warning("Session %s non démarrée (aucune session utilisateur, non-assisté indisponible).", kind)
            return False
        _logger.info("Session utilisateur : exécution directe de la session (kind=%s).", kind)
        _run_session_inline(token, ws_url, verify_tls, kind, shell)
        return True
    except Exception as exc:  # noqa: BLE001 - filet ultime.
        _logger.error("Démarrage de la session distante échoué : %s", exc)
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
