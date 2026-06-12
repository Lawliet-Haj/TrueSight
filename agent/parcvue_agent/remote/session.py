"""Session « Bureau à distance » côté agent : client WebSocket synchrone.

Architecture (volontairement simple, sans asyncio côté agent) :
  - un **thread d'envoi** itère le générateur de trames de ``capture`` et les
    pousse en binaire sur la WebSocket (agent → viewer) ;
  - la **boucle principale** reçoit les messages texte JSON (viewer → agent) et
    les applique : entrées souris/clavier via ``inject``, contrôles
    set_quality / request_keyframe / set_monitor sur le ``ScreenCapturer``.

Transport : ``websocket-client`` (``websocket.create_connection``). On reste en
mode bloquant : la lib gère le framing WebSocket, le TLS (wss) et le ping/pong.

Robustesse : la session ne crashe jamais l'agent. Toute exception est
journalisée ; la déconnexion d'un côté provoque l'arrêt propre de l'autre (le
relais ferme de toute façon la session côté serveur).

Cette classe est utilisée :
  - directement en mode console (agent en session utilisateur), et
  - par le helper lancé dans la session interactive (``remote-helper``) quand
    l'agent tourne en service SYSTEM.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import capture as capture_mod
from . import inject as inject_mod

_logger = logging.getLogger("parcvue.remote.session")

# Import tolérant de websocket-client (la session échoue proprement si absent).
try:
    import websocket  # type: ignore  (paquet « websocket-client »)
    _WS_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    websocket = None  # type: ignore
    _WS_AVAILABLE = False
    _logger.warning("websocket-client indisponible (%s) : bureau à distance inopérant.", _exc)

# Délai d'établissement de la connexion wss (secondes).
_CONNECT_TIMEOUT = 15
# Durée de vie maximale d'une session sans démantèlement explicite (garde-fou).
_MAX_SESSION_SECONDS = 60 * 60  # 1 h


class RemoteSession:
    """Pilote une session de bureau à distance jusqu'à sa fermeture."""

    def __init__(self, token: str, ws_url: str, verify_tls: bool = True) -> None:
        self.token = token
        self.ws_url = ws_url
        self.verify_tls = verify_tls
        self._stop = threading.Event()
        self._ws = None
        self._send_thread: threading.Thread | None = None
        self._capturer = capture_mod.ScreenCapturer()
        self._injector = inject_mod.InputInjector(self._capturer.current_monitor_geometry())
        # Sérialise les envois (le thread capture et d'éventuels acks ne se chevauchent pas).
        self._send_lock = threading.Lock()
        self._started_at = 0.0

    # -- Cycle de vie ---------------------------------------------------------
    def stop(self) -> None:
        """Demande l'arrêt propre de la session."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _should_stop(self) -> bool:
        if self._stop.is_set():
            return True
        if self._started_at and (time.monotonic() - self._started_at) > _MAX_SESSION_SECONDS:
            _logger.info("Durée maximale de session atteinte, fermeture.")
            return True
        return False

    # -- Connexion ------------------------------------------------------------
    def _connect(self) -> bool:
        """Établit la WebSocket vers le relais (chemin /ws/remote/agent)."""
        if not _WS_AVAILABLE:
            _logger.error("Connexion impossible : websocket-client absent.")
            return False
        try:
            sslopt = None
            if self.ws_url.lower().startswith("wss://") and not self.verify_tls:
                # Mode dev : on ne vérifie pas le certificat (aligné sur verify_tls).
                import ssl
                sslopt = {"cert_reqs": ssl.CERT_NONE}
            self._ws = websocket.create_connection(
                self.ws_url,
                timeout=_CONNECT_TIMEOUT,
                sslopt=sslopt,
                enable_multithread=True,  # envoi (thread) + réception (boucle) en parallèle.
            )
            # Au-delà du handshake, on veut une réception bloquante mais réveillable.
            self._ws.settimeout(1.0)
            _logger.info("Connecté au relais (agent) : %s", _redact(self.ws_url))
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.error("Connexion au relais impossible : %s", exc)
            return False

    # -- Boucle d'envoi des trames (thread) -----------------------------------
    def _send_loop(self) -> None:
        """Capture en continu et envoie les trames binaires (agent → viewer)."""
        _logger.info("Flux de capture démarré.")
        try:
            for frame in self._capturer.frames(self._should_stop):
                if self._should_stop():
                    break
                if not self._send_binary(frame):
                    break
        except Exception as exc:  # noqa: BLE001
            _logger.error("Boucle d'envoi interrompue : %s", exc)
        finally:
            _logger.info("Flux de capture arrêté.")
            # Si la capture s'arrête, on termine toute la session.
            self._stop.set()

    def _send_binary(self, data: bytes) -> bool:
        """Envoie une trame binaire ; renvoie False si la WebSocket est tombée."""
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._send_lock:
                ws.send_binary(data)
            return True
        except Exception as exc:  # noqa: BLE001 - viewer parti / relais fermé.
            _logger.info("Envoi d'une trame impossible (session probablement fermée) : %s", exc)
            return False

    def _send_text(self, obj: dict) -> bool:
        """Envoie un message texte JSON (agent → viewer : confort/latence/écrans)."""
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(obj))
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Envoi d'un message texte impossible : %s", exc)
            return False

    def _send_metadata(self) -> None:
        """Transmet au viewer les infos de confort : liste des écrans + utilisateur connecté."""
        try:
            self._send_text({"t": "monitors", "list": capture_mod.list_monitors()})
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Envoi de la liste des moniteurs impossible : %s", exc)
        try:
            self._send_text({"t": "user", "name": _current_user_label()})
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Envoi de l'utilisateur impossible : %s", exc)

    # -- Boucle de réception des entrées (principal) --------------------------
    def _recv_loop(self) -> None:
        """Reçoit les messages texte JSON (viewer → agent) et les applique."""
        while not self._should_stop():
            ws = self._ws
            if ws is None:
                break
            try:
                message = ws.recv()
            except Exception as exc:  # noqa: BLE001
                # Timeout de lecture (settimeout) : on reboucle pour vérifier l'arrêt.
                name = exc.__class__.__name__
                if "timeout" in name.lower():
                    continue
                _logger.info("Réception terminée (session fermée) : %s", exc)
                break
            if message is None or message == "":
                continue
            # Le viewer n'envoie que du texte JSON (entrées + contrôles).
            if isinstance(message, (bytes, bytearray)):
                # Inattendu dans le sens viewer → agent ; on ignore.
                continue
            self._handle_text(message)
        self._stop.set()

    def _handle_text(self, raw: str) -> None:
        """Décode et applique un message JSON viewer → agent."""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            _logger.debug("Message viewer non-JSON ignoré.")
            return
        if not isinstance(data, dict):
            return

        msg_type = data.get("t")
        # Mesure de latence : on renvoie un pong (texte agent → viewer).
        if msg_type == "ping":
            self._send_text({"t": "pong", "ts": data.get("ts")})
            return
        # Messages de contrôle : pilotent la capture.
        if msg_type == "set_quality":
            self._capturer.set_quality(data.get("q", 70))
            return
        if msg_type == "request_keyframe":
            self._capturer.request_keyframe()
            return
        if msg_type == "set_monitor":
            self._capturer.set_monitor(data.get("i", 0))
            # L'injection doit suivre le moniteur courant (échelle des coordonnées).
            self._injector.set_monitor(self._capturer.current_monitor_geometry())
            return
        # Sinon : entrée souris/clavier effective.
        inject_mod.apply_input_message(self._injector, data)

    # -- Exécution ------------------------------------------------------------
    def run(self) -> None:
        """Établit la session et la fait tourner jusqu'à fermeture."""
        # Conscience DPI AVANT toute capture/métrique : aligne mss (pixels
        # physiques) et les coordonnées d'injection sur les écrans à 125/150 %.
        inject_mod.set_dpi_awareness()
        if not capture_mod.is_available():
            _logger.error("Capture indisponible (mss/encodeur manquant) : session annulée.")
            return
        if not self._connect():
            return

        self._started_at = time.monotonic()
        # Keyframe d'amorçage : le viewer doit recevoir une image dès l'appairage.
        self._capturer.request_keyframe()
        # Infos de confort au viewer (écrans + utilisateur connecté).
        self._send_metadata()

        # Thread d'envoi (trames) ; la boucle principale reçoit les entrées.
        self._send_thread = threading.Thread(
            target=self._send_loop, name="parcvue-remote-send", daemon=True
        )
        self._send_thread.start()

        try:
            self._recv_loop()
        finally:
            self._teardown()

    def _teardown(self) -> None:
        """Ferme proprement : stoppe la capture, ferme la WebSocket, joint le thread."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._send_thread is not None:
            try:
                self._send_thread.join(timeout=5)
            except RuntimeError:
                pass
        _logger.info("Session de bureau à distance terminée.")


def run(token: str, ws_url: str, verify_tls: bool = True) -> int:
    """Point d'entrée de la session (utilisé par le helper et le mode console).

    Bloque jusqu'à la fin de la session. Renvoie 0 (succès), 1 (échec
    d'initialisation). Ne lève jamais.
    """
    if not token or not ws_url:
        _logger.error("Session impossible : token ou ws_url manquant.")
        return 1
    try:
        session = RemoteSession(token, ws_url, verify_tls=verify_tls)
        session.run()
        return 0
    except Exception as exc:  # noqa: BLE001 - filet ultime.
        _logger.error("Session de bureau à distance en échec : %s", exc)
        return 1


def _current_user_label() -> str:
    """Libellé de l'utilisateur connecté (DOMAINE\\user) — le helper tourne dans sa session."""
    domain = os.environ.get("USERDOMAIN", "") or os.environ.get("COMPUTERNAME", "")
    user = os.environ.get("USERNAME", "")
    if domain and user:
        return f"{domain}\\{user}"
    return user or "—"


def _redact(ws_url: str) -> str:
    """Masque le token dans l'URL pour les logs (le token reste secret)."""
    if "token=" not in ws_url:
        return ws_url
    head, _sep, _rest = ws_url.partition("token=")
    return head + "token=***"
