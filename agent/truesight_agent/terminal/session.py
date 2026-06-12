"""Session « Terminal interactif » côté agent : shell PTY relayé en WebSocket.

Architecture (volontairement simple, sans asyncio — calquée sur remote/session) :
  - un **PTY** (ConPTY) est lancé via ``pywinpty`` (``PtyProcess.spawn``) ;
  - un **thread de lecture** lit en continu la sortie du PTY et la pousse au
    viewer en messages texte JSON ``{"t":"output","data":...}`` ;
  - la **boucle principale** reçoit les messages texte JSON (viewer → agent) :
    ``input`` (frappes clavier), ``resize`` (taille du PTY), ``ping`` (latence).

Transport : ``websocket-client`` (``websocket.create_connection``), exactement
comme le bureau à distance — la lib gère le framing WebSocket, le TLS (wss) et
le ping/pong. On se connecte au **même** relais, chemin ``/ws/remote/agent``
(le serveur fournit déjà le bon ws_url dans ``remote_session.ws_url``).

PROTOCOLE (texte JSON dans les DEUX sens) :
  viewer → agent :
    {"t":"input","data":"<frappes clavier brutes>"}  → écrites dans le PTY
    {"t":"resize","cols":N,"rows":M}                   → redimensionne le PTY
    {"t":"ping"}                                        → répond {"t":"pong"}
  agent → viewer :
    {"t":"output","data":"<texte UTF-8>"}              → sortie du PTY (par chunks)
    {"t":"exit","code":N}                               → le shell s'est terminé
    {"t":"pong"}

Robustesse : la session ne crashe jamais l'agent. Toute exception est
journalisée (logger ``truesight.terminal``) ; la déconnexion d'un côté provoque
l'arrêt propre de l'autre (fermeture du PTY + de la WebSocket).
"""

from __future__ import annotations

import json
import logging
import threading
import time

_logger = logging.getLogger("truesight.terminal")

# Import tolérant de websocket-client (la session échoue proprement si absent).
try:
    import websocket  # type: ignore  (paquet « websocket-client »)
    _WS_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    websocket = None  # type: ignore
    _WS_AVAILABLE = False
    _logger.warning("websocket-client indisponible (%s) : terminal inopérant.", _exc)

# Import tolérant de pywinpty (ConPTY). Dépendance Windows uniquement ; le module
# doit pouvoir s'importer hors Windows (et échouer proprement à l'usage).
try:
    from winpty import PtyProcess  # type: ignore  (paquet « pywinpty »)
    _PTY_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    PtyProcess = None  # type: ignore
    _PTY_AVAILABLE = False
    _logger.warning("pywinpty indisponible (%s) : terminal inopérant.", _exc)

# Délai d'établissement de la connexion wss (secondes).
_CONNECT_TIMEOUT = 15
# Durée de vie maximale d'une session sans démantèlement explicite (garde-fou).
_MAX_SESSION_SECONDS = 60 * 60  # 1 h
# Taille de lecture du PTY (octets/caractères par chunk).
_READ_SIZE = 4096
# Taille initiale du PTY (colonnes x lignes).
_DEFAULT_COLS = 120
_DEFAULT_ROWS = 30


