"""Compagnon de session utilisateur (terminal interactif + bureau à distance).

POURQUOI ce module
------------------
Le service tourne en SYSTEM (session 0). Or :
- le **bureau à distance** doit capturer le bureau de l'utilisateur (session interactive) ;
- le **terminal interactif** (ConPTY/pywinpty) n'est PAS fiable lancé par un process
  créé en cross-session via ``CreateProcessAsUser`` (ConPTY produit son init mais le
  shell ne rend pas son invite) — alors qu'il marche parfaitement quand le process
  tourne NORMALEMENT dans la session de l'utilisateur.

Architecture retenue (un service + un compagnon) :
- le **service SYSTEM** reste l'unique agent enrôlé (supervision, commandes/scripts
  en SYSTEM) — un seul jeton, aucun conflit ;
- le **compagnon** tourne dans la session de l'utilisateur (tâche planifiée au logon)
  et exécute les sessions distantes EN INLINE (capture + ConPTY fiables) ;
- le service signale le compagnon via un **named pipe** local (pousse {token, ws_url,
  kind, shell, verify_tls}). Le compagnon ne s'enrôle pas et n'interroge pas le
  serveur : pas de second jeton, pas de va-et-vient 401.

Sécurité : le pipe est local à la machine ; un message n'est exploitable qu'avec un
jeton de session valide (usage unique, TTL court) délivré par le serveur.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import tempfile
import threading
import time

_logger = logging.getLogger("truesight.companion")

PIPE_NAME = r"\\.\pipe\TrueSightRemoteSession"
_PIPE_BUFFER = 64 * 1024

# Imports pywin32 tolérants (le module doit s'importer hors Windows).
try:
    import win32pipe  # type: ignore
    import win32file  # type: ignore
    import win32event  # type: ignore
    import win32api  # type: ignore
    import winerror  # type: ignore
    import pywintypes  # type: ignore
    _PYWIN32_AVAILABLE = True
except Exception:  # noqa: BLE001
    win32pipe = win32file = win32event = win32api = winerror = pywintypes = None  # type: ignore
    _PYWIN32_AVAILABLE = False


def _setup_companion_logging() -> str:
    """Journalise le compagnon dans un fichier accessible à l'utilisateur."""
    root = logging.getLogger("truesight")
    root.setLevel(logging.INFO)
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    log_dir = os.path.join(base, "TrueSight")
    log_path = os.path.join(log_dir, "companion.log")
    if not root.handlers:
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
            )
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
            root.addHandler(fh)
        except Exception:  # noqa: BLE001
            pass
    return log_path


# --------------------------------------------------------------------------
# Côté SERVICE : client qui pousse une demande de session au compagnon
# --------------------------------------------------------------------------
def send_session_request(payload: dict, retries: int = 8, wait_ms: int = 400) -> bool:
    """Pousse {token, ws_url, kind, shell, verify_tls} au compagnon via le pipe.

    Renvoie True si le message a été remis. Renvoie False si le compagnon n'écoute
    pas (aucun utilisateur connecté / compagnon absent) → l'appelant peut alors
    se rabattre sur l'ancien helper. Ne lève jamais.
    """
    if not _PYWIN32_AVAILABLE:
        return False
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(max(1, retries)):
        handle = None
        try:
            handle = win32file.CreateFile(
                PIPE_NAME,
                win32file.GENERIC_WRITE,
                0, None,
                win32file.OPEN_EXISTING,
                0, None,
            )
            win32file.WriteFile(handle, data)
            return True
        except pywintypes.error as exc:  # type: ignore[attr-defined]
            # 2 = introuvable (compagnon pas démarré), 231 = pipe occupé (entre 2
            # instances) : on patiente un peu et on réessaie.
            if exc.winerror in (getattr(winerror, "ERROR_FILE_NOT_FOUND", 2),
                                getattr(winerror, "ERROR_PIPE_BUSY", 231)):
                time.sleep(wait_ms / 1000.0)
                continue
            _logger.debug("Envoi au compagnon impossible : %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Envoi au compagnon impossible : %s", exc)
            return False
        finally:
            if handle is not None:
                try:
                    win32file.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass
    return False


