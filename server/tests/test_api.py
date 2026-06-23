"""Tests d'intégration TrueSight (chemin nominal).

Utilise le client de test Flask + SQLite en mémoire (TestConfig).
Couvre : enroll, heartbeat, inventory, création de commande (admin), poll agent,
remontée de résultat, et lecture côté dashboard.
"""
import os
import sys

import pytest

# Permet d'importer le paquet ``app`` depuis le dossier server/.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.extensions import db  # noqa: E402


@pytest.fixture()
def app():
    """Crée une application de test isolée (SQLite mémoire, sans thread de fond)."""
    application = create_app(TestConfig)
    yield application
    # Nettoyage : on vide la base mémoire.
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _reset_enroll_rate_limit():
    """Réinitialise le rate-limiter mémoire de /enroll entre les tests.

    Le compteur ``_enroll_hits`` est un état global de module (par IP, fenêtre
    glissante de 60 s) : sans réinitialisation, l'accumulation des enrôlements
    des différents tests finit par déclencher le 429 et rend la suite dépendante
    de l'ordre d'exécution.
    """
    from app import api_agent, web

    api_agent._enroll_hits.clear()
    web._login_failures.clear()
    yield


@pytest.fixture()
def client(app):
    """Client de test Flask."""
    return app.test_client()


@pytest.fixture()
def admin_session(client, app):
    """Connecte le client en tant qu'admin (sans MFA) et renvoie le client."""
    resp = client.post(
        "/login",
        data={"email": TestConfig.ADMIN_EMAIL, "password": TestConfig.ADMIN_PASSWORD},
        follow_redirects=False,
    )
    # 302 vers /agents en cas de succès.
    assert resp.status_code in (302, 303)
    return client


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _enroll(client, machine_id="MACHINE-TEST-001"):
    """Enrôle un agent et renvoie (agent_id, agent_token)."""
    resp = client.post(
        "/api/v1/enroll",
        json={
            "enrollment_token": TestConfig.ENROLLMENT_TOKEN,
            "machine_id": machine_id,
            "hostname": "PC-TEST-01",
            "os_version": "Windows 11 Pro 26100",
            "agent_version": "1.0.0",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    assert "agent_id" in data and "agent_token" in data
    return data["agent_id"], data["agent_token"]


def _auth(token):
    """En-tête d'authentification agent."""
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_healthz(client):
    """Le point de santé répond OK."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_enroll_invalid_token(client):
    """Un token d'enrôlement invalide renvoie 401."""
    resp = client.post(
        "/api/v1/enroll",
        json={"enrollment_token": "mauvais", "machine_id": "X"},
    )
    assert resp.status_code == 401


def test_enroll_is_idempotent_and_rotates_token(client):
    """Réenrôler le même machine_id renvoie le même agent_id avec un nouveau token."""
    aid1, tok1 = _enroll(client, "MACHINE-IDEM")
    aid2, tok2 = _enroll(client, "MACHINE-IDEM")
    assert aid1 == aid2
    assert tok1 != tok2  # rotation du token


def test_heartbeat(client):
    """Le heartbeat met à jour l'agent, insère une métrique et renvoie la config."""
    agent_id, token = _enroll(client)
    resp = client.post(
        f"/api/v1/agents/{agent_id}/heartbeat",
        json={
            "metrics": {
                "cpu_pct": 12.34,
                "ram_used_pct": 45.6,
                "ram_total_mb": 16384,
                "disk_free": {"C:": 42.1, "D:": 870.3},
                "uptime_seconds": 123456,
                "logged_in_user": "MEDICOFI\\jdupont",
            }
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["pending_commands"] == 0
    assert "config" in body and "heartbeat_interval" in body["config"]


def test_heartbeat_requires_auth(client):
    """Le heartbeat sans Authorization renvoie 401."""
    agent_id, _ = _enroll(client)
    resp = client.post(f"/api/v1/agents/{agent_id}/heartbeat", json={"metrics": {}})
    assert resp.status_code == 401


def test_heartbeat_wrong_token(client):
    """Un token agent erroné renvoie 401."""
    agent_id, _ = _enroll(client)
    resp = client.post(
        f"/api/v1/agents/{agent_id}/heartbeat",
        json={"metrics": {}},
        headers=_auth("token-bidon"),
    )
    assert resp.status_code == 401


def test_inventory(client):
    """L'inventaire upsert le matériel et remplace le logiciel."""
    agent_id, token = _enroll(client)
    payload = {
        "hardware": {
            "manufacturer": "Dell Inc.",
            "model": "Latitude 5520",
            "serial_number": "ABC123",
            "cpu_model": "Intel Core i5-1145G7",
            "cpu_cores": 8,
            "ram_total_mb": 16384,
            "disks": [{"drive": "C:", "total_gb": 237.5, "free_gb": 42.1}],
            "mac_addresses": ["AA:BB:CC:DD:EE:FF"],
        },
        "software": [
            {"name": "Google Chrome", "version": "125.0", "publisher": "Google LLC", "install_date": "2026-01-12"},
            {"name": "7-Zip", "version": "23.01", "publisher": "Igor Pavlov", "install_date": None},
        ],
    }
    resp = client.post(f"/api/v1/agents/{agent_id}/inventory", json=payload, headers=_auth(token))
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    # Réinventaire avec un seul logiciel : remplacement complet.
    payload["software"] = [{"name": "Firefox", "version": "126.0", "publisher": "Mozilla"}]
    resp2 = client.post(f"/api/v1/agents/{agent_id}/inventory", json=payload, headers=_auth(token))
    assert resp2.status_code == 200


def test_command_full_cycle(client, admin_session):
    """Cycle complet : création (admin), poll agent, remontée de résultat, lecture."""
    # 1) Enrôlement agent.
    agent_id, token = _enroll(client, "MACHINE-CMD")

    # 2) Création d'une commande par l'admin.
    resp = admin_session.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"shell": "powershell", "command_text": "Get-Service spooler", "timeout_seconds": 60},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    command_id = resp.get_json()["command_id"]

    # 3) Le heartbeat doit signaler 1 commande en attente.
    hb = client.post(f"/api/v1/agents/{agent_id}/heartbeat", json={"metrics": {}}, headers=_auth(token))
    assert hb.get_json()["pending_commands"] == 1

    # 4) L'agent récupère la commande (pending -> dispatched).
    poll = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    assert poll.status_code == 200
    cmds = poll.get_json()["commands"]
    assert len(cmds) == 1
    assert cmds[0]["id"] == command_id
    assert cmds[0]["shell"] == "powershell"

    # Un second poll ne renvoie plus rien (déjà dispatched).
    poll2 = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    assert poll2.get_json()["commands"] == []

    # 5) L'agent remonte le résultat.
    res = client.post(
        f"/api/v1/commands/{command_id}/result",
        json={"exit_code": 0, "stdout": "Running  Spooler", "stderr": "", "duration_seconds": 1.23},
        headers=_auth(token),
    )
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    # 6) Lecture côté dashboard : statut done + résultat.
    status = admin_session.get(f"/api/v1/commands/{command_id}")
    assert status.status_code == 200
    sdata = status.get_json()
    assert sdata["status"] == "done"
    assert sdata["result"]["exit_code"] == 0
    assert "Spooler" in sdata["result"]["stdout"]


def test_command_error_status(client, admin_session):
    """Un exit_code non nul passe la commande en statut 'error'."""
    agent_id, token = _enroll(client, "MACHINE-ERR")
    resp = admin_session.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"shell": "cmd", "command_text": "exit 1"},
    )
    command_id = resp.get_json()["command_id"]
    client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    client.post(
        f"/api/v1/commands/{command_id}/result",
        json={"exit_code": 1, "stdout": "", "stderr": "échec", "duration_seconds": 0.5},
        headers=_auth(token),
    )
    status = admin_session.get(f"/api/v1/commands/{command_id}")
    assert status.get_json()["status"] == "error"


def test_dashboard_requires_login(client):
    """L'API dashboard renvoie 401 sans session."""
    resp = client.get("/api/v1/agents", headers={"Accept": "application/json"})
    assert resp.status_code == 401


def test_dashboard_list_and_detail(client, admin_session):
    """La liste et le détail des agents sont accessibles à un admin connecté."""
    agent_id, token = _enroll(client, "MACHINE-DASH")
    client.post(
        f"/api/v1/agents/{agent_id}/heartbeat",
        json={"metrics": {"cpu_pct": 5.0, "ram_used_pct": 30.0}},
        headers=_auth(token),
    )

    listing = admin_session.get("/api/v1/agents")
    assert listing.status_code == 200
    agents = listing.get_json()
    assert any(a["id"] == agent_id for a in agents)
    # L'agent vient d'émettre un heartbeat -> online.
    target = next(a for a in agents if a["id"] == agent_id)
    assert target["status"] == "online"

    detail = admin_session.get(f"/api/v1/agents/{agent_id}")
    assert detail.status_code == 200
    assert detail.get_json()["hostname"] == "PC-TEST-01"


def test_command_creation_requires_admin(client):
    """La création de commande sans session admin est refusée (401)."""
    agent_id, _ = _enroll(client, "MACHINE-NOADMIN")
    resp = client.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"shell": "cmd", "command_text": "dir"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 401


def test_metrics_endpoint(client, admin_session):
    """Les métriques sont renvoyées en série temporelle."""
    agent_id, token = _enroll(client, "MACHINE-METRICS")
    client.post(
        f"/api/v1/agents/{agent_id}/heartbeat",
        json={"metrics": {"cpu_pct": 10.0, "ram_used_pct": 20.0, "disk_free": {"C:": 50.0}, "uptime_seconds": 100}},
        headers=_auth(token),
    )
    resp = admin_session.get(f"/api/v1/agents/{agent_id}/metrics?hours=24")
    assert resp.status_code == 200
    rows = resp.get_json()
    assert len(rows) >= 1
    assert rows[0]["cpu_pct"] == 10.0


def test_audit_records_command_creation(client, admin_session):
    """La création d'une commande génère une entrée d'audit lisible par l'admin."""
    agent_id, _ = _enroll(client, "MACHINE-AUDIT")
    admin_session.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"shell": "powershell", "command_text": "Get-Date"},
    )
    resp = admin_session.get("/api/v1/audit?limit=50")
    assert resp.status_code == 200
    actions = [e["action"] for e in resp.get_json()]
    assert "command.create" in actions
    assert "login.success" in actions