class TerminalSession:
    """Pilote une session de terminal interactif jusqu'à sa fermeture."""

    def __init__(self, token: str, ws_url: str, shell: str = "powershell",
                 verify_tls: bool = True) -> None:
        self.token = token
        self.ws_url = ws_url
        self.shell = (shell or "powershell").lower()
        self.verify_tls = verify_tls
        self._stop = threading.Event()
        self._ws = None
        self._proc = None  # winpty.PtyProcess
        self._read_thread: threading.Thread | None = None
        # Sérialise les envois (le thread lecture et les pong ne se chevauchent pas).
        self._send_lock = threading.Lock()
        self._started_at = 0.0
        self._exit_sent = False

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
            _logger.info("Durée maximale de session terminal atteinte, fermeture.")
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
                enable_multithread=True,  # lecture PTY (thread) + réception (boucle).
            )
            # Au-delà du handshake, réception bloquante mais réveillable.
            self._ws.settimeout(1.0)
            _logger.info("Terminal connecté au relais (agent) : %s", _redact(self.ws_url))
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.error("Connexion du terminal au relais impossible : %s", exc)
            return False

    # -- Lancement du PTY -----------------------------------------------------
    def _spawn_pty(self) -> bool:
        """Lance le shell dans un PTY ConPTY (pywinpty). Renvoie True si OK."""
        if not _PTY_AVAILABLE:
            _logger.error("PTY indisponible : pywinpty absent.")
            return False
        cmd = "powershell.exe" if self.shell == "powershell" else "cmd.exe"
        try:
            self._proc = PtyProcess.spawn(
                cmd, dimensions=(_DEFAULT_ROWS, _DEFAULT_COLS)
            )
            _logger.info("PTY démarré : %s (%dx%d).", cmd, _DEFAULT_COLS, _DEFAULT_ROWS)
            return True
        except TypeError:
            # Certaines versions de pywinpty n'acceptent pas `dimensions=` :
            # on retombe sur l'appel minimal puis un redimensionnement explicite.
            try:
                self._proc = PtyProcess.spawn(cmd)
                try:
                    self._proc.setwinsize(_DEFAULT_ROWS, _DEFAULT_COLS)
                except Exception:  # noqa: BLE001 - non bloquant.
                    pass
                _logger.info("PTY démarré (repli) : %s.", cmd)
                return True
            except Exception as exc:  # noqa: BLE001
                _logger.error("Lancement du PTY impossible : %s", exc)
                return False
        except Exception as exc:  # noqa: BLE001
            _logger.error("Lancement du PTY impossible : %s", exc)
            return False

    # -- Envoi (agent → viewer) ----------------------------------------------
    def _send_text(self, obj: dict) -> bool:
        """Envoie un message texte JSON ; False si la WebSocket est tombée."""
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(obj))
            return True
        except Exception as exc:  # noqa: BLE001 - viewer parti / relais fermé.
            _logger.debug("Envoi d'un message terminal impossible : %s", exc)
            return False

    def _send_exit(self) -> None:
        """Notifie le viewer que le shell s'est terminé (une seule fois)."""
        if self._exit_sent:
            return
        self._exit_sent = True
        code = None
        proc = self._proc
        if proc is not None:
            try:
                code = proc.exitstatus
            except Exception:  # noqa: BLE001 - attribut absent selon version.
                code = None
        self._send_text({"t": "exit", "code": code})

    # -- Boucle de lecture du PTY (thread) ------------------------------------
    def _read_loop(self) -> None:
        """Lit en continu la sortie du PTY et la pousse au viewer (texte JSON)."""
        _logger.info("Lecture du PTY démarrée.")
        proc = self._proc
        try:
            while not self._should_stop() and proc is not None and proc.isalive():
                try:
                    data = proc.read(_READ_SIZE)
                except EOFError:
                    # Le shell a fermé sa sortie : fin normale.
                    break
                except Exception as exc:  # noqa: BLE001
                    # `.read()` lève quand le PTY se ferme ; on termine proprement.
                    name = exc.__class__.__name__
                    if "EOF" not in name and "Closed" not in name:
                        _logger.debug("Lecture du PTY interrompue : %s", exc)
                    break
                if not data:
                    # pywinpty renvoie '' à la fermeture ; petite pause pour ne
                    # pas tourner à vide si le shell est momentanément silencieux.
                    if proc is not None and not proc.isalive():
                        break
                    time.sleep(0.01)
                    continue
                # pywinpty .read() renvoie un str (déjà décodé UTF-8).
                if isinstance(data, (bytes, bytearray)):
                    data = data.decode("utf-8", errors="replace")
                if not self._send_text({"t": "output", "data": data}):
                    break
        except Exception as exc:  # noqa: BLE001 - jamais fatal.
            _logger.error("Boucle de lecture du PTY interrompue : %s", exc)
        finally:
            _logger.info("Lecture du PTY arrêtée.")
            # La sortie du shell termine toute la session.
            self._send_exit()
            self._stop.set()

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
                _logger.info("Réception terminal terminée (session fermée) : %s", exc)
                break
            if message is None or message == "":
                continue
            if isinstance(message, (bytes, bytearray)):
                # Le viewer n'envoie que du texte JSON ; binaire inattendu ignoré.
                continue
            self._handle_text(message)
        self._stop.set()

    def _handle_text(self, raw: str) -> None:
        """Décode et applique un message JSON viewer → agent."""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            _logger.debug("Message terminal non-JSON ignoré.")
            return
        if not isinstance(data, dict):
            return

        msg_type = data.get("t")
        if msg_type == "ping":
            self._send_text({"t": "pong"})
            return
        if msg_type == "input":
            self._write_input(data.get("data", ""))
            return
        if msg_type == "resize":
            self._resize(data.get("cols"), data.get("rows"))
            return
        # Type inconnu : on ignore (forward-compat).

    def _write_input(self, payload) -> None:
        """Écrit les frappes clavier dans le stdin du PTY."""
        proc = self._proc
        if proc is None or not payload:
            return
        if not isinstance(payload, str):
            try:
                payload = str(payload)
            except Exception:  # noqa: BLE001
                return
        try:
            proc.write(payload)
        except Exception as exc:  # noqa: BLE001 - PTY fermé / shell terminé.
            _logger.debug("Écriture dans le PTY impossible : %s", exc)
            self._stop.set()

    def _resize(self, cols, rows) -> None:
        """Redimensionne le PTY (pywinpty attend (rows, cols))."""
        proc = self._proc
        if proc is None:
            return
        try:
            cols_i = int(cols)
            rows_i = int(rows)
        except (TypeError, ValueError):
            return
        if cols_i <= 0 or rows_i <= 0:
            return
        try:
            proc.setwinsize(rows_i, cols_i)
        except Exception as exc:  # noqa: BLE001 - non bloquant.
            _logger.debug("Redimensionnement du PTY impossible : %s", exc)

    # -- Exécution ------------------------------------------------------------
    def run(self) -> None:
        """Établit la session et la fait tourner jusqu'à fermeture."""
        if not _PTY_AVAILABLE:
            _logger.error("Terminal indisponible (pywinpty manquant) : session annulée.")
            return
        if not self._connect():
            return
        if not self._spawn_pty():
            # Pas de PTY : on ferme la WebSocket proprement.
            self._teardown()
            return

        self._started_at = time.monotonic()

        # Thread de lecture du PTY ; la boucle principale reçoit les entrées.
        self._read_thread = threading.Thread(
            target=self._read_loop, name="truesight-terminal-read", daemon=True
        )
        self._read_thread.start()

        try:
            self._recv_loop()
        finally:
            self._teardown()

    def _teardown(self) -> None:
        """Ferme proprement : termine le PTY, ferme la WebSocket, joint le thread."""
        self._stop.set()
        # 1. Terminer le PTY (réveille aussi le thread de lecture).
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                if hasattr(proc, "close"):
                    proc.close()
            except Exception:  # noqa: BLE001
                pass
        # 2. Fermer la WebSocket.
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        # 3. Joindre le thread de lecture.
        if self._read_thread is not None:
            try:
                self._read_thread.join(timeout=5)
            except RuntimeError:
                pass
        _logger.info("Session de terminal terminée.")


def run(token: str, ws_url: str, shell: str = "powershell", verify_tls: bool = True) -> int:
    """Point d'entrée de la session terminal (utilisé en thread inline / helper).

    Bloque jusqu'à la fin de la session. Renvoie 0 (succès), 1 (échec
    d'initialisation). Ne lève jamais.
    """
    if not token or not ws_url:
        _logger.error("Session terminal impossible : token ou ws_url manquant.")
        return 1
    try:
        session = TerminalSession(token, ws_url, shell=shell, verify_tls=verify_tls)
        session.run()
        return 0
    except Exception as exc:  # noqa: BLE001 - filet ultime.
        _logger.error("Session de terminal en échec : %s", exc)
        return 1


def _redact(ws_url: str) -> str:
    """Masque le token dans l'URL pour les logs (le token reste secret)."""
    if "token=" not in ws_url:
        return ws_url
    head, _sep, _rest = ws_url.partition("token=")
    return head + "token=***"
