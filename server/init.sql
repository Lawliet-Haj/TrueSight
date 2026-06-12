-- ============================================================================
-- TrueSight — schéma PostgreSQL 16 (référence / déploiement manuel)
-- Équivalent au schéma créé par SQLAlchemy via db.create_all() (cf. SPEC §1).
-- Les noms de tables et de colonnes sont NORMATIFS.
-- ============================================================================

-- Extension requise pour gen_random_uuid() (alternative aux UUID générés côté app).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ----------------------------------------------------------------------------
-- agents
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    machine_id    TEXT NOT NULL UNIQUE,
    hostname      TEXT,
    agent_version TEXT,
    os_version    TEXT,
    enrolled_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ,
    token_hash    TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    tags          TEXT[] NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_agents_machine_id ON agents (machine_id);

-- ----------------------------------------------------------------------------
-- hardware_inventory (1 ligne courante par agent)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hardware_inventory (
    agent_id      UUID PRIMARY KEY REFERENCES agents (id) ON DELETE CASCADE,
    manufacturer  TEXT,
    model         TEXT,
    serial_number TEXT,
    cpu_model     TEXT,
    cpu_cores     INTEGER,
    ram_total_mb  INTEGER,
    disks         JSONB,
    mac_addresses JSONB,
    collected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- software_inventory (remplacé en bloc par agent à chaque collecte)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS software_inventory (
    id           BIGSERIAL PRIMARY KEY,
    agent_id     UUID NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    name         TEXT,
    version      TEXT,
    publisher    TEXT,
    install_date DATE,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_software_agent ON software_inventory (agent_id);

-- ----------------------------------------------------------------------------
-- metrics (série temporelle)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metrics (
    id             BIGSERIAL PRIMARY KEY,
    agent_id       UUID NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    cpu_pct        NUMERIC(5, 2),
    ram_used_pct   NUMERIC(5, 2),
    disk_free      JSONB,
    uptime_seconds BIGINT,
    logged_in_user TEXT
);
CREATE INDEX IF NOT EXISTS ix_metrics_agent_ts ON metrics (agent_id, ts DESC);

-- ----------------------------------------------------------------------------
-- users (accès dashboard)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'viewer',
    mfa_secret    TEXT,
    mfa_enabled   BOOLEAN NOT NULL DEFAULT FALSE,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- commands
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commands (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    created_by      UUID REFERENCES users (id),
    shell           TEXT NOT NULL,
    command_text    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    timeout_seconds INTEGER NOT NULL DEFAULT 120,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at   TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_commands_agent ON commands (agent_id);
CREATE INDEX IF NOT EXISTS ix_commands_status ON commands (status);

-- ----------------------------------------------------------------------------
-- command_results
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS command_results (
    command_id       UUID PRIMARY KEY REFERENCES commands (id) ON DELETE CASCADE,
    exit_code        INTEGER,
    stdout           TEXT,
    stderr           TEXT,
    duration_seconds NUMERIC(8, 2),
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- audit_log (append-only)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id      UUID,
    action       TEXT NOT NULL,
    target_agent UUID,
    ip           INET,
    details      JSONB
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log (ts DESC);

-- ----------------------------------------------------------------------------
-- alert_rules
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_rules (
    id        SERIAL PRIMARY KEY,
    type      TEXT NOT NULL,
    threshold NUMERIC,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

-- ----------------------------------------------------------------------------
-- alerts
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id           BIGSERIAL PRIMARY KEY,
    agent_id     UUID NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    rule_id      INTEGER NOT NULL REFERENCES alert_rules (id),
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ,
    notified     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_alerts_agent ON alerts (agent_id);

-- ----------------------------------------------------------------------------
-- remote_sessions (bureau à distance, cf. REMOTE.md §6)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS remote_sessions (
    id            UUID PRIMARY KEY,
    agent_id      UUID NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
    admin_user_id UUID REFERENCES users (id),
    token_hash    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'requested',
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    ended_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_remote_sessions_agent ON remote_sessions (agent_id);
CREATE INDEX IF NOT EXISTS ix_remote_sessions_token ON remote_sessions (token_hash);
CREATE INDEX IF NOT EXISTS ix_remote_sessions_status ON remote_sessions (status);

-- ----------------------------------------------------------------------------
-- Règles d'alerte par défaut (équivalent de seed.ensure_alert_rules()).
-- Les seuils peuvent être ajustés selon la configuration applicative.
-- ----------------------------------------------------------------------------
INSERT INTO alert_rules (type, threshold, is_active)
SELECT 'offline', 300, TRUE
WHERE NOT EXISTS (SELECT 1 FROM alert_rules WHERE type = 'offline');

INSERT INTO alert_rules (type, threshold, is_active)
SELECT 'disk_low', 10, TRUE
WHERE NOT EXISTS (SELECT 1 FROM alert_rules WHERE type = 'disk_low');

INSERT INTO alert_rules (type, threshold, is_active)
SELECT 'cpu_high', 90, TRUE
WHERE NOT EXISTS (SELECT 1 FROM alert_rules WHERE type = 'cpu_high');

INSERT INTO alert_rules (type, threshold, is_active)
SELECT 'ram_high', 90, TRUE
WHERE NOT EXISTS (SELECT 1 FROM alert_rules WHERE type = 'ram_high');
