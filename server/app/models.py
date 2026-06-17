"""Modèles de données TrueSight (cf. SPEC §1).

Les noms de tables et de colonnes sont NORMATIFS — ils correspondent exactement
au schéma du SPEC. Les UUID sont générés côté Python (default=uuid4).

Portabilité : on utilise les types PostgreSQL natifs (JSONB, ARRAY(text), INET,
numeric, timestamptz) via ``sqlalchemy.dialects.postgresql``, avec une variante
``.with_variant(...)`` pour SQLite afin de permettre les tests en base mémoire.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .extensions import db


def utcnow() -> datetime:
    """Horodatage UTC « aware » (timezone explicite)."""
    return datetime.now(timezone.utc)


# Types portables : PostgreSQL natif + repli SQLite pour les tests.
JSONType = JSONB().with_variant(JSON(), "sqlite")
TextArrayType = ARRAY(Text).with_variant(JSON(), "sqlite")
InetType = INET().with_variant(String(64), "sqlite")
TZDateTime = DateTime(timezone=True)

# Clé primaire bigint auto-incrémentée : BIGINT sous PostgreSQL (équivaut à
# BIGSERIAL via autoincrement), mais INTEGER sous SQLite car SQLite n'auto-
# incrémente que les colonnes « INTEGER PRIMARY KEY ». Le comportement métier
# est identique ; seuls les tests utilisent SQLite.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class Agent(db.Model):
    """Poste enrôlé dans le parc (table ``agents``)."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    machine_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    hostname: Mapped[str | None] = mapped_column(Text)
    agent_version: Mapped[str | None] = mapped_column(Text)
    os_version: Mapped[str | None] = mapped_column(Text)
    enrolled_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tags: Mapped[list] = mapped_column(TextArrayType, default=list)

    # Relations (suppression en cascade côté ORM + côté SGBD).
    hardware = relationship(
        "HardwareInventory", back_populates="agent",
        uselist=False, cascade="all, delete-orphan", passive_deletes=True,
    )
    software = relationship(
        "SoftwareInventory", back_populates="agent",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    metrics = relationship(
        "Metric", back_populates="agent",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    commands = relationship(
        "Command", back_populates="agent",
        cascade="all, delete-orphan", passive_deletes=True,
    )


class HardwareInventory(db.Model):
    """Inventaire matériel courant (1 ligne par agent — table ``hardware_inventory``)."""

    __tablename__ = "hardware_inventory"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    manufacturer: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    serial_number: Mapped[str | None] = mapped_column(Text)
    cpu_model: Mapped[str | None] = mapped_column(Text)
    cpu_cores: Mapped[int | None] = mapped_column(Integer)
    ram_total_mb: Mapped[int | None] = mapped_column(Integer)
    disks: Mapped[list | None] = mapped_column(JSONType)
    mac_addresses: Mapped[list | None] = mapped_column(JSONType)
    collected_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    agent = relationship("Agent", back_populates="hardware")


class SoftwareInventory(db.Model):
    """Logiciel installé (remplacé en bloc par agent — table ``software_inventory``)."""

    __tablename__ = "software_inventory"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text)
    install_date: Mapped[Date | None] = mapped_column(Date)
    collected_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    agent = relationship("Agent", back_populates="software")


class Metric(db.Model):
    """Point de télémétrie (série temporelle — table ``metrics``)."""

    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
    cpu_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    ram_used_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    disk_free: Mapped[dict | None] = mapped_column(JSONType)
    uptime_seconds: Mapped[int | None] = mapped_column(BigInteger)
    logged_in_user: Mapped[str | None] = mapped_column(Text)

    agent = relationship("Agent", back_populates="metrics")

    __table_args__ = (
        db.Index("ix_metrics_agent_ts", "agent_id", ts.desc()),
    )


class User(db.Model):
    """Compte d'accès au dashboard (table ``users``)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, default="viewer", nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(Text)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class Command(db.Model):
    """Commande à distance mise en file (table ``commands``)."""

    __tablename__ = "commands"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=True
    )
    shell: Mapped[str] = mapped_column(Text, nullable=False)
    command_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False, index=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    agent = relationship("Agent", back_populates="commands")
    result = relationship(
        "CommandResult", back_populates="command",
        uselist=False, cascade="all, delete-orphan", passive_deletes=True,
    )


class CommandResult(db.Model):
    """Résultat d'exécution d'une commande (table ``command_results``)."""

    __tablename__ = "command_results"

    command_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("commands.id", ondelete="CASCADE"), primary_key=True
    )
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout: Mapped[str | None] = mapped_column(Text)
    stderr: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(8, 2))
    received_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    command = relationship("Command", back_populates="result")


class AuditLog(db.Model):
    """Journal d'audit append-only (table ``audit_log``)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_agent: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    ip: Mapped[str | None] = mapped_column(InetType)
    details: Mapped[dict | None] = mapped_column(JSONType)


class AlertRule(db.Model):
    """Règle d'alerte configurable (table ``alert_rules``)."""

    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[float] = mapped_column(Numeric)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Alert(db.Model):
    """Alerte déclenchée (table ``alerts``)."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_id: Mapped[int] = mapped_column(Integer, ForeignKey("alert_rules.id"), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AgentRelease(db.Model):
    """Paquet d'agent publié (table ``agent_releases``).

    Le binaire (dossier onedir PyInstaller zippé) est stocké sur le disque
    (volume ``AGENT_RELEASE_DIR``) ; cette table n'en conserve que les
    métadonnées. La release marquée ``is_current`` est celle servie pour
    l'auto-update (heartbeat) ET le lien d'installation.
    """

    __tablename__ = "agent_releases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=True
    )
    published_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class InstallToken(db.Model):
    """Lien d'installation à usage différé (table ``install_tokens``).

    Un admin génère un jeton (stocké hashé) ; le poste cible l'échange contre le
    paquet de l'agent + un ``config.ini`` (URL serveur + enrollment_token). Le
    jeton est révocable et expirable ; le ``enrollment_token`` n'apparaît jamais
    dans l'URL copiée par l'admin (il n'est servi qu'au script d'installation
    via HTTPS, contre le jeton d'installation).
    """

    __tablename__ = "install_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    label: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class RemoteSession(db.Model):
    """Session de bureau à distance (table ``remote_sessions``, cf. REMOTE.md §6).

    Un admin demande une session sur un agent : on crée une ligne ``requested``
    avec le hash SHA-256 du jeton de session (jamais le jeton en clair). Le relais
    WebSocket apparie 1 agent + 1 viewer puis passe la session à ``active`` ; à la
    déconnexion de l'un des deux, elle passe à ``ended``. Le jeton est à usage
    unique et à TTL court (~60 s pour s'apparier).
    """

    __tablename__ = "remote_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=True
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        Text, default="requested", nullable=False, index=True
    )
    # Nature de la session : 'remote' = bureau à distance (capture écran),
    # 'terminal' = terminal interactif (shell PTY). L'agent lit ``kind`` pour
    # décider entre capture vs terminal. Le relais WebSocket est identique dans
    # les deux cas (mêmes chemins /ws/remote/agent et /ws/remote/viewer).
    kind: Mapped[str] = mapped_column(Text, default="remote", nullable=False)
    # Shell utilisé quand ``kind == 'terminal'`` ('powershell' ou 'cmd').
    shell: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime)
