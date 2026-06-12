"""Initialisation des données (cf. SPEC §4.1 / §7).

- ``ensure_admin`` : crée le compte administrateur initial depuis ``ADMIN_EMAIL`` /
  ``ADMIN_PASSWORD`` s'il n'existe pas déjà.
- ``ensure_alert_rules`` : crée les règles d'alerte par défaut si absentes
  (offline, disk_low, cpu_high, ram_high=90).
"""
import logging

from flask import current_app
from sqlalchemy.exc import IntegrityError

from .extensions import db
from .models import AlertRule, User
from .models import utcnow
from .security import hash_password

_logger = logging.getLogger("truesight.seed")


def ensure_admin():
    """Crée l'administrateur initial s'il n'existe pas encore.

    À appeler dans un contexte d'application. Ne fait rien si ``ADMIN_EMAIL`` ou
    ``ADMIN_PASSWORD`` est vide, ou si un utilisateur avec cet email existe déjà.
    """
    email = (current_app.config.get("ADMIN_EMAIL") or "").strip().lower()
    password = current_app.config.get("ADMIN_PASSWORD") or ""

    if not email or not password:
        _logger.warning(
            "ADMIN_EMAIL/ADMIN_PASSWORD non définis : aucun admin initial créé."
        )
        return None

    existing = db.session.query(User).filter(
        db.func.lower(User.email) == email
    ).one_or_none()
    if existing is not None:
        return existing

    admin = User(
        email=email,
        password_hash=hash_password(password),
        role="admin",
        mfa_enabled=False,
        is_active=True,
        created_at=utcnow(),
    )
    db.session.add(admin)
    try:
        db.session.commit()
    except IntegrityError:
        # Course possible si un autre processus a créé l'admin entre-temps.
        db.session.rollback()
        return db.session.query(User).filter(
            db.func.lower(User.email) == email
        ).one_or_none()
    _logger.info("Administrateur initial créé : %s", email)
    return admin


# Règles d'alerte par défaut. Les seuils CPU/disque sont lus depuis la config ;
# le seuil RAM par défaut est fixé à 90 (cf. SPEC §13 de la consigne).
def ensure_alert_rules():
    """Crée les règles d'alerte par défaut si elles n'existent pas déjà."""
    cfg = current_app.config
    defaults = [
        ("offline", float(cfg.get("OFFLINE_THRESHOLD_SECONDS", 300))),
        ("disk_low", float(cfg.get("ALERT_DISK_LOW_PCT", 10))),
        ("cpu_high", float(cfg.get("ALERT_CPU_HIGH_PCT", 90))),
        ("ram_high", float(cfg.get("ALERT_RAM_HIGH_PCT", 90))),
    ]

    created = 0
    for rule_type, threshold in defaults:
        exists = db.session.query(AlertRule).filter_by(type=rule_type).first()
        if exists is None:
            db.session.add(
                AlertRule(type=rule_type, threshold=threshold, is_active=True)
            )
            created += 1

    if created:
        try:
            db.session.commit()
            _logger.info("%s règle(s) d'alerte par défaut créée(s).", created)
        except IntegrityError:
            # Course possible entre processus : les règles existent déjà.
            db.session.rollback()
