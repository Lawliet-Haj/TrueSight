"""Configuration du serveur ParcVue.

Toutes les valeurs sont lues depuis l'environnement (cf. SPEC §4.1).
Les variables marquées obligatoires lèvent une erreur si absentes en production ;
des valeurs de repli sûres sont fournies pour le développement et les tests.
"""
import os


def _get_bool(name: str, default: bool) -> bool:
    """Lit une variable d'environnement booléenne de façon tolérante."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "oui")


def _get_int(name: str, default: int) -> int:
    """Lit une variable d'environnement entière avec valeur de repli."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    """Lit une variable d'environnement flottante avec valeur de repli."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class Config:
    """Configuration Flask de base, alimentée par l'environnement."""

    # --- Base de données -------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://parcvue:parcvue@db:5432/parcvue",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,  # évite les connexions mortes (VPS, longues inactivités)
        "pool_recycle": 1800,
    }

    # --- Sécurité / sessions --------------------------------------------
    # SECRET_KEY est obligatoire ; valeur de repli uniquement pour le dev/tests.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

    # Cookies de session sécurisés (cf. SPEC §5).
    SESSION_COOKIE_SECURE = _get_bool("SESSION_COOKIE_SECURE", True)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = _get_int("SESSION_LIFETIME_SECONDS", 3600)

    # --- Secrets métier --------------------------------------------------
    ENROLLMENT_TOKEN = os.environ.get("ENROLLMENT_TOKEN", "dev-enrollment-token")

    # --- Intégration n8n (alertes) --------------------------------------
    N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "").strip()

    # --- Pilotage central de l'agent (renvoyé au heartbeat, cf. SPEC §2.2) ---
    # La présence n'a pas besoin d'être instantanée : 30 s suffit (décision projet).
    AGENT_HEARTBEAT_INTERVAL = _get_int("AGENT_HEARTBEAT_INTERVAL", 30)
    AGENT_COMMAND_POLL_INTERVAL = _get_int("AGENT_COMMAND_POLL_INTERVAL", 8)

    # --- Réseau : confiance au reverse-proxy (nginx) --------------------
    # True en production (derrière nginx). Mettre à false pour un accès direct (dev).
    TRUST_PROXY = _get_bool("TRUST_PROXY", True)

    # --- Seuils & rétention ---------------------------------------------
    OFFLINE_THRESHOLD_SECONDS = _get_int("OFFLINE_THRESHOLD_SECONDS", 300)
    METRICS_RETENTION_DAYS = _get_int("METRICS_RETENTION_DAYS", 90)
    ALERT_DISK_LOW_PCT = _get_float("ALERT_DISK_LOW_PCT", 10.0)
    ALERT_CPU_HIGH_PCT = _get_float("ALERT_CPU_HIGH_PCT", 90.0)
    ALERT_RAM_HIGH_PCT = _get_float("ALERT_RAM_HIGH_PCT", 90.0)

    # --- Admin initial ---------------------------------------------------
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@parcvue.local")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

    # --- Limite de troncature des sorties de commande (1 Mo) ------------
    COMMAND_OUTPUT_MAX_BYTES = 1024 * 1024

    # --- Activation du thread de fond -----------------------------------
    ENABLE_BACKGROUND_TASKS = _get_bool("ENABLE_BACKGROUND_TASKS", True)


class TestConfig(Config):
    """Configuration dédiée aux tests (SQLite en mémoire, pas de thread)."""

    SQLALCHEMY_DATABASE_URI = "sqlite+pysqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    TESTING = True
    SESSION_COOKIE_SECURE = False
    SECRET_KEY = "test-secret-key"
    ENROLLMENT_TOKEN = "test-enrollment-token"
    ADMIN_EMAIL = "admin@test.local"
    ADMIN_PASSWORD = "test-admin-password"
    ENABLE_BACKGROUND_TASKS = False
    N8N_WEBHOOK_URL = ""
    TRUST_PROXY = False