# --------------------------------------------------------------------------
# Côté COMPAGNON : serveur de pipe + exécution des sessions inline
# --------------------------------------------------------------------------
def _dispatch(msg: dict) -> None:
    """Lance la session demandée EN INLINE dans la session utilisateur courante."""
    token = msg.get("token")
    ws_url = msg.get("ws_url")
    kind = (msg.get("kind") or "remote").lower()
    shell = (msg.get("shell") or "powershell").lower()
    verify_tls = bool(msg.get("verify_tls", True))
    if not token or not ws_url:
        _logger.warning("Demande de session compagnon incomplète, ignorée.")
        return

    def _target() -> None:
        try:
            if kind == "terminal":
                from .terminal import session as terminal_session
                terminal_session.run(token, ws_url, shell=shell, verify_tls=verify_tls)
            else:
                from .remote import session as remote_session
                remote_session.run(token, ws_url, verify_tls=verify_tls)
        except Exception as exc:  # noqa: BLE001 - jamais fatal pour le compagnon.
            _logger.error("Session compagnon (%s) interrompue : %s", kind, exc)

    _logger.info("Compagnon : démarrage d'une session %s en session utilisateur.", kind)
    threading.Thread(target=_target, name="truesight-companion-session", daemon=True).start()


def _serve_once() -> bool:
    """Crée une instance de pipe, attend un client, lit la demande, dispatche.

    Renvoie True si une demande a été traitée, False sur erreur (l'appelant
    temporise avant de recréer une instance).
    """
    pipe = None
    try:
        pipe = win32pipe.CreateNamedPipe(
            PIPE_NAME,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            _PIPE_BUFFER, _PIPE_BUFFER, 0, None,
        )
        # Bloque jusqu'à la connexion d'un client (le service).
        win32pipe.ConnectNamedPipe(pipe, None)
        hr, raw = win32file.ReadFile(pipe, _PIPE_BUFFER)
        try:
            msg = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, TypeError):
            _logger.debug("Message compagnon non-JSON ignoré.")
            return False
        if isinstance(msg, dict):
            _dispatch(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        _logger.debug("Boucle de pipe interrompue : %s", exc)
        return False
    finally:
        if pipe is not None:
            try:
                win32pipe.DisconnectNamedPipe(pipe)
            except Exception:  # noqa: BLE001
                pass
            try:
                win32file.CloseHandle(pipe)
            except Exception:  # noqa: BLE001
                pass


def _acquire_single_instance() -> bool:
    """Empêche deux compagnons dans la même session (mutex local de session)."""
    if not _PYWIN32_AVAILABLE:
        return True
    try:
        win32event.CreateMutex(None, False, "TrueSightCompanionSingleton")
        return win32api.GetLastError() != getattr(winerror, "ERROR_ALREADY_EXISTS", 183)
    except Exception:  # noqa: BLE001
        return True


def run_companion() -> int:
    """Point d'entrée du compagnon (``truesight-agent.exe companion``).

    Boucle indéfiniment : écoute le pipe, exécute chaque session demandée dans la
    session utilisateur. Ne quitte que sur erreur fatale. Ne lève jamais.
    """
    log_path = _setup_companion_logging()
    if not _PYWIN32_AVAILABLE:
        _logger.error("pywin32 absent : compagnon inopérant.")
        return 1
    if not _acquire_single_instance():
        _logger.info("Un compagnon TrueSight tourne déjà dans cette session — arrêt.")
        return 0

    _logger.info("Compagnon TrueSight démarré (pipe=%s, log=%s).", PIPE_NAME, log_path)
    while True:
        try:
            if not _serve_once():
                time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001 - jamais fatal.
            _logger.error("Erreur compagnon : %s", exc)
            time.sleep(1.0)
