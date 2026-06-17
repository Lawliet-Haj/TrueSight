"""Boucle principale de l'agent TrueSight.

Trois boucles tournent en threads démon :
  1. heartbeat  — envoie les métriques toutes les ``heartbeat_interval`` s et
                  applique la ``config`` renvoyée par le serveur (pilotage central) ;
  2. commandes  — interroge la file toutes les ``command_poll_interval`` s,
                  exécute les commandes reçues et renvoie leurs résultats ;
  3. inventaire — envoie l'inventaire matériel + logiciel toutes les
                  ``inventory_interval_hours`` (et une fois au démarrage).

Le thread principal (run) installe le logging, configure le client, garantit
l'enrôlement, lance les boucles et survit aux exceptions (l'agent ne crashe
jamais sur une erreur réseau ou de collecte).

``run()`` est utilisable aussi bien en mode console qu'en service Windows.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time

from . import __version__, collectors, commands as cmd_exec, config as cfg
from .client import ApiClient, AuthError
from .enroll import ensure_enrolled, EnrollmentError

# Délai mini entre deux démarrages de session distante (anti-rafale si le
# serveur re-signale la même demande sur plusieurs cycles de poll).
_REMOTE_START_COOLDOWN_SECONDS = 3.0
# Durée après laquelle on oublie le session_id actif (dédoublonnage). Doit être
# > au TTL d'appariement serveur (60 s) : au-delà, le serveur ne re-signale plus
# cette session (status ≠ requested), donc oublier l'id est sûr et permet une
# nouvelle session. Couvre les cas helper (processus séparé) et thread inline,
# pour lesquels le runner n'a pas de handle de fin de session.
_REMOTE_SESSION_DEDUP_TTL = 70.0

_logger = logging.getLogger("truesight.runner")

# Intervalle de réessai quand l'enrôlement ou la config échoue (secondes).
_BOOTSTRAP_RETRY_SECONDS = 30
# Délai mini entre deux tentatives de réenrôlement automatique (anti-boucle).
_REENROLL_COOLDOWN_SECONDS = 60
# Délai mini avant de réessayer la même version d'auto-update (anti-boucle si un
# téléchargement échoue ou si l'empreinte ne correspond pas).
_UPDATE_RETRY_COOLDOWN_SECONDS = 600


def setup_logging(console: bool = False) -> None:
    """Configure le logging : fichier tournant + console optionnelle.

    Le fichier de log est placé dans le répertoire de données (ProgramData en
    prod, dossier courant en dev).
    """
    root = logging.getLogger("truesight")
    root.setLevel(logging.INFO)

    # Évite les handlers en double si setup_logging est rappelé.
    if root.handlers:
        return

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handler fichier tournant (5 fichiers de 2 Mo).
    try:
        log_path = cfg.get_log_path()
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # noqa: BLE001 - on continue même sans fichier.
        # On ne peut pas écrire le fichier (droits) : on bascule console si possible.
        console = True
        root.warning("Journal fichier indisponible (%s), bascule console.", exc)

    # Handler console — UNIQUEMENT si une sortie standard existe. Sous pythonw.exe
    # (tâche planifiée / service, fenêtre cachée), sys.stderr est None : ajouter un
    # StreamHandler provoquerait des erreurs d'émission. Le fichier suffit alors.
    if console and getattr(sys, "stderr", None) is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)


class AgentRunner:
    """Orchestrateur des boucles de l'agent."""

    def __init__(self, agent_config: cfg.AgentConfig) -> None:
        self.config = agent_config
        self.client = ApiClient(agent_config.server_url, verify_tls=agent_config.verify_tls)
        # Événement d'arrêt global (déclenché par le service ou Ctrl+C).
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        # Garde-fou contre les réenrôlements en rafale.
        self._last_reenroll = 0.0
        self._reenroll_lock = threading.Lock()
        # Bureau à distance : une seule session active à la fois (déduplication
        # par session_id, car heartbeat ET poll commandes peuvent signaler la
        # même demande tant qu'elle est « requested »).
        self._remote_lock = threading.Lock()
        self._remote_active_session_id: str | None = None
        self._remote_last_started = 0.0
        # Auto-update : une seule bascule à la fois ; cooldown par version tentée.
        self._update_lock = threading.Lock()
        self._update_in_progress = False
        self._update_attempts: dict[str, float] = {}

    # -- Cycle de vie ---------------------------------------------------------
    def stop(self) -> None:
        """Demande l'arrêt propre de toutes les boucles."""
        _logger.info("Arrêt de l'agent demandé.")
        self._stop_event.set()

    def _sleep(self, seconds: float) -> bool:
        """Attend ``seconds`` ou jusqu'à l'arrêt. Retourne True si arrêt demandé."""
        return self._stop_event.wait(timeout=max(0.1, seconds))

    def _ensure_enrolled_blocking(self) -> bool:
        """Boucle jusqu'à l'enrôlement réussi ou l'arrêt. Retourne True si enrôlé."""
        while not self._stop_event.is_set():
            try:
                state = ensure_enrolled(self.client, self.config)
                if state.is_enrolled:
                    return True
            except EnrollmentError as exc:
                _logger.error("Enrôlement impossible : %s", exc)
            except Exception as exc:  # noqa: BLE001
                _logger.error("Erreur inattendue à l'enrôlement : %s", exc)
            _logger.info("Nouvelle tentative d'enrôlement dans %ss.", _BOOTSTRAP_RETRY_SECONDS)
            if self._sleep(_BOOTSTRAP_RETRY_SECONDS):
                return False
        return False

    def _handle_auth_error(self) -> None:
        """Réagit à un 401/403 : tente un réenrôlement (token probablement révoqué).

        Protégé par un cooldown pour éviter une boucle de réenrôlement agressive
        (cas d'un agent réellement révoqué : ``is_active=false``).
        """
        with self._reenroll_lock:
            now = time.monotonic()
            if now - self._last_reenroll < _REENROLL_COOLDOWN_SECONDS:
                return
            self._last_reenroll = now

        _logger.warning("Authentification refusée : tentative de réenrôlement.")
        try:
            ensure_enrolled(self.client, self.config, force=True)
        except EnrollmentError as exc:
            _logger.error("Réenrôlement échoué (agent peut-être révoqué) : %s", exc)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Erreur lors du réenrôlement : %s", exc)

    # -- Bureau à distance ----------------------------------------------------
    def _maybe_start_remote_session(self, remote_session) -> None:
        """Démarre une session distante si le serveur la demande.

        ``remote_session`` est le champ renvoyé par le heartbeat ET par le poll
        commandes (CONTRAT REMOTE) : ``{session_id, token, ws_url, kind, shell}``
        ou None. Le champ ``kind`` distingue :
          - ``'remote'`` (ou absent) : bureau à distance (capture écran +
            injection) — délégué au launcher (thread inline ou helper session 0) ;
          - ``'terminal'`` : shell interactif (PowerShell/cmd) via PTY, relayé
            en texte JSON — exécuté INLINE dans un thread du process agent (un
            shell n'a aucune contrainte de session 0, contrairement à la capture).

        Garanties (communes aux deux types) :
          - une seule session active à la fois (verrou + mémorisation du
            ``session_id``) ;
          - déduplication : si la même demande revient sur plusieurs cycles
            (heartbeat + commandes), on ne relance pas ;
          - cooldown anti-rafale ;
          - jamais bloquant, jamais crash (lancement délégué / thread démon).
        """
        if not remote_session or not isinstance(remote_session, dict):
            return

        session_id = remote_session.get("session_id")
        token = remote_session.get("token")
        ws_url = remote_session.get("ws_url")
        kind = (remote_session.get("kind") or "remote").lower()
        shell = (remote_session.get("shell") or "powershell").lower()
        if not session_id or not token:
            _logger.warning("Demande de session distante incomplète, ignorée : %s",
                            {k: ("***" if k == "token" else v) for k, v in remote_session.items()})
            return

        # Repli : si le serveur n'a pas fourni de ws_url, on le reconstruit.
        # Le chemin agent est le même pour les deux types (/ws/remote/agent).
        if not ws_url:
            ws_url = self.client.remote_agent_ws_url(token)

        with self._remote_lock:
            # Déjà la session active courante : rien à faire (signal répété).
            if self._remote_active_session_id == session_id:
                return
            now = time.monotonic()
            if now - self._remote_last_started < _REMOTE_START_COOLDOWN_SECONDS:
                return
            self._remote_last_started = now
            self._remote_active_session_id = session_id

        _logger.info("Session distante : démarrage de %s (kind=%s).", session_id, kind)
        try:
            # Import différé : ne charge le launcher (et ses dépendances) qu'à la
            # demande. Le launcher gère les DEUX types : en service (session 0) il
            # lance un helper dans la session utilisateur — requis pour la capture
            # ÉCRAN mais aussi pour le TERMINAL (ConPTY peu fiable en session 0).
            from .remote import launcher as remote_launcher
            started = remote_launcher.start_session(
                token, ws_url, verify_tls=self.config.verify_tls, kind=kind, shell=shell
            )
            if not started:
                _logger.warning("Session distante %s non démarrée.", session_id)
                # On libère le verrou de session pour autoriser une nouvelle tentative.
                with self._remote_lock:
                    if self._remote_active_session_id == session_id:
                        self._remote_active_session_id = None
            else:
                # Démarrée : on oublie l'id après le TTL pour autoriser une
                # future session (pas de handle de fin pour helper / thread).
                self._schedule_remote_dedup_reset(session_id)
        except Exception as exc:  # noqa: BLE001 - jamais bloquant pour l'agent.
            _logger.error("Démarrage de la session distante impossible : %s", exc)
            with self._remote_lock:
                if self._remote_active_session_id == session_id:
                    self._remote_active_session_id = None

    # -- Auto-update ----------------------------------------------------------
    def _maybe_apply_update(self, update_info) -> None:
        """Déclenche l'auto-update si le serveur annonce une version plus récente.

        Le téléchargement + la bascule tournent dans un thread démon (non bloquant
        pour le heartbeat). Une seule bascule à la fois ; on ne ré-essaie pas la
        même version avant ``_UPDATE_RETRY_COOLDOWN_SECONDS``. Si la bascule
        démarre, le service sera arrêté/redémarré par le script détaché.
        """
        if not update_info or not isinstance(update_info, dict):
            return
        from . import updater

        version = update_info.get("version")
        if not version or not updater.can_self_update() or not updater.is_newer(version):
            return

        with self._update_lock:
            if self._update_in_progress:
                return
            now = time.monotonic()
            if now - self._update_attempts.get(version, 0.0) < _UPDATE_RETRY_COOLDOWN_SECONDS:
                return
            self._update_attempts[version] = now
            self._update_in_progress = True

        def _worker() -> None:
            try:
                started = updater.apply_update(self.client, update_info)
            except Exception as exc:  # noqa: BLE001 - jamais bloquant.
                _logger.error("Auto-update échouée : %s", exc)
                started = False
            if not started:
                # Libère le verrou pour autoriser une nouvelle tentative (cooldown).
                with self._update_lock:
                    self._update_in_progress = False

        thread = threading.Thread(target=_worker, name="truesight-update", daemon=True)
        thread.start()

    def _schedule_remote_dedup_reset(self, session_id: str) -> None:
        """Oublie ``session_id`` du dédoublonnage après le TTL (thread minuteur)."""
        def _reset() -> None:
            with self._remote_lock:
                if self._remote_active_session_id == session_id:
                    self._remote_active_session_id = None
                    _logger.debug("Dédoublonnage de session %s réinitialisé.", session_id)

        timer = threading.Timer(_REMOTE_SESSION_DEDUP_TTL, _reset)
        timer.daemon = True
        timer.start()

    # -- Boucle heartbeat -----------------------------------------------------
    def _heartbeat_loop(self) -> None:
        _logger.info("Boucle heartbeat démarrée (intervalle %ss).", self.config.heartbeat_interval)
        # Métadonnées du poste, jointes au heartbeat pour que le serveur les
        # rafraîchisse sans ré-enrôlement (ex. correction Windows 10 → 11, MAJ agent).
        meta = {
            "os_version": cfg.get_os_version(),
            "agent_version": __version__,
            "hostname": cfg.get_hostname(),
        }
        while not self._stop_event.is_set():
            try:
                metrics = collectors.collect_metrics()
                result = self.client.heartbeat(metrics, meta=meta)
                if result.ok and isinstance(result.data, dict):
                    # Pilotage central des intervalles.
                    server_config = result.data.get("config")
                    if server_config:
                        self.config.apply_server_config(server_config)
                    # Signalisation bureau à distance (champ remote_session).
                    self._maybe_start_remote_session(result.data.get("remote_session"))
                    # Signalisation auto-update (champ agent_update).
                    self._maybe_apply_update(result.data.get("agent_update"))
                elif not result.ok:
                    _logger.warning("Heartbeat en échec : %s", result.error)
            except AuthError:
                self._handle_auth_error()
            except Exception as exc:  # noqa: BLE001 - la boucle survit à tout.
                _logger.error("Erreur dans la boucle heartbeat : %s", exc)
            # On relit l'intervalle à chaque tour (peut avoir changé via la config serveur).
            if self._sleep(self.config.heartbeat_interval):
                break
        _logger.info("Boucle heartbeat arrêtée.")

    # -- Boucle commandes -----------------------------------------------------
    def _commands_loop(self) -> None:
        _logger.info("Boucle commandes démarrée (poll %ss).", self.config.command_poll_interval)
        while not self._stop_event.is_set():
            try:
                result = self.client.get_commands()
                if result.ok and isinstance(result.data, dict):
                    # Signalisation bureau à distance (champ remote_session) :
                    # la réponse GET commands le porte aussi (CONTRAT REMOTE).
                    self._maybe_start_remote_session(result.data.get("remote_session"))
                    pending = result.data.get("commands") or []
                    for command in pending:
                        if self._stop_event.is_set():
                            break
                        self._run_one_command(command)
                elif not result.ok:
                    _logger.debug("Poll commandes en échec : %s", result.error)
            except AuthError:
                self._handle_auth_error()
            except Exception as exc:  # noqa: BLE001
                _logger.error("Erreur dans la boucle commandes : %s", exc)
            if self._sleep(self.config.command_poll_interval):
                break
        _logger.info("Boucle commandes arrêtée.")

    def _run_one_command(self, command: dict) -> None:
        """Exécute une commande et renvoie son résultat au serveur."""
        command_id = command.get("id")
        if not command_id:
            _logger.warning("Commande sans identifiant ignorée : %s", command)
            return

        shell = command.get("shell", "cmd")
        command_text = command.get("command_text", "")
        timeout_seconds = command.get("timeout_seconds")

        try:
            outcome = cmd_exec.execute(shell, command_text, timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - filet ultime.
            _logger.error("Exécution de la commande %s échouée : %s", command_id, exc)
            outcome = {
                "status": "error",
                "exit_code": None,
                "stdout": "",
                "stderr": f"Erreur interne de l'agent : {exc}",
                "duration_seconds": 0.0,
            }

        # Payload SPEC 2.5 + 'status' : le serveur lit data.get('status') pour
        # distinguer 'timeout' de 'error' (sinon un dépassement de délai serait
        # enregistré comme 'error'). Champ rétrocompatible et attendu côté serveur.
        result_payload = {
            "status": outcome.get("status"),
            "exit_code": outcome.get("exit_code"),
            "stdout": outcome.get("stdout", ""),
            "stderr": outcome.get("stderr", ""),
            "duration_seconds": outcome.get("duration_seconds", 0.0),
        }

        try:
            res = self.client.post_result(command_id, result_payload)
            if not res.ok:
                _logger.warning("Renvoi du résultat de %s en échec : %s", command_id, res.error)
        except AuthError:
            self._handle_auth_error()
        except Exception as exc:  # noqa: BLE001
            _logger.error("Renvoi du résultat de %s impossible : %s", command_id, exc)

    # -- Boucle inventaire ----------------------------------------------------
    def _inventory_loop(self) -> None:
        interval_seconds = max(60.0, self.config.inventory_interval_hours * 3600.0)
        _logger.info("Boucle inventaire démarrée (intervalle %.1f h).",
                    self.config.inventory_interval_hours)
        # Une première collecte au démarrage.
        first = True
        while not self._stop_event.is_set():
            if not first:
                if self._sleep(interval_seconds):
                    break
            first = False
            try:
                self._send_inventory_once()
            except AuthError:
                self._handle_auth_error()
            except Exception as exc:  # noqa: BLE001
                _logger.error("Erreur dans la boucle inventaire : %s", exc)
        _logger.info("Boucle inventaire arrêtée.")

    def _send_inventory_once(self) -> None:
        """Collecte et envoie l'inventaire matériel + logiciel une fois."""
        _logger.info("Collecte de l'inventaire matériel + logiciel...")
        hardware = collectors.collect_hardware()
        software = collectors.collect_software()
        result = self.client.send_inventory(hardware, software)
        if result.ok:
            _logger.info("Inventaire envoyé (%d logiciels).", len(software))
        else:
            _logger.warning("Envoi de l'inventaire en échec : %s", result.error)

    # -- Démarrage des boucles ------------------------------------------------
    def run(self) -> None:
        """Lance l'agent : enrôlement puis boucles, jusqu'à l'arrêt."""
        _logger.info("Démarrage de l'agent TrueSight v%s.", __version__)
        _logger.info("Serveur : %s (verify_tls=%s)", self.config.server_url, self.config.verify_tls)

        # 1. Enrôlement (bloquant avec réessai).
        if not self._ensure_enrolled_blocking():
            _logger.info("Arrêt avant enrôlement complet.")
            self.client.close()
            return

        # 2. Lancement des trois boucles en threads démon.
        loops = [
            ("truesight-heartbeat", self._heartbeat_loop),
            ("truesight-commands", self._commands_loop),
            ("truesight-inventory", self._inventory_loop),
        ]
        for name, target in loops:
            thread = threading.Thread(target=self._guarded_loop, args=(name, target), daemon=True, name=name)
            thread.start()
            self._threads.append(thread)

        # 3. Le thread principal attend l'arrêt.
        try:
            while not self._stop_event.is_set():
                # On vérifie périodiquement la santé des boucles ; on relance
                # celle qui se serait arrêtée anormalement.
                self._restart_dead_loops(loops)
                if self._sleep(5):
                    break
        finally:
            self._shutdown()

    def _guarded_loop(self, name: str, target) -> None:
        """Exécute une boucle ; journalise toute sortie inattendue."""
        try:
            target()
        except Exception as exc:  # noqa: BLE001
            _logger.error("Boucle %s interrompue par une exception : %s", name, exc)

    def _restart_dead_loops(self, loops) -> None:
        """Relance une boucle démon morte (robustesse longue durée)."""
        if self._stop_event.is_set():
            return
        for index, thread in enumerate(self._threads):
            if not thread.is_alive():
                name, target = loops[index]
                _logger.warning("Boucle %s arrêtée, relance.", name)
                new_thread = threading.Thread(
                    target=self._guarded_loop, args=(name, target), daemon=True, name=name
                )
                new_thread.start()
                self._threads[index] = new_thread

    def _shutdown(self) -> None:
        """Arrêt propre : signale l'arrêt et ferme la session HTTP."""
        self._stop_event.set()
        for thread in self._threads:
            try:
                thread.join(timeout=10)
            except RuntimeError:
                pass
        self.client.close()
        _logger.info("Agent TrueSight arrêté proprement.")


# ----------------------------------------------------------------------------
# Garde mono-instance
# ----------------------------------------------------------------------------
# Le handle du mutex est conservé pour toute la durée de vie du process (sinon le
# ramasse-miettes le libérerait et la garde sauterait).
_singleton_handle = None


def _acquire_single_instance() -> bool:
    """Acquiert le mutex mono-instance. Renvoie False si un agent tourne déjà.

    Évite qu'un second agent (ex. process manuel + tâche planifiée) ne tourne en
    parallèle dans la même session. Hors Windows / sans pywin32, la garde est
    inactive (renvoie True).
    """
    global _singleton_handle
    try:
        import win32event
        import win32api
        import winerror
    except Exception:  # noqa: BLE001 - pas de garde sans pywin32.
        return True
    try:
        handle = win32event.CreateMutex(None, False, "TrueSightAgentSingleton")
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            return False
        _singleton_handle = handle
        return True
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Garde mono-instance indisponible (%s), on continue.", exc)
        return True


# ----------------------------------------------------------------------------
# Points d'entrée
# ----------------------------------------------------------------------------
def run(console: bool = False, enroll_only: bool = False) -> int:
    """Point d'entrée commun (console et service).

    - ``console`` : ajoute un handler console au logging.
    - ``enroll_only`` : effectue uniquement l'enrôlement puis sort.

    Retourne un code de sortie (0 = succès).
    """
    setup_logging(console=console)

    try:
        agent_config = cfg.load_config()
    except (FileNotFoundError, ValueError) as exc:
        _logger.error("Configuration invalide : %s", exc)
        return 2

    runner = AgentRunner(agent_config)

    if enroll_only:
        _logger.info("Mode enrôlement seul.")
        if runner._ensure_enrolled_blocking():
            _logger.info("Enrôlement terminé.")
            runner.client.close()
            return 0
        runner.client.close()
        return 1

    # Garde mono-instance : si un agent tourne déjà dans cette session, on sort.
    if not _acquire_single_instance():
        _logger.warning("Un agent TrueSight tourne déjà dans cette session — arrêt de cette instance.")
        runner.client.close()
        return 0

    try:
        runner.run()
    except KeyboardInterrupt:
        _logger.info("Interruption clavier reçue, arrêt.")
        runner.stop()
        runner._shutdown()
    return 0


def create_runner() -> "AgentRunner":
    """Crée un runner configuré (utilisé par le service Windows)."""
    setup_logging(console=False)
    agent_config = cfg.load_config()
    return AgentRunner(agent_config)
