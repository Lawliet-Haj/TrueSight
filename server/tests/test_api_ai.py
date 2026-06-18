"""Tests du Copilote IA (client fournisseur mocké — aucun appel réseau).

Couvre : ACL (admin only), désactivation sans clé, réponse simple + audit,
minimisation des données par les outils, propositions structurées NON exécutées,
validation/whitelist, confirmation réutilisant un endpoint existant, boucle bornée,
et dégradation gracieuse sur erreur fournisseur.
"""
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app.ai import client as ai_client  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Command  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture()
def app():
    application = create_app(TestConfig)
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    from app import api_agent, web

    api_agent._enroll_hits.clear()
    web._login_failures.clear()
    yield


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_session(client):
    resp = client.post(
        "/login",
        data={"email": TestConfig.ADMIN_EMAIL, "password": TestConfig.ADMIN_PASSWORD},
    )
    assert resp.status_code in (302, 303)
    return client


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _enroll(client, machine_id="MACHINE-AI-001"):
    resp = client.post(
        "/api/v1/enroll",
        json={
            "enrollment_token": TestConfig.ENROLLMENT_TOKEN,
            "machine_id": machine_id,
            "hostname": "PC-AI-01",
            "os_version": "Windows 11 Pro 26100",
            "agent_version": "1.0.0",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    return data["agent_id"], data["agent_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _new_session(app, email, password):
    c = app.test_client()
    r = c.post("/login", data={"email": email, "password": password})
    assert r.status_code in (302, 303), r.get_data(as_text=True)
    return c


def _tc(name, args, call_id="call_1"):
    return ai_client.ToolCall(id=call_id, name=name, arguments=json.dumps(args))


def _result(text="", tool_calls=None):
    tool_calls = tool_calls or []
    raw_tcs = [
        {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
        for tc in tool_calls
    ]
    return ai_client.ChatResult(
        text=text,
        tool_calls=tool_calls,
        finish_reason="tool_calls" if tool_calls else "stop",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        raw_message={"role": "assistant", "content": text or None, "tool_calls": raw_tcs or None},
    )


def _enable_ai(app, monkeypatch, fake):
    """Active le Copilote (clé présente) et branche un client fournisseur factice."""
    app.config["OPENAI_API_KEY"] = "test-key"
    monkeypatch.setattr(ai_client, "create_chat", fake)


def _scripted(*results):
    """Renvoie un faux create_chat qui débite ``results`` (répète le dernier)."""
    state = {"n": 0}

    def fake(messages, tools=None, **kwargs):
        state["n"] += 1
        return results[min(state["n"] - 1, len(results) - 1)]

    fake.state = state
    return fake


# --------------------------------------------------------------------------
# ACL & activation
# --------------------------------------------------------------------------
def test_ai_chat_requires_admin(app, client, admin_session):
    # Sans session : client frais → 401.
    fresh = app.test_client()
    assert fresh.post("/api/v1/ai/chat", json={"message": "salut"},
                      headers={"Accept": "application/json"}).status_code == 401
    # Viewer connecté → 403.
    admin_session.post("/api/v1/users", json={"email": "vai@medicofi.fr", "password": "viewerpass1", "role": "viewer"})
    vw = _new_session(app, "vai@medicofi.fr", "viewerpass1")
    assert vw.post("/api/v1/ai/chat", json={"message": "salut"}).status_code == 403


def test_ai_disabled_without_key(admin_session):
    # TestConfig n'a pas de clé → réponse « désactivé », sans appel réseau.
    r = admin_session.post("/api/v1/ai/chat", json={"message": "bonjour"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["disabled"] is True
    assert data["proposals"] == []


def test_ai_message_required(app, admin_session, monkeypatch):
    _enable_ai(app, monkeypatch, _scripted(_result("ok")))
    assert admin_session.post("/api/v1/ai/chat", json={"message": "   "}).status_code == 400


# --------------------------------------------------------------------------
# Réponse simple + audit
# --------------------------------------------------------------------------
def test_ai_basic_reply_and_audit(app, admin_session, monkeypatch):
    _enable_ai(app, monkeypatch, _scripted(_result("Le poste va bien.")))
    r = admin_session.post("/api/v1/ai/chat", json={"message": "comment va ce poste ?"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["reply"] == "Le poste va bien."
    assert data["proposals"] == []
    assert "history" in data

    # Une ligne d'audit ai.query (métadonnées, pas le texte brut).
    audit = admin_session.get("/api/v1/audit?limit=20").get_json()
    entry = next((a for a in audit if a["action"] == "ai.query"), None)
    assert entry is not None
    assert "message" not in entry["details"]  # texte brut non journalisé
    assert entry["details"]["message_len"] > 0


# --------------------------------------------------------------------------
# Minimisation des données par les outils
# --------------------------------------------------------------------------
def test_tools_strip_identifiers(app, client):
    agent_id, token = _enroll(client, "MACHINE-AI-MINI")
    client.post(f"/api/v1/agents/{agent_id}/heartbeat", json={"metrics": {"cpu_pct": 5, "ram_used_pct": 30}}, headers=_auth(token))

    with app.app_context():
        from app.ai import tools as ai_tools
        from app.models import HardwareInventory, Metric

        aid = uuid.UUID(agent_id)
        db.session.add(HardwareInventory(
            agent_id=aid, manufacturer="Dell", model="OptiPlex", serial_number="SN-SECRET-123",
            cpu_model="i5", cpu_cores=4, ram_total_mb=8192, disks=[], mac_addresses=["AA:BB:CC:DD:EE:FF"],
        ))
        db.session.add(Metric(agent_id=aid, cpu_pct=12, ram_used_pct=40,
                              disk_free={"C:": 120.0}, uptime_seconds=1000, logged_in_user="DOMAIN\\jdupont"))
        db.session.commit()

        detail = ai_tools.dispatch("get_agent_detail", {}, {"agent_id": agent_id})
        assert detail["hardware"] is not None
        assert "serial_number" not in detail["hardware"]
        assert "mac_addresses" not in detail["hardware"]
        assert detail["last_metric"] is not None
        assert "logged_in_user" not in detail["last_metric"]
        # Données IT utiles toujours présentes.
        assert detail["hardware"]["cpu_model"] == "i5"
        assert "SN-SECRET-123" not in json.dumps(detail)
        assert "jdupont" not in json.dumps(detail)


# --------------------------------------------------------------------------
# Propositions : structurées, validées, NON exécutées
# --------------------------------------------------------------------------
def test_ai_proposal_is_structured_not_executed(app, client, admin_session, monkeypatch):
    agent_id, _ = _enroll(client, "MACHINE-AI-PROP")
    fake = _scripted(
        _result(tool_calls=[_tc("propose_action", {"kind": "run_script", "rationale": "Vider le cache DNS", "script_key": "flush-dns"})]),
        _result("Je propose de vider le cache DNS."),
    )
    _enable_ai(app, monkeypatch, fake)

    r = admin_session.post("/api/v1/ai/chat", json={"message": "le DNS déconne", "agent_id": agent_id})
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["proposals"]) == 1
    prop = data["proposals"][0]
    assert prop["kind"] == "run_script"
    assert prop["confirm"]["endpoint"] == f"/api/v1/agents/{agent_id}/commands"
    # Preview résolu DEPUIS le catalogue (le modèle n'a fourni qu'une clé).
    assert "ipconfig /flushdns" in prop["preview"]["command_text"]

    # AUCUNE commande n'a été créée : l'IA propose, elle n'exécute pas.
    with app.app_context():
        assert db.session.query(Command).count() == 0


def test_ai_proposal_validation_and_danger(app, client, admin_session, monkeypatch):
    agent_id, _ = _enroll(client, "MACHINE-AI-VALID")
    # Script inconnu → rejeté (aucune proposition) ; le modèle « répond » ensuite.
    _enable_ai(app, monkeypatch, _scripted(
        _result(tool_calls=[_tc("propose_action", {"kind": "run_script", "rationale": "x", "script_key": "inexistant"})]),
        _result("Désolé, ce script n'existe pas."),
    ))
    r = admin_session.post("/api/v1/ai/chat", json={"message": "fais un truc", "agent_id": agent_id})
    assert r.status_code == 200
    assert r.get_json()["proposals"] == []

    # run_command libre → proposition marquée danger, preview verbatim.
    _enable_ai(app, monkeypatch, _scripted(
        _result(tool_calls=[_tc("propose_action", {"kind": "run_command", "rationale": "diag", "shell": "powershell", "command_text": "Get-Service spooler"})]),
        _result("Voici une commande de diagnostic."),
    ))
    r2 = admin_session.post("/api/v1/ai/chat", json={"message": "diag", "agent_id": agent_id})
    prop = r2.get_json()["proposals"][0]
    assert prop["kind"] == "run_command" and prop["danger"] is True
    assert prop["preview"]["command_text"] == "Get-Service spooler"


def test_ai_proposal_needs_agent_scope(app, admin_session, monkeypatch):
    # Sans agent_id, propose_action est rejeté (pas de cible).
    _enable_ai(app, monkeypatch, _scripted(
        _result(tool_calls=[_tc("propose_action", {"kind": "run_script", "rationale": "x", "script_key": "flush-dns"})]),
        _result("Je ne sais pas sur quel poste agir."),
    ))
    r = admin_session.post("/api/v1/ai/chat", json={"message": "vide le DNS"})
    assert r.status_code == 200
    assert r.get_json()["proposals"] == []


# --------------------------------------------------------------------------
# Confirmation : réutilise l'endpoint existant déjà audité
# --------------------------------------------------------------------------
def test_ai_confirm_reuses_existing_endpoint(app, client, admin_session, monkeypatch):
    agent_id, _ = _enroll(client, "MACHINE-AI-CONFIRM")
    _enable_ai(app, monkeypatch, _scripted(
        _result(tool_calls=[_tc("propose_action", {"kind": "run_script", "rationale": "DNS", "script_key": "flush-dns"})]),
        _result("Proposition prête."),
    ))
    prop = admin_session.post("/api/v1/ai/chat", json={"message": "dns", "agent_id": agent_id}).get_json()["proposals"][0]

    # L'UI poste le corps de la proposition vers l'endpoint existant.
    confirm = prop["confirm"]
    resp = admin_session.post(confirm["endpoint"], json=confirm["body"])
    assert resp.status_code == 201
    command_id = resp.get_json()["command_id"]

    with app.app_context():
        assert db.session.query(Command).count() == 1

    # L'audit existant command.create est produit (pas un audit IA spécifique).
    audit = admin_session.get("/api/v1/audit?limit=50").get_json()
    assert any(a["action"] == "command.create" for a in audit)
    # La commande est lisible via l'endpoint existant.
    assert admin_session.get(f"/api/v1/commands/{command_id}").status_code == 200


# --------------------------------------------------------------------------
# Boucle bornée & robustesse
# --------------------------------------------------------------------------
def test_ai_loop_is_bounded(app, client, admin_session, monkeypatch):
    agent_id, _ = _enroll(client, "MACHINE-AI-LOOP")
    # Le modèle réclame toujours un outil → on s'arrête à AI_MAX_TOOL_ITERS, puis on
    # force UN appel final sans outils (réponse de repli garantie).
    fake = _scripted(_result(tool_calls=[_tc("get_agent_detail", {})]))
    _enable_ai(app, monkeypatch, fake)

    r = admin_session.post("/api/v1/ai/chat", json={"message": "boucle ?", "agent_id": agent_id})
    assert r.status_code == 200
    assert fake.state["n"] == app.config["AI_MAX_TOOL_ITERS"] + 1  # + l'appel final forcé
    assert len(r.get_json()["reply"]) > 0  # une réponse est toujours renvoyée


def test_ai_client_error_is_graceful(app, admin_session, monkeypatch):
    def boom(messages, tools=None, **kwargs):
        raise ai_client.AIClientError("réseau coupé")

    _enable_ai(app, monkeypatch, boom)
    r = admin_session.post("/api/v1/ai/chat", json={"message": "salut"})
    assert r.status_code == 200  # jamais un 500
    data = r.get_json()
    assert data["error"] is True
    assert "erreur" in data["reply"].lower()
    # Le détail technique est remonté (endpoint réservé aux admins) pour le diagnostic.
    assert "réseau coupé" in data["reply"]
