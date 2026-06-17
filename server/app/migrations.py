"""Migrations de schéma idempotentes (PostgreSQL).

``db.create_all()`` crée les tables manquantes mais n'ajoute JAMAIS de colonne à
une table existante. Pour les nouvelles colonnes sur ``agents`` (display_name,
site_id) on exécute donc des ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` au
démarrage — sans effet sur une base neuve, et sans SQL manuel sur le VPS.

Sous SQLite (tests), ``create_all`` crée déjà ``agents`` avec ces colonnes : on
ne fait rien.
"""
import logging

from sqlalchemy import text

from .extensions import db

_logger = logging.getLogger("truesight.migrations")


def ensure_schema():
    """Ajoute les colonnes/contraintes récentes sur une base PostgreSQL existante."""
    bind = db.session.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite (tests) : create_all a déjà le schéma à jour.

    statements = [
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS display_name text",
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS site_id uuid",
        # Emplacement pré-affecté sur un lien d'installation (si la table existait
        # déjà d'un déploiement antérieur sans cette colonne).
        "ALTER TABLE install_tokens ADD COLUMN IF NOT EXISTS site_id uuid",
    ]
    for stmt in statements:
        try:
            db.session.execute(text(stmt))
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 - migration tolérante.
            db.session.rollback()
            _logger.warning("Migration ignorée (%s) : %s", stmt, exc)

    # Contrainte de clé étrangère (pas d'IF NOT EXISTS pour une contrainte :
    # on tente, on ignore si elle existe déjà).
    try:
        db.session.execute(text(
            "ALTER TABLE agents ADD CONSTRAINT fk_agents_site "
            "FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE SET NULL"
        ))
        db.session.commit()
    except Exception:  # noqa: BLE001 - contrainte déjà présente.
        db.session.rollback()

    # Index sur site_id (accélère le filtrage par emplacement).
    try:
        db.session.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_agents_site_id ON agents (site_id)"
        ))
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
