"""Tâches de fond (cf. SPEC §7).

Un unique thread daemon :
- toutes les 60 s : évalue les règles d'alerte (offline, disk_low, cpu_high, ram_high) ;
- une fois par jour : purge les métriques plus anciennes que ``METRICS_RETENTION_DAYS``.

Tout s'exécute dans un ``app.app_context()`` et n'interrompt jamais la boucle en
cas d'erreur (journalisation + continuation).
"""
import logging
import threading
import time
from datetime import timedelta

from .alerts import evaluate_all
from .extensions import db
from .models import Metric
from .models import utcnow

_logger = logging.getLogger("truesight.tasks")

_LOOP_INTERVAL_SECONDS = 60
_PURGE_INTERVAL_SECONDS = 24 * 3600

# Garde-fou : un seul thread de fond par processus.
_started = False
_lock = threading.Lock()


def start_background(app):
    """Démarre le thread de fond (idempotent par processus)."""
    global _started
    with _lock:
        if _started:
            return
        if not app.config.get("ENABLE_BACKGROUND_TASKS", True):
            _logger.info("Tâches de fond désactivées par configuration.")
            return
        _started = True

    thread = threading.Thread(
        target=_run_loop, args=(app,), name="truesight-background", daemon=True
    )
    thread.start()
    _logger.info("Thread de fond TrueSight démarré.")


def _run_loop(app):
    """Boucle principale : alertes toutes les 60 s, purge 1×/jour."""
    last_purge = 0.0
    while True:
        cycle_start = time.monotonic()

        # --- Évaluation des alertes ---
        try:
            with app.app_context():
                evaluate_all(app)
        except Exception:  # pragma: no cover - robustesse
            _logger.exception("Erreur durant l'évaluation des alertes")

        # --- Purge quotidienne des métriques ---
        now = time.monotonic()
        if now - last_purge >= _PURGE_INTERVAL_SECONDS:
            try:
                with app.app_context():
                    _purge_metrics(app)
                last_purge = now
            except Exception:  # pragma: no cover - robustesse
                _logger.exception("Erreur durant la purge des métriques")
                last_purge = now  # évite de boucler en erreur immédiate

        # --- Attente jusqu'au prochain cycle ---
        elapsed = time.monotonic() - cycle_start
        time.sleep(max(1.0, _LOOP_INTERVAL_SECONDS - elapsed))


def _purge_metrics(app):
    """Supprime les métriques plus anciennes que la rétention configurée."""
    retention_days = app.config.get("METRICS_RETENTION_DAYS", 90)
    cutoff = utcnow() - timedelta(days=retention_days)
    deleted = (
        db.session.query(Metric)
        .filter(Metric.ts < cutoff)
        .delete(synchronize_session=False)
    )
    db.session.commit()
    if deleted:
        _logger.info("Purge des métriques : %s lignes supprimées (< %s jours).", deleted, retention_days)


def run_once(app):
    """Exécute un cycle complet (alertes + purge) — utile pour les tests."""
    with app.app_context():
        evaluate_all(app)
        _purge_metrics(app)