# --------------------------------------------------------------------------
# Bureau à distance (remote sessions) — R1+R2
# --------------------------------------------------------------------------
def test_remote_session_create_by_admin(client, admin_session):
    """Un admin crée une session de bureau à distance : 201 + token + ws_url viewer."""
    agent_id, _ = _enroll(client, "MACHINE-REMOTE")

    resp = admin_session.post(f"/api/v1/agents/{agent_id}/remote-session")
    assert resp.status_code == 201, resp.get_data(as_text=True)
    data = resp.get_json()
    assert "session_id" in data
    assert data.get("token")  # jeton en clair non vide
    assert data["ws_url"].endswith(f"/ws/remote/viewer?token={data['token']}")
    # Le scheme WebSocket est dérivé du host (ws en test http, jamais http://).
    assert data["ws_url"].startswith(("ws://", "wss://"))

    # L'audit enregistre remote.start.
    audit = admin_session.get("/api/v1/audit?limit=50")
    assert "remote.start" in [e["action"] for e in audit.get_json()]

    # Le statut de la session est consultable et vaut 'requested' avant appariement.
    status = admin_session.get(f"/api/v1/remote-sessions/{data['session_id']}")
    assert status.status_code == 200
    assert status.get_json()["status"] == "requested"


def test_remote_session_requires_admin(client):
    """Créer une session de bureau à distance sans session admin est refusé (401/403)."""
    agent_id, _ = _enroll(client, "MACHINE-REMOTE-NOADMIN")
    resp = client.post(
        f"/api/v1/agents/{agent_id}/remote-session",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code in (401, 403)


def test_remote_session_signaled_to_agent(client, admin_session):
    """Après création, la réponse GET /commands de l'agent inclut le champ remote_session."""
    agent_id, token = _enroll(client, "MACHINE-REMOTE-SIGNAL")

    # Avant toute session : le champ est présent mais nul.
    poll0 = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    assert poll0.status_code == 200
    assert "remote_session" in poll0.get_json()
    assert poll0.get_json()["remote_session"] is None

    # L'admin demande une session.
    created = admin_session.post(f"/api/v1/agents/{agent_id}/remote-session")
    expected_token = created.get_json()["token"]
    expected_session_id = created.get_json()["session_id"]

    # L'agent voit désormais la signalisation (avec le jeton + ws_url agent).
    poll = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    rs = poll.get_json()["remote_session"]
    assert rs is not None
    assert rs["session_id"] == expected_session_id
    assert rs["token"] == expected_token
    assert rs["ws_url"].endswith(f"/ws/remote/agent?token={expected_token}")

    # Le heartbeat porte la même signalisation.
    hb = client.post(
        f"/api/v1/agents/{agent_id}/heartbeat", json={"metrics": {}}, headers=_auth(token)
    )
    hb_rs = hb.get_json()["remote_session"]
    assert hb_rs is not None
    assert hb_rs["session_id"] == expected_session_id


# --------------------------------------------------------------------------
# Terminal interactif (remote sessions kind='terminal')
# --------------------------------------------------------------------------
def test_remote_session_terminal_create(client, admin_session):
    """Une session kind='terminal' shell='powershell' : 201 + kind/shell renvoyés."""
    agent_id, _ = _enroll(client, "MACHINE-TERMINAL")

    resp = admin_session.post(
        f"/api/v1/agents/{agent_id}/remote-session",
        json={"kind": "terminal", "shell": "powershell"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["kind"] == "terminal"
    assert data["shell"] == "powershell"
    # Le ws_url reste sur le chemin viewer, inchangé par le kind.
    assert data["ws_url"].endswith(f"/ws/remote/viewer?token={data['token']}")


def test_remote_session_default_kind_remote(client, admin_session):
    """Sans body, kind vaut 'remote' et shell est nul."""
    agent_id, _ = _enroll(client, "MACHINE-DEFAULT-KIND")
    resp = admin_session.post(f"/api/v1/agents/{agent_id}/remote-session")
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["kind"] == "remote"
    assert data["shell"] is None


def test_remote_session_terminal_signaled_to_agent(client, admin_session):
    """GET /commands renvoie remote_session avec kind quand une session terminal est demandée."""
    agent_id, token = _enroll(client, "MACHINE-TERMINAL-SIGNAL")

    admin_session.post(
        f"/api/v1/agents/{agent_id}/remote-session",
        json={"kind": "terminal", "shell": "cmd"},
    )

    poll = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    rs = poll.get_json()["remote_session"]
    assert rs is not None
    assert rs["kind"] == "terminal"
    assert rs["shell"] == "cmd"


# --------------------------------------------------------------------------
# Actions rapides (quick-action)
# --------------------------------------------------------------------------
def test_quick_action_lock(client, admin_session):
    """Une action rapide 'lock' (admin) : 201 + command_id + command_text attendu."""
    agent_id, token = _enroll(client, "MACHINE-QUICK-LOCK")

    resp = admin_session.post(
        f"/api/v1/agents/{agent_id}/quick-action", json={"action": "lock"}
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    command_id = resp.get_json()["command_id"]

    # La commande créée a le bon command_text et le shell cmd.
    poll = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    cmds = poll.get_json()["commands"]
    assert len(cmds) == 1
    assert cmds[0]["id"] == command_id
    assert cmds[0]["shell"] == "cmd"
    assert cmds[0]["command_text"] == "rundll32.exe user32.dll,LockWorkStation"


def test_quick_action_requires_admin(client):
    """Une action rapide sans session admin est refusée (401/403)."""
    agent_id, _ = _enroll(client, "MACHINE-QUICK-NOADMIN")
    resp = client.post(
        f"/api/v1/agents/{agent_id}/quick-action",
        json={"action": "lock"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code in (401, 403)


def test_quick_action_message_requires_text(client, admin_session):
    """Une action 'message' sans texte renvoie 400."""
    agent_id, _ = _enroll(client, "MACHINE-QUICK-MSG")
    resp = admin_session.post(
        f"/api/v1/agents/{agent_id}/quick-action",
        json={"action": "message", "text": ""},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Alertes (GET /api/v1/alerts)
# --------------------------------------------------------------------------
def _seed_alert(app, agent_id, rule_type, threshold, resolved=False):
    """Crée directement une alerte (et sa règle si besoin) en base pour les tests."""
    import uuid as _uuid

    from app.extensions import db
    from app.models import Alert, AlertRule, utcnow

    with app.app_context():
        rule = db.session.query(AlertRule).filter_by(type=rule_type).first()
        if rule is None:
            rule = AlertRule(type=rule_type, threshold=threshold, is_active=True)
            db.session.add(rule)
            db.session.flush()
        alert = Alert(
            agent_id=_uuid.UUID(str(agent_id)),
            rule_id=rule.id,
            triggered_at=utcnow(),
            resolved_at=utcnow() if resolved else None,
            notified=False,
        )
        db.session.add(alert)
        db.session.commit()
        return alert.id


def test_alerts_requires_login(client):
    """L'API des alertes renvoie 401 sans session."""
    resp = client.get("/api/v1/alerts", headers={"Accept": "application/json"})
    assert resp.status_code == 401


def test_alerts_list_structure_and_status_filter(client, admin_session, app):
    """La liste des alertes a la bonne structure et le filtre status fonctionne."""
    agent_id, _ = _enroll(client, "MACHINE-ALERTS")
    _seed_alert(app, agent_id, "cpu_high", 90.0, resolved=False)
    _seed_alert(app, agent_id, "disk_low", 10.0, resolved=True)

    # Défaut : status=active -> seules les alertes non résolues.
    resp = admin_session.get("/api/v1/alerts")
    assert resp.status_code == 200
    active = resp.get_json()
    assert len(active) == 1
    a = active[0]
    for key in (
        "id", "agent_id", "hostname", "type", "threshold",
        "triggered_at", "resolved_at", "notified", "active",
    ):
        assert key in a
    assert a["hostname"] == "PC-TEST-01"
    assert a["type"] == "cpu_high"
    assert a["threshold"] == 90.0
    assert a["active"] is True
    assert a["resolved_at"] is None
    assert a["notified"] is False

    # status=all -> les deux alertes (active + résolue).
    resp_all = admin_session.get("/api/v1/alerts?status=all")
    assert resp_all.status_code == 200
    rows = resp_all.get_json()
    assert len(rows) == 2
    assert any(r["active"] is False and r["resolved_at"] is not None for r in rows)


# --------------------------------------------------------------------------
# Inventaire logiciel agrégé (GET /api/v1/inventory/software)
# --------------------------------------------------------------------------
def _post_inventory(client, agent_id, token, software):
    """Pousse un inventaire (matériel minimal + logiciels) pour un agent."""
    payload = {
        "hardware": {"manufacturer": "Dell Inc.", "model": "Latitude"},
        "software": software,
    }
    resp = client.post(
        f"/api/v1/agents/{agent_id}/inventory", json=payload, headers=_auth(token)
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_inventory_software_aggregation_and_filter(client, admin_session):
    """L'inventaire logiciel est agrégé (agent_count) et filtrable via q."""
    aid1, tok1 = _enroll(client, "MACHINE-INV-1")
    aid2, tok2 = _enroll(client, "MACHINE-INV-2")

    chrome = {"name": "Google Chrome", "version": "125.0", "publisher": "Google LLC"}
    seven = {"name": "7-Zip", "version": "23.01", "publisher": "Igor Pavlov"}
    firefox = {"name": "Mozilla Firefox", "version": "126.0", "publisher": "Mozilla"}

    # Chrome sur les deux postes -> agent_count = 2 ; 7-Zip et Firefox sur un seul.
    _post_inventory(client, aid1, tok1, [chrome, seven])
    _post_inventory(client, aid2, tok2, [chrome, firefox])

    resp = admin_session.get("/api/v1/inventory/software")
    assert resp.status_code == 200
    rows = resp.get_json()
    by_name = {r["name"]: r for r in rows}
    assert by_name["Google Chrome"]["agent_count"] == 2
    assert by_name["Google Chrome"]["version"] == "125.0"
    assert by_name["Google Chrome"]["publisher"] == "Google LLC"
    assert by_name["7-Zip"]["agent_count"] == 1
    # Tri par name croissant.
    names = [r["name"] for r in rows]
    assert names == sorted(names)

    # Filtre q insensible à la casse sur le nom.
    resp_q = admin_session.get("/api/v1/inventory/software?q=chrome")
    assert resp_q.status_code == 200
    q_rows = resp_q.get_json()
    assert len(q_rows) == 1
    assert q_rows[0]["name"] == "Google Chrome"

    # Filtre q sur l'éditeur.
    resp_pub = admin_session.get("/api/v1/inventory/software?q=mozilla")
    assert {r["name"] for r in resp_pub.get_json()} == {"Mozilla Firefox"}


# --------------------------------------------------------------------------
# Réglages — mot de passe
# --------------------------------------------------------------------------
def test_settings_password_change_success(client, admin_session):
    """Changer le mot de passe avec le bon ancien et un nouveau valide : 200."""
    resp = admin_session.post(
        "/api/v1/settings/password",
        json={
            "current_password": TestConfig.ADMIN_PASSWORD,
            "new_password": "nouveau-mot-de-passe-1",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["ok"] is True

    # L'audit enregistre settings.password.
    audit = admin_session.get("/api/v1/audit?limit=50")
    assert "settings.password" in [e["action"] for e in audit.get_json()]


def test_settings_password_wrong_current(client, admin_session):
    """Un mot de passe actuel erroné renvoie 401."""
    resp = admin_session.post(
        "/api/v1/settings/password",
        json={"current_password": "faux", "new_password": "nouveau-mot-de-passe-1"},
    )
    assert resp.status_code == 401


def test_settings_password_too_short(client, admin_session):
    """Un nouveau mot de passe de moins de 8 caractères renvoie 400."""
    resp = admin_session.post(
        "/api/v1/settings/password",
        json={"current_password": TestConfig.ADMIN_PASSWORD, "new_password": "court"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Réglages — MFA (cycle setup -> enable -> disable)
# --------------------------------------------------------------------------
def test_settings_mfa_setup_enable_disable_cycle(client, admin_session):
    """Cycle MFA complet : statut initial, setup, enable (code TOTP), disable."""
    import pyotp

    # Statut initial : désactivé.
    status0 = admin_session.get("/api/v1/settings/mfa")
    assert status0.status_code == 200
    assert status0.get_json()["enabled"] is False

    # Setup : renvoie un secret + URI otpauth + QR PNG en data-URI.
    setup = admin_session.post("/api/v1/settings/mfa/setup")
    assert setup.status_code == 200
    sdata = setup.get_json()
    assert sdata["secret"]
    assert sdata["otpauth_uri"].startswith("otpauth://totp/")
    assert "issuer=TrueSight" in sdata["otpauth_uri"]
    assert sdata["qr_png_base64"].startswith("data:image/png;base64,")

    # Enable avec un mauvais code -> 400.
    bad = admin_session.post("/api/v1/settings/mfa/enable", json={"code": "000000"})
    assert bad.status_code == 400

    # Enable avec le bon code TOTP calculé depuis le secret en attente -> 200.
    code = pyotp.TOTP(sdata["secret"]).now()
    enable = admin_session.post("/api/v1/settings/mfa/enable", json={"code": code})
    assert enable.status_code == 200, enable.get_data(as_text=True)
    assert enable.get_json()["ok"] is True

    # Le statut est désormais activé.
    status1 = admin_session.get("/api/v1/settings/mfa")
    assert status1.get_json()["enabled"] is True

    # Disable avec un mauvais mot de passe -> 401.
    bad_disable = admin_session.post(
        "/api/v1/settings/mfa/disable", json={"password": "faux"}
    )
    assert bad_disable.status_code == 401

    # Disable avec le bon mot de passe -> 200, MFA désactivé.
    disable = admin_session.post(
        "/api/v1/settings/mfa/disable",
        json={"password": TestConfig.ADMIN_PASSWORD},
    )
    assert disable.status_code == 200
    assert disable.get_json()["ok"] is True

    status2 = admin_session.get("/api/v1/settings/mfa")
    assert status2.get_json()["enabled"] is False

    # L'audit contient enable + disable.
    actions = [e["action"] for e in admin_session.get("/api/v1/audit?limit=50").get_json()]
    assert "settings.mfa.enable" in actions
    assert "settings.mfa.disable" in actions


def test_settings_mfa_enable_without_pending(client, admin_session):
    """Enable sans secret en attente renvoie 400."""
    resp = admin_session.post("/api/v1/settings/mfa/enable", json={"code": "123456"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# Gestion des accès (superadmin) — /api/v1/users
# --------------------------------------------------------------------------
def test_seeded_admin_is_superadmin(client, admin_session):
    """Le compte initial (ADMIN_EMAIL) est promu/créé super-administrateur."""
    me = next(u for u in admin_session.get("/api/v1/users").get_json() if u["is_self"])
    assert me["role"] == "superadmin"


def test_users_unauthenticated(client):
    """Sans session, la gestion des accès renvoie 401."""
    assert client.get("/api/v1/users", headers={"Accept": "application/json"}).status_code == 401


def test_users_management_forbidden_for_admin(client, admin_session):
    """Un simple administrateur (non superadmin) ne peut pas gérer les accès (403)."""
    r = admin_session.post(
        "/api/v1/users",
        json={"email": "adm@medicofi.fr", "password": "adminpass1", "role": "admin"},
    )
    assert r.status_code == 201, r.get_data(as_text=True)

    admin_session.get("/logout")
    login = admin_session.post(
        "/login", data={"email": "adm@medicofi.fr", "password": "adminpass1"}
    )
    assert login.status_code in (302, 303)

    assert admin_session.get(
        "/api/v1/users", headers={"Accept": "application/json"}
    ).status_code == 403
    assert admin_session.post(
        "/api/v1/users",
        json={"email": "z@z.fr", "password": "abcdefgh", "role": "viewer"},
    ).status_code == 403


def test_user_management_cycle(client, admin_session):
    """CRUD complet d'un accès par le superadmin + validations + audit."""
    # Validations de création.
    assert admin_session.post(
        "/api/v1/users", json={"email": "bad", "password": "abcdefgh", "role": "viewer"}
    ).status_code == 400
    assert admin_session.post(
        "/api/v1/users", json={"email": "a@b.fr", "password": "court", "role": "viewer"}
    ).status_code == 400
    assert admin_session.post(
        "/api/v1/users", json={"email": "a@b.fr", "password": "abcdefgh", "role": "king"}
    ).status_code == 400

    # Création OK.
    created = admin_session.post(
        "/api/v1/users",
        json={"email": "op@medicofi.fr", "password": "motdepasse1", "role": "admin"},
    )
    assert created.status_code == 201, created.get_data(as_text=True)
    uid = created.get_json()["id"]

    # E-mail en doublon -> 409.
    assert admin_session.post(
        "/api/v1/users",
        json={"email": "op@medicofi.fr", "password": "motdepasse1", "role": "viewer"},
    ).status_code == 409

    # Changement de rôle, activation/désactivation, reset de mot de passe.
    assert admin_session.post(f"/api/v1/users/{uid}/role", json={"role": "viewer"}).status_code == 200
    assert admin_session.post(f"/api/v1/users/{uid}/active", json={"active": False}).status_code == 200
    assert admin_session.post(f"/api/v1/users/{uid}/active", json={"active": True}).status_code == 200
    assert admin_session.post(
        f"/api/v1/users/{uid}/reset-password", json={"new_password": "court"}
    ).status_code == 400
    assert admin_session.post(
        f"/api/v1/users/{uid}/reset-password", json={"new_password": "nouveaupass1"}
    ).status_code == 200

    # Le nouveau mot de passe permet de se connecter.
    admin_session.get("/logout")
    assert admin_session.post(
        "/login", data={"email": "op@medicofi.fr", "password": "nouveaupass1"}
    ).status_code in (302, 303)
    admin_session.get("/logout")
    admin_session.post(
        "/login", data={"email": TestConfig.ADMIN_EMAIL, "password": TestConfig.ADMIN_PASSWORD}
    )

    # Suppression.
    assert admin_session.delete(f"/api/v1/users/{uid}").status_code == 200
    assert all(u["id"] != uid for u in admin_session.get("/api/v1/users").get_json())

    # Audit complet.
    actions = [e["action"] for e in admin_session.get("/api/v1/audit?limit=100").get_json()]
    for a in ("user.create", "user.role", "user.active", "user.password_reset", "user.delete"):
        assert a in actions


def test_user_guards_last_superadmin_and_self(client, admin_session):
    """Garde-fous : pas de rétrogradation/désactivation/suppression du dernier superadmin (soi-même)."""
    me = next(u for u in admin_session.get("/api/v1/users").get_json() if u["is_self"])
    uid = me["id"]
    assert admin_session.post(f"/api/v1/users/{uid}/role", json={"role": "viewer"}).status_code == 409
    assert admin_session.post(f"/api/v1/users/{uid}/active", json={"active": False}).status_code == 409
    assert admin_session.delete(f"/api/v1/users/{uid}").status_code == 409


# --------------------------------------------------------------------------
# Durcissement : en-têtes de sécurité + anti-bruteforce du login
# --------------------------------------------------------------------------
def test_security_headers_present(client):
    """Les en-têtes de sécurité de base sont posés sur toute réponse."""
    resp = client.get("/login")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "Referrer-Policy" in resp.headers
    assert "Permissions-Policy" in resp.headers
    # En HTTP (pile de test), pas de HSTS.
    assert "Strict-Transport-Security" not in resp.headers


def test_login_bruteforce_rate_limited(client):
    """Après trop d'échecs de connexion, l'IP est temporairement bloquée (429)."""
    from app import web

    # Les 10 premières tentatives erronées renvoient 401.
    for _ in range(web._LOGIN_MAX_FAILURES):
        r = client.post("/login", data={"email": "admin@test.local", "password": "faux"})
        assert r.status_code == 401

    # La suivante est bloquée (429), MÊME avec le bon mot de passe.
    blocked = client.post(
        "/login",
        data={"email": TestConfig.ADMIN_EMAIL, "password": TestConfig.ADMIN_PASSWORD},
    )
    assert blocked.status_code == 429

    # L'audit conserve les échecs et le blocage.
    web._login_failures.clear()
    actions = []
    # On se connecte proprement pour lire l'audit.
    client.post("/login", data={"email": TestConfig.ADMIN_EMAIL, "password": TestConfig.ADMIN_PASSWORD})
    actions = [e["action"] for e in client.get("/api/v1/audit?limit=100").get_json()]
    assert "login.fail" in actions
    assert "login.blocked" in actions


def test_login_success_clears_failures(client):
    """Une connexion réussie réinitialise le compteur d'échecs de l'IP."""
    from app import web

    for _ in range(3):
        client.post("/login", data={"email": "admin@test.local", "password": "faux"})
    assert web._login_failures.get("127.0.0.1")  # des échecs ont bien été comptés
    client.post(
        "/login",
        data={"email": TestConfig.ADMIN_EMAIL, "password": TestConfig.ADMIN_PASSWORD},
    )
    assert "127.0.0.1" not in web._login_failures


# --------------------------------------------------------------------------
# Fiche poste : la zone de travail admin est visible pour admin ET superadmin
# (non-régression : superadmin doit hériter des pouvoirs admin côté UI)
# --------------------------------------------------------------------------
def test_agent_detail_workzone_visible_for_superadmin(client, admin_session):
    """Le superadmin voit le bureau à distance / terminal / commande sur la fiche poste."""
    agent_id, _ = _enroll(client, "MACHINE-WZ")
    html = admin_session.get(f"/agents/{agent_id}").get_data(as_text=True)
    for marker in ("remote-start", "terminal-open", "cmd-run", "qa-lock", "workzone",
                   "script-groups", "proc-load", "tab-processes", "tab-activity", "act-feed", "act-filter"):
        assert marker in html, f"marqueur zone de travail manquant : {marker}"


def test_agent_detail_workzone_hidden_for_viewer(client, admin_session):
    """Un viewer (lecture seule) ne voit PAS la zone de travail admin."""
    agent_id, _ = _enroll(client, "MACHINE-WZ2")
    admin_session.post(
        "/api/v1/users",
        json={"email": "vv@medicofi.fr", "password": "viewerpass1", "role": "viewer"},
    )
    admin_session.get("/logout")
    admin_session.post("/login", data={"email": "vv@medicofi.fr", "password": "viewerpass1"})
    html = admin_session.get(f"/agents/{agent_id}").get_data(as_text=True)
    assert "remote-start" not in html
    assert "terminal-open" not in html


# --------------------------------------------------------------------------
# Heartbeat : rafraîchit les métadonnées du poste (os_version, agent_version)
# --------------------------------------------------------------------------
def test_heartbeat_refreshes_metadata(client, admin_session):
    """Un heartbeat portant os_version/agent_version met à jour la fiche poste
    sans ré-enrôlement (ex. correction Windows 10 → 11)."""
    agent_id, token = _enroll(client, "MACHINE-OSREFRESH")

    r = client.post(
        f"/api/v1/agents/{agent_id}/heartbeat",
        json={
            "metrics": {"cpu_pct": 5, "ram_used_pct": 40},
            "os_version": "Windows 11 Professional 26220",
            "agent_version": "1.2.3",
        },
        headers=_auth(token),
    )
    assert r.status_code == 200

    detail = admin_session.get(f"/api/v1/agents/{agent_id}").get_json()
    assert detail["os_version"] == "Windows 11 Professional 26220"
    assert detail["agent_version"] == "1.2.3"


# --------------------------------------------------------------------------
# Bibliothèque de scripts 1-clic (GET /api/v1/scripts)
# --------------------------------------------------------------------------
def test_scripts_catalog_admin(client, admin_session):
    """Le catalogue de scripts est servi à l'admin avec la structure attendue."""
    resp = admin_session.get("/api/v1/scripts")
    assert resp.status_code == 200
    rows = resp.get_json()
    assert len(rows) >= 10
    sample = rows[0]
    for key in ("key", "label", "category", "shell", "command_text", "danger", "timeout"):
        assert key in sample
    # Tous les shells sont valides et les clés uniques.
    assert all(r["shell"] in ("powershell", "cmd") for r in rows)
    assert len({r["key"] for r in rows}) == len(rows)


def test_scripts_catalog_requires_admin(client):
    """Sans session admin, le catalogue est refusé (401 sans session)."""
    resp = client.get("/api/v1/scripts", headers={"Accept": "application/json"})
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Étiquettes (POST /api/v1/agents/<id>/tags)
# --------------------------------------------------------------------------
def test_set_agent_tags_normalizes(client, admin_session):
    """Les étiquettes sont normalisées (trim, dédoublonnage insensible à la casse)."""
    agent_id, _ = _enroll(client, "MACHINE-TAGS")
    r = admin_session.post(
        f"/api/v1/agents/{agent_id}/tags",
        json={"tags": ["  Compta ", "compta", "Accueil", ""]},
    )
    assert r.status_code == 200
    assert r.get_json()["tags"] == ["Compta", "Accueil"]

    agents = admin_session.get("/api/v1/agents").get_json()
    a = next(x for x in agents if x["id"] == agent_id)
    assert a["tags"] == ["Compta", "Accueil"]


# --------------------------------------------------------------------------
# Actions groupées (POST /api/v1/agents/bulk)
# --------------------------------------------------------------------------
def test_bulk_quick_action(client, admin_session):
    """Une action rapide groupée crée une commande par poste."""
    a1, t1 = _enroll(client, "MACHINE-BULK-1")
    a2, t2 = _enroll(client, "MACHINE-BULK-2")
    r = admin_session.post(
        "/api/v1/agents/bulk",
        json={"agent_ids": [a1, a2], "kind": "quick", "action": "lock"},
    )
    assert r.status_code == 201, r.get_data(as_text=True)
    assert r.get_json()["count"] == 2

    cmds1 = client.get(f"/api/v1/agents/{a1}/commands", headers=_auth(t1)).get_json()["commands"]
    assert len(cmds1) == 1 and cmds1[0]["shell"] == "cmd"


def test_bulk_command_and_validation(client, admin_session):
    """Validation du bulk + création d'une commande groupée."""
    a1, _ = _enroll(client, "MACHINE-BULK-3")
    assert admin_session.post(
        "/api/v1/agents/bulk", json={"agent_ids": [], "kind": "quick", "action": "lock"}
    ).status_code == 400
    assert admin_session.post(
        "/api/v1/agents/bulk", json={"agent_ids": [a1], "kind": "bogus"}
    ).status_code == 400
    assert admin_session.post(
        "/api/v1/agents/bulk",
        json={"agent_ids": [a1], "kind": "command", "shell": "bash", "command_text": "x"},
    ).status_code == 400
    ok = admin_session.post(
        "/api/v1/agents/bulk",
        json={"agent_ids": [a1], "kind": "command", "shell": "powershell", "command_text": "Get-Date"},
    )
    assert ok.status_code == 201 and ok.get_json()["count"] == 1


def test_bulk_requires_admin(client):
    """Le bulk sans session est refusé (401)."""
    assert client.post(
        "/api/v1/agents/bulk",
        json={"agent_ids": ["x"], "kind": "quick", "action": "lock"},
        headers={"Accept": "application/json"},
    ).status_code == 401


# ==========================================================================
# Déploiement & mises à jour : releases, auto-update, liens d'installation
# ==========================================================================
import io  # noqa: E402
import zipfile  # noqa: E402


def _make_agent_zip(version="1.1.0"):
    """Construit un faux paquet onedir en mémoire (version.txt + exe factice)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("truesight-agent/version.txt", version)
        zf.writestr("truesight-agent/truesight-agent.exe", b"MZ-fake-exe")
        zf.writestr("truesight-agent/_internal/base_library.zip", b"x")
    buf.seek(0)
    return buf


def _new_session(app, email, password):
    """Ouvre une session fraîche pour un compte donné (client dédié)."""
    c = app.test_client()
    r = c.post("/login", data={"email": email, "password": password})
    assert r.status_code in (302, 303), r.get_data(as_text=True)
    return c


def _publish_release(admin_session, version="1.1.0", make_current=True):
    return admin_session.post(
        "/api/v1/agent-releases",
        data={
            "file": (_make_agent_zip(version), f"truesight-agent-{version}.zip"),
            "make_current": "true" if make_current else "false",
            "notes": "test",
        },
        content_type="multipart/form-data",
    )


def test_version_compare():
    """Comparaison de versions : logique d'annonce d'auto-update."""
    from app.releases import parse_version, version_gt

    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("nope") is None
    assert version_gt("1.1.0", "1.0.9") is True
    assert version_gt("1.0.0", "1.0.0") is False
    assert version_gt("1.0.0", None) is True       # agent inconnu → MAJ
    assert version_gt("bad", "1.0.0") is False      # release douteuse → pas de MAJ


def test_publish_release_and_current(app, admin_session, tmp_path):
    """Le superadmin publie un paquet ; il devient la version courante."""
    app.config["AGENT_RELEASE_DIR"] = str(tmp_path)
    r = _publish_release(admin_session, "1.1.0")
    assert r.status_code == 201, r.get_data(as_text=True)
    assert r.get_json()["version"] == "1.1.0" and r.get_json()["is_current"] is True

    rows = admin_session.get("/api/v1/agent-releases").get_json()
    assert len(rows) == 1 and rows[0]["is_current"] is True
    # Le fichier a bien été écrit sur le disque.
    assert (tmp_path / "truesight-agent-1.1.0.zip").is_file()


def test_publish_requires_superadmin(app, client, admin_session, tmp_path):
    """Un admin (non superadmin) ne peut PAS publier de paquet (403) mais peut lister."""
    app.config["AGENT_RELEASE_DIR"] = str(tmp_path)
    admin_session.post(
        "/api/v1/users",
        json={"email": "adm@medicofi.fr", "password": "adminpass1", "role": "admin"},
    )
    adm = _new_session(app, "adm@medicofi.fr", "adminpass1")
    assert adm.get("/api/v1/agent-releases").status_code == 200  # lecture OK (admin)
    r = adm.post(
        "/api/v1/agent-releases",
        data={"file": (_make_agent_zip(), "x.zip")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 403


def test_heartbeat_advertises_update(app, client, admin_session, tmp_path):
    """Le heartbeat annonce agent_update pour un agent plus ancien, pas pour un à jour."""
    app.config["AGENT_RELEASE_DIR"] = str(tmp_path)
    _publish_release(admin_session, "1.1.0")

    agent_id, token = _enroll(client, "MACHINE-UPD")  # agent_version 1.0.0
    r = client.post(
        f"/api/v1/agents/{agent_id}/heartbeat",
        json={"metrics": {"cpu_pct": 1}, "agent_version": "1.0.0"},
        headers=_auth(token),
    )
    upd = r.get_json()["agent_update"]
    assert upd and upd["version"] == "1.1.0"
    assert upd["url"].endswith(f"/api/v1/agents/{agent_id}/package")
    assert len(upd["sha256"]) == 64

    # Agent déjà à jour → pas d'annonce.
    r2 = client.post(
        f"/api/v1/agents/{agent_id}/heartbeat",
        json={"metrics": {"cpu_pct": 1}, "agent_version": "1.1.0"},
        headers=_auth(token),
    )
    assert r2.get_json()["agent_update"] is None


def test_auto_update_can_be_disabled(app, client, admin_session, tmp_path):
    """AGENT_AUTO_UPDATE_ENABLED=False gèle le parc (jamais d'annonce)."""
    app.config["AGENT_RELEASE_DIR"] = str(tmp_path)
    app.config["AGENT_AUTO_UPDATE_ENABLED"] = False
    _publish_release(admin_session, "1.1.0")
    agent_id, token = _enroll(client, "MACHINE-NOUPD")
    r = client.post(
        f"/api/v1/agents/{agent_id}/heartbeat",
        json={"metrics": {"cpu_pct": 1}, "agent_version": "1.0.0"},
        headers=_auth(token),
    )
    assert r.get_json()["agent_update"] is None


def test_agent_package_download(app, client, admin_session, tmp_path):
    """L'agent télécharge le paquet courant avec son Bearer."""
    app.config["AGENT_RELEASE_DIR"] = str(tmp_path)
    _publish_release(admin_session, "1.1.0")
    agent_id, token = _enroll(client, "MACHINE-PKG")
    r = client.get(f"/api/v1/agents/{agent_id}/package", headers=_auth(token))
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    assert len(r.get_data()) > 0
    # Sans Bearer : refusé.
    assert client.get(f"/api/v1/agents/{agent_id}/package").status_code == 401


def test_install_token_and_bootstrap(app, client, admin_session, tmp_path):
    """Génération d'un lien d'installation + script bootstrap + config gardée par jeton."""
    app.config["AGENT_RELEASE_DIR"] = str(tmp_path)
    _publish_release(admin_session, "1.1.0")

    r = admin_session.post("/api/v1/install-tokens", json={"label": "compta", "ttl_days": 7})
    assert r.status_code == 201, r.get_data(as_text=True)
    data = r.get_json()
    token = data["token"]
    assert "iwr" in data["one_liner"] and token in data["one_liner"]

    # Le script bootstrap embarque le jeton et le service.
    ps = client.get(f"/install.ps1?t={token}")
    assert ps.status_code == 200
    body = ps.get_data(as_text=True)
    assert token in body and "TrueSightAgent" in body

    # Le config.ini sert l'URL serveur + l'enrollment_token.
    cfg = client.get(f"/api/v1/install/{token}/config")
    assert cfg.status_code == 200
    cfg_text = cfg.get_data(as_text=True)
    assert "enrollment_token = test-enrollment-token" in cfg_text
    assert "[server]" in cfg_text and "verify_tls = true" in cfg_text

    # Le paquet est servi contre le jeton d'installation.
    pkg = client.get(f"/api/v1/install/{token}/package")
    assert pkg.status_code == 200 and pkg.mimetype == "application/zip"

    # Le compteur d'usage a été incrémenté par la récupération de config.
    tokens = admin_session.get("/api/v1/install-tokens").get_json()
    assert tokens[0]["use_count"] >= 1


def test_install_token_invalid_and_revoke(app, client, admin_session, tmp_path):
    """Jeton invalide → script d'erreur clair / 403 ; révocation effective."""
    app.config["AGENT_RELEASE_DIR"] = str(tmp_path)
    # Jeton bidon : script renvoyé (200) mais inactif, et endpoints gardés en 403.
    bad = client.get("/install.ps1?t=nope")
    assert bad.status_code == 200 and "invalide" in bad.get_data(as_text=True)
    assert client.get("/api/v1/install/nope/config").status_code == 403

    r = admin_session.post("/api/v1/install-tokens", json={})
    token = r.get_json()["token"]
    tid = r.get_json()["id"]
    assert client.get(f"/api/v1/install/{token}/config").status_code == 200

    assert admin_session.delete(f"/api/v1/install-tokens/{tid}").status_code == 200
    assert client.get(f"/api/v1/install/{token}/config").status_code == 403
    assert "invalide" in client.get(f"/install.ps1?t={token}").get_data(as_text=True)


def test_install_token_permissions(app, client, admin_session):
    """Création de lien : admin OK, viewer refusé (403)."""
    admin_session.post(
        "/api/v1/users",
        json={"email": "adm2@medicofi.fr", "password": "adminpass1", "role": "admin"},
    )
    admin_session.post(
        "/api/v1/users",
        json={"email": "vw2@medicofi.fr", "password": "viewerpass1", "role": "viewer"},
    )
    adm = _new_session(app, "adm2@medicofi.fr", "adminpass1")
    vw = _new_session(app, "vw2@medicofi.fr", "viewerpass1")

    assert adm.post("/api/v1/install-tokens", json={}).status_code == 201
    assert vw.post("/api/v1/install-tokens", json={}).status_code == 403
    assert vw.get("/api/v1/agent-releases", headers={"Accept": "application/json"}).status_code == 403


def test_enrollment_token_superadmin_only(app, client, admin_session):
    """Le jeton d'enrôlement n'est lisible que par le super-administrateur."""
    r = admin_session.get("/api/v1/enrollment-token")
    assert r.status_code == 200
    assert r.get_json()["token"] == TestConfig.ENROLLMENT_TOKEN

    admin_session.post(
        "/api/v1/users",
        json={"email": "adm3@medicofi.fr", "password": "adminpass1", "role": "admin"},
    )
    adm = _new_session(app, "adm3@medicofi.fr", "adminpass1")
    assert adm.get("/api/v1/enrollment-token", headers={"Accept": "application/json"}).status_code == 403


# ==========================================================================
# Socle : emplacements (sites), nom convivial, santé, vue d'ensemble
# ==========================================================================
def test_sites_crud_and_assign(client, admin_session):
    """Création/affectation/renommage/suppression d'emplacement + filtre ?site."""
    a, _ = _enroll(client, "MACHINE-SITE-1")
    r = admin_session.post("/api/v1/sites", json={"name": "Madagascar", "color": "#34e2b0"})
    assert r.status_code == 201, r.get_data(as_text=True)
    sid = r.get_json()["id"]
    # Nom en double (insensible à la casse) → 409.
    assert admin_session.post("/api/v1/sites", json={"name": "madagascar"}).status_code == 409

    assert admin_session.post(f"/api/v1/agents/{a}/site", json={"site_id": sid}).status_code == 200
    sites = admin_session.get("/api/v1/sites").get_json()
    mada = next(s for s in sites if s["id"] == sid)
    assert mada["total"] == 1

    ag = admin_session.get(f"/api/v1/agents/{a}").get_json()
    assert ag["site_id"] == sid and ag["site_name"] == "Madagascar"

    flt = admin_session.get(f"/api/v1/agents?site={sid}").get_json()
    assert len(flt) == 1 and flt[0]["id"] == a
    assert admin_session.get("/api/v1/agents?site=none").get_json() == []

    assert admin_session.patch(f"/api/v1/sites/{sid}", json={"name": "Mada"}).status_code == 200
    assert admin_session.delete(f"/api/v1/sites/{sid}").status_code == 200
    # L'agent redevient non assigné après suppression du site.
    assert admin_session.get(f"/api/v1/agents/{a}").get_json()["site_id"] is None


def test_agent_rename(client, admin_session):
    """Nom convivial : défini puis effacé (retour au hostname)."""
    a, _ = _enroll(client, "MACHINE-NAME-1")
    r = admin_session.post(f"/api/v1/agents/{a}/name", json={"name": "Accueil-PC1"})
    assert r.status_code == 200 and r.get_json()["name"] == "Accueil-PC1"
    ag = admin_session.get(f"/api/v1/agents/{a}").get_json()
    assert ag["display_name"] == "Accueil-PC1" and ag["name"] == "Accueil-PC1"

    admin_session.post(f"/api/v1/agents/{a}/name", json={"name": ""})
    ag = admin_session.get(f"/api/v1/agents/{a}").get_json()
    assert ag["display_name"] is None and ag["name"] == ag["hostname"]


def test_bulk_site_assign(client, admin_session):
    """Affectation groupée à un emplacement + désaffectation."""
    a1, _ = _enroll(client, "M-BS-1")
    a2, _ = _enroll(client, "M-BS-2")
    sid = admin_session.post("/api/v1/sites", json={"name": "Tunisie"}).get_json()["id"]
    r = admin_session.post("/api/v1/agents/bulk-site", json={"agent_ids": [a1, a2], "site_id": sid})
    assert r.status_code == 200 and r.get_json()["count"] == 2
    assert admin_session.post(
        "/api/v1/agents/bulk-site", json={"agent_ids": [a1], "site_id": None}
    ).status_code == 200


def test_delete_agent(client, admin_session):
    """Suppression d'un poste : 404 ensuite + absent de la liste."""
    a, t = _enroll(client, "MACHINE-DEL")
    client.post(f"/api/v1/agents/{a}/heartbeat", json={"metrics": {"cpu_pct": 5}}, headers=_auth(t))
    assert admin_session.delete(f"/api/v1/agents/{a}").status_code == 200
    assert admin_session.get(f"/api/v1/agents/{a}").status_code == 404
    assert all(x["id"] != a for x in admin_session.get("/api/v1/agents").get_json())


def test_overview_and_health(client, admin_session):
    """Vue d'ensemble : un poste hors ligne = inconnu ; après heartbeat = sain."""
    a, t = _enroll(client, "MACHINE-OV")
    ov = admin_session.get("/api/v1/overview").get_json()
    assert ov["total"] == 1 and ov["health"]["unknown"] == 1

    client.post(
        f"/api/v1/agents/{a}/heartbeat",
        json={"metrics": {"cpu_pct": 5, "ram_used_pct": 30}}, headers=_auth(t),
    )
    ov = admin_session.get("/api/v1/overview").get_json()
    assert ov["online"] == 1 and ov["health"]["healthy"] == 1 and ov["healthy_pct"] == 100.0
    for key in ("problems", "sites", "active_alerts", "updates_available"):
        assert key in ov

    ag = admin_session.get("/api/v1/agents").get_json()[0]
    assert ag["health"] == "healthy" and "name" in ag


def test_socle_permissions(app, client, admin_session):
    """Lecture pour tous, mutations réservées aux admins."""
    a, _ = _enroll(client, "M-PERM")
    admin_session.post(
        "/api/v1/users",
        json={"email": "vw3@medicofi.fr", "password": "viewerpass1", "role": "viewer"},
    )
    vw = _new_session(app, "vw3@medicofi.fr", "viewerpass1")
    assert vw.get("/api/v1/overview").status_code == 200
    assert vw.get("/api/v1/sites").status_code == 200
    assert vw.post("/api/v1/sites", json={"name": "X"}).status_code == 403
    assert vw.post(f"/api/v1/agents/{a}/name", json={"name": "x"}).status_code == 403
    assert vw.delete(f"/api/v1/agents/{a}").status_code == 403


def test_enroll_with_site_assigns(client, admin_session):
    """Un poste enrôlé avec un ``site`` rejoint cet emplacement (find-or-create)."""
    resp = client.post("/api/v1/enroll", json={
        "enrollment_token": TestConfig.ENROLLMENT_TOKEN,
        "machine_id": "MACHINE-SITE-ENROLL",
        "hostname": "PC", "os_version": "Windows 11", "agent_version": "1.0.0",
        "site": "Tunisie",
    })
    assert resp.status_code == 200
    aid = resp.get_json()["agent_id"]
    assert admin_session.get(f"/api/v1/agents/{aid}").get_json()["site_name"] == "Tunisie"


def test_install_token_with_site(client, admin_session):
    """Un lien d'installation lié à un site écrit ``site = ...`` dans config.ini."""
    sid = admin_session.post("/api/v1/sites", json={"name": "Direction"}).get_json()["id"]
    r = admin_session.post("/api/v1/install-tokens", json={"site_id": sid})
    assert r.status_code == 201 and r.get_json()["site_name"] == "Direction"
    token = r.get_json()["token"]
    cfg = client.get(f"/api/v1/install/{token}/config").get_data(as_text=True)
    assert "site = Direction" in cfg
    toks = admin_session.get("/api/v1/install-tokens").get_json()
    assert any(t.get("site_name") == "Direction" for t in toks)


def test_security_collection_and_health(client, admin_session):
    """Inventaire avec sécurité : stocké, reflété dans la santé + la vue d'ensemble."""
    a, t = _enroll(client, "MACHINE-SEC")
    client.post(
        f"/api/v1/agents/{a}/heartbeat",
        json={"metrics": {"cpu_pct": 3, "ram_used_pct": 20}}, headers=_auth(t),
    )
    r = client.post(
        f"/api/v1/agents/{a}/inventory",
        json={
            "hardware": {}, "software": [],
            "security": {
                "defender": {"enabled": False, "realtime": False},
                "windows_update": {"pending_count": 5, "pending_critical": 2},
            },
        },
        headers=_auth(t),
    )
    assert r.status_code == 200

    ag = admin_session.get(f"/api/v1/agents/{a}").get_json()
    assert ag["security"]["pending_updates"] == 5 and ag["security"]["defender_enabled"] is False
    assert ag["health"] == "warning"
    assert any("antivirus" in x.lower() for x in ag["health_reasons"])

    ov = admin_session.get("/api/v1/overview").get_json()
    probs = {p["type"]: p["count"] for p in ov["problems"]}
    assert probs.get("updates") == 1 and probs.get("defender") == 1

    assert len(admin_session.get("/api/v1/agents?security=updates").get_json()) == 1
    assert len(admin_session.get("/api/v1/agents?security=defender").get_json()) == 1


def test_export_agents_csv(client, admin_session):
    """Export CSV du parc : en-tête + ligne du poste, type text/csv."""
    _enroll(client, "MACHINE-CSV")
    r = admin_session.get("/api/v1/agents/export.csv")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert "attachment" in r.headers.get("Content-Disposition", "")
    body = r.get_data(as_text=True)
    assert "Nom;Hote;Emplacement" in body
    assert "PC-TEST-01" in body


# ==========================================================================
# Déploiement logiciel : installation / désinstallation silencieuse
# ==========================================================================
def _last_command_text(client, agent_id, token):
    """L'agent récupère sa commande en attente et renvoie son command_text."""
    cmds = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token)).get_json()["commands"]
    assert len(cmds) == 1, cmds
    return cmds[0]["command_text"], cmds[0]["shell"]


def test_software_catalog(admin_session):
    """Le catalogue d'apps est servi à l'admin avec la structure attendue."""
    rows = admin_session.get("/api/v1/software/catalog").get_json()
    assert len(rows) >= 8
    for key in ("key", "label", "category", "winget_id"):
        assert key in rows[0]
    assert len({r["key"] for r in rows}) == len(rows)


def test_software_install_from_catalog(client, admin_session):
    """Installer depuis le catalogue crée une commande PowerShell winget pour le poste."""
    agent_id, token = _enroll(client, "MACHINE-SW-CAT")
    r = admin_session.post(
        f"/api/v1/agents/{agent_id}/software/install",
        json={"source": "catalog", "key": "chrome"},
    )
    assert r.status_code == 201, r.get_data(as_text=True)
    text, shell = _last_command_text(client, agent_id, token)
    assert shell == "powershell"
    assert "Google.Chrome" in text and "winget" in text.lower() and "--silent" in text


def test_software_install_winget_validation(client, admin_session):
    """ID winget : valide -> 201 ; injection -> 400 (refusé par le validateur)."""
    agent_id, token = _enroll(client, "MACHINE-SW-WID")
    ok = admin_session.post(
        f"/api/v1/agents/{agent_id}/software/install",
        json={"source": "winget", "winget_id": "VideoLAN.VLC"},
    )
    assert ok.status_code == 201
    bad = admin_session.post(
        f"/api/v1/agents/{agent_id}/software/install",
        json={"source": "winget", "winget_id": "VLC'; Remove-Item C:\\ -Recurse"},
    )
    assert bad.status_code == 400
    # Catalogue inconnu -> 400.
    assert admin_session.post(
        f"/api/v1/agents/{agent_id}/software/install",
        json={"source": "catalog", "key": "inexistant"},
    ).status_code == 400


def test_software_install_url_requires_https(client, admin_session):
    """URL : HTTPS .msi accepté ; HTTP refusé. MSI -> msiexec /qn."""
    agent_id, token = _enroll(client, "MACHINE-SW-URL")
    ok = admin_session.post(
        f"/api/v1/agents/{agent_id}/software/install",
        json={"source": "url", "url": "https://example.com/setup.msi"},
    )
    assert ok.status_code == 201
    text, _ = _last_command_text(client, agent_id, token)
    assert "msiexec.exe" in text and "/qn" in text
    assert admin_session.post(
        f"/api/v1/agents/{agent_id}/software/install",
        json={"source": "url", "url": "http://example.com/setup.exe"},
    ).status_code == 400


def test_software_uninstall_registry(client, admin_session):
    """Désinstallation par nom : le nom est injecté en littéral PS échappé (anti-injection)."""
    agent_id, token = _enroll(client, "MACHINE-SW-UNINST")
    r = admin_session.post(
        f"/api/v1/agents/{agent_id}/software/uninstall",
        json={"source": "registry", "name": "7-Zip 23.01 (x64)"},
    )
    assert r.status_code == 201
    text, shell = _last_command_text(client, agent_id, token)
    assert shell == "powershell"
    assert "QuietUninstallString" in text and "7-Zip 23.01 (x64)" in text
    # Nom manquant -> 400.
    assert admin_session.post(
        f"/api/v1/agents/{agent_id}/software/uninstall",
        json={"source": "registry", "name": "  "},
    ).status_code == 400


def test_software_uninstall_quote_escaping(client, admin_session):
    """Une apostrophe dans le nom est doublée (échappement littéral PowerShell)."""
    agent_id, token = _enroll(client, "MACHINE-SW-QUOTE")
    r = admin_session.post(
        f"/api/v1/agents/{agent_id}/software/uninstall",
        json={"source": "registry", "name": "L'éditeur"},
    )
    assert r.status_code == 201
    text, _ = _last_command_text(client, agent_id, token)
    # La quote simple doit être doublée -> aucune évasion possible de la chaîne.
    assert "L''" in text


def test_software_bulk_install(client, admin_session):
    """Installation groupée : une commande par poste."""
    a1, t1 = _enroll(client, "MACHINE-SW-B1")
    a2, t2 = _enroll(client, "MACHINE-SW-B2")
    r = admin_session.post(
        "/api/v1/software/bulk-install",
        json={"agent_ids": [a1, a2], "source": "catalog", "key": "7zip"},
    )
    assert r.status_code == 201 and r.get_json()["count"] == 2
    text1, _ = _last_command_text(client, a1, t1)
    assert "7zip.7zip" in text1
    # Spec invalide -> 400 global (aucune commande créée).
    assert admin_session.post(
        "/api/v1/software/bulk-install",
        json={"agent_ids": [a1], "source": "winget", "winget_id": "bad id with spaces"},
    ).status_code == 400


def test_software_requires_admin(client, app, admin_session):
    """Install/uninstall réservés aux admins (401 sans session, 403 pour un viewer)."""
    agent_id, _ = _enroll(client, "MACHINE-SW-PERM")
    # Sans session : client frais non authentifié (le fixture admin_session
    # connecte le client partagé, d'où un client dédié pour ce contrôle).
    fresh = app.test_client()
    assert fresh.post(
        f"/api/v1/agents/{agent_id}/software/install",
        json={"source": "catalog", "key": "chrome"}, headers={"Accept": "application/json"},
    ).status_code == 401
    assert fresh.get("/api/v1/software/catalog", headers={"Accept": "application/json"}).status_code == 401
    # Viewer connecté -> 403.
    admin_session.post(
        "/api/v1/users",
        json={"email": "vwsw@medicofi.fr", "password": "viewerpass1", "role": "viewer"},
    )
    vw = _new_session(app, "vwsw@medicofi.fr", "viewerpass1")
    assert vw.post(
        f"/api/v1/agents/{agent_id}/software/install",
        json={"source": "catalog", "key": "chrome"},
    ).status_code == 403
    assert vw.get("/api/v1/software/catalog").status_code == 403


# ==========================================================================
# Comptes utilisateurs locaux : list / create / delete
# ==========================================================================
def _fetch_command_text(client, agent_id, token):
    """L'agent récupère sa commande en attente et renvoie son command_text."""
    cmds = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token)).get_json()["commands"]
    assert len(cmds) == 1, cmds
    return cmds[0]["command_text"]


def test_accounts_list(client, admin_session):
    """Lister les comptes : crée une commande PowerShell Get-LocalUser."""
    aid, token = _enroll(client, "MACHINE-ACCT-LIST")
    r = admin_session.post(f"/api/v1/agents/{aid}/accounts/list", json={})
    assert r.status_code == 201, r.get_data(as_text=True)
    text = _fetch_command_text(client, aid, token)
    assert "Get-LocalUser" in text and "ConvertTo-Json" in text


def test_accounts_create_and_audit_excludes_password(client, admin_session):
    """Créer un compte admin : commande New-LocalUser + ajout au groupe ;
    le mot de passe figure dans la commande mais JAMAIS dans l'audit."""
    aid, token = _enroll(client, "MACHINE-ACCT-CREATE")
    secret = "Sup3rSecretX!"
    r = admin_session.post(
        f"/api/v1/agents/{aid}/accounts/create",
        json={"username": "techuser", "password": secret, "full_name": "Technicien", "administrator": True},
    )
    assert r.status_code == 201, r.get_data(as_text=True)
    text = _fetch_command_text(client, aid, token)
    assert "New-LocalUser" in text and "techuser" in text
    assert secret in text  # nécessaire à l'exécution sur le poste
    assert "Add-LocalGroupMember" in text  # rôle administrateur

    audit = admin_session.get("/api/v1/audit?limit=50").get_json()
    entry = next((a for a in audit if a["action"] == "account.create"), None)
    assert entry is not None
    assert entry["details"]["username"] == "techuser"
    assert entry["details"]["administrator"] is True
    assert "password" not in entry["details"]
    assert secret not in str(entry["details"])  # le secret n'est jamais journalisé


def test_accounts_create_validation(client, admin_session):
    """Nom d'utilisateur invalide (injection) et mot de passe trop court → 400."""
    aid, _ = _enroll(client, "MACHINE-ACCT-VALID")
    assert admin_session.post(
        f"/api/v1/agents/{aid}/accounts/create",
        json={"username": "bad'; rm", "password": "abcd"},
    ).status_code == 400
    assert admin_session.post(
        f"/api/v1/agents/{aid}/accounts/create",
        json={"username": "okuser", "password": "x"},
    ).status_code == 400


def test_accounts_create_quote_escaping(client, admin_session):
    """Une apostrophe dans le mot de passe est doublée (littéral PowerShell sûr)."""
    aid, token = _enroll(client, "MACHINE-ACCT-QUOTE")
    r = admin_session.post(
        f"/api/v1/agents/{aid}/accounts/create",
        json={"username": "user1", "password": "Secret'1!"},
    )
    assert r.status_code == 201
    text = _fetch_command_text(client, aid, token)
    assert "Secret''1!" in text  # quote doublée → pas d'évasion possible


def test_accounts_delete(client, admin_session):
    """Supprimer un compte (+ profil) ; nom invalide → 400."""
    aid, token = _enroll(client, "MACHINE-ACCT-DEL")
    r = admin_session.post(
        f"/api/v1/agents/{aid}/accounts/delete",
        json={"username": "olduser", "remove_profile": True},
    )
    assert r.status_code == 201
    text = _fetch_command_text(client, aid, token)
    assert "Remove-LocalUser" in text and "olduser" in text
    assert "Win32_UserProfile" in text  # suppression du profil demandée
    assert admin_session.post(
        f"/api/v1/agents/{aid}/accounts/delete",
        json={"username": "bad name!"},
    ).status_code == 400


def test_accounts_require_admin(client):
    """Sans session, les endpoints comptes sont refusés (401)."""
    aid, _ = _enroll(client, "MACHINE-ACCT-ACL")
    assert client.post(
        f"/api/v1/agents/{aid}/accounts/list", json={}, headers={"Accept": "application/json"}
    ).status_code == 401


def test_accounts_create_password_scrubbed_after_run(client, admin_session):
    """Le mot de passe (présent dans la commande pour l'exécution) est purgé du
    command_text une fois la commande exécutée par l'agent."""
    aid, token = _enroll(client, "MACHINE-ACCT-SCRUB")
    secret = "Sup3rSecretZ!"
    cid = admin_session.post(
        f"/api/v1/agents/{aid}/accounts/create",
        json={"username": "tmpz", "password": secret},
    ).get_json()["command_id"]

    # L'agent récupère la commande : le mot de passe y est (nécessaire à l'exécution).
    assert secret in _fetch_command_text(client, aid, token)

    # L'agent remonte le résultat → purge du command_text.
    res = client.post(
        f"/api/v1/commands/{cid}/result",
        json={"exit_code": 0, "stdout": "Compte cree: tmpz", "stderr": "", "duration_seconds": 1.0},
        headers=_auth(token),
    )
    assert res.status_code == 200

    after = admin_session.get(f"/api/v1/commands/{cid}").get_json()
    assert secret not in (after.get("command_text") or "")
    assert "purg" in (after.get("command_text") or "").lower()


def test_install_cmd_download(client, admin_session):
    """Un lien d'installation expose un installeur .cmd double-cliquable (auto-élévation)."""
    r = admin_session.post("/api/v1/install-tokens", json={"label": "test-cmd"})
    assert r.status_code == 201, r.get_data(as_text=True)
    body = r.get_json()
    token = body["token"]
    assert body.get("installer_cmd_url", "").endswith("/install/" + token + "/installer.cmd")

    # Téléchargement (sans session, gardé par le jeton).
    dl = client.get(f"/api/v1/install/{token}/installer.cmd")
    assert dl.status_code == 200
    cd = dl.headers.get("Content-Disposition", "")
    assert "attachment" in cd and "TrueSight-Installer.cmd" in cd
    text = dl.get_data(as_text=True)
    assert ("/install.ps1?t=" + token) in text  # bootstrap avec le jeton
    assert "RunAs" in text                       # auto-élévation UAC

    # Jeton inexistant → 403.
    assert client.get("/api/v1/install/inexistant/installer.cmd").status_code == 403


# --------------------------------------------------------------------------
# Lien d'installation : ré-afficher la commande d'un lien actif
# --------------------------------------------------------------------------
def test_install_token_show_command(client, admin_session):
    """La commande d'un lien actif est ré-affichable (one-liner + .cmd) et auditée."""
    created = admin_session.post("/api/v1/install-tokens", json={"label": "revoir"}).get_json()
    tid, token = created["id"], created["token"]

    # La liste signale que la commande est ré-affichable.
    rows = admin_session.get("/api/v1/install-tokens").get_json()
    row = [r for r in rows if r["id"] == tid][0]
    assert row["can_show_command"] is True

    # Ré-affichage : le one-liner contient le jeton en clair.
    cmd = admin_session.get(f"/api/v1/install-tokens/{tid}/command")
    assert cmd.status_code == 200, cmd.get_data(as_text=True)
    body = cmd.get_json()
    assert token in body["one_liner"]
    assert body["installer_cmd_url"].endswith(f"/install/{token}/installer.cmd")

    # Audit de la lecture sensible.
    audit = admin_session.get("/api/v1/audit?limit=50").get_json()
    assert "install.token.view" in [e["action"] for e in audit]


def test_install_token_command_gone_after_revoke(client, admin_session):
    """Après révocation : la commande n'est plus disponible (409) et le jeton clair est purgé."""
    created = admin_session.post("/api/v1/install-tokens", json={"label": "r2"}).get_json()
    tid = created["id"]
    assert admin_session.delete(f"/api/v1/install-tokens/{tid}").status_code == 200

    assert admin_session.get(f"/api/v1/install-tokens/{tid}/command").status_code == 409
    rows = admin_session.get("/api/v1/install-tokens").get_json()
    row = [r for r in rows if r["id"] == tid][0]
    assert row["can_show_command"] is False


def test_install_token_command_requires_login(client):
    """Sans session, la commande n'est pas révélée."""
    assert client.get("/api/v1/install-tokens/00000000-0000-0000-0000-000000000000/command",
                      headers={"Accept": "application/json"}).status_code == 401


# --------------------------------------------------------------------------
# Préférences UI — ordre des onglets de la fiche poste
# --------------------------------------------------------------------------
def test_settings_preferences_default(client, admin_session):
    """GET /settings/preferences renvoie le catalogue d'onglets + un ordre vide par défaut."""
    resp = admin_session.get("/api/v1/settings/preferences")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["tab_order"] == []
    keys = [t["key"] for t in data["tabs"]]
    for k in ("remote", "terminal", "hardware", "patches"):
        assert k in keys


def test_settings_tab_order_requires_login(client):
    """Sans session, l'enregistrement de l'ordre est refusé."""
    assert client.post("/api/v1/settings/tab-order", json={"order": ["remote"]}).status_code == 401


def test_settings_tab_order_save_filters_and_persists(client, admin_session):
    """POST filtre les clés inconnues + doublons, et l'ordre persiste (GET le renvoie)."""
    resp = admin_session.post(
        "/api/v1/settings/tab-order",
        json={"order": ["hardware", "bogus", "remote", "hardware", "terminal"]},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["tab_order"] == ["hardware", "remote", "terminal"]

    # Persistance.
    again = admin_session.get("/api/v1/settings/preferences").get_json()
    assert again["tab_order"] == ["hardware", "remote", "terminal"]

    # Réinitialisation (liste vide).
    reset = admin_session.post("/api/v1/settings/tab-order", json={"order": []})
    assert reset.get_json()["tab_order"] == []


def test_settings_tab_order_invalid_type(client, admin_session):
    """Un champ order non-liste est rejeté (400)."""
    assert admin_session.post(
        "/api/v1/settings/tab-order", json={"order": "remote"}
    ).status_code == 400


def test_agent_detail_embeds_tab_order(client, admin_session):
    """L'ordre enregistré est injecté dans la fiche poste (data-tab-order)."""
    agent_id, _ = _enroll(client, "MACHINE-TABORDER")
    admin_session.post("/api/v1/settings/tab-order", json={"order": ["hardware", "remote"]})
    html = admin_session.get(f"/agents/{agent_id}").get_data(as_text=True)
    assert "data-tab-order=" in html
    assert "hardware" in html
