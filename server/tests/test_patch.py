"""Tests de la gestion des correctifs Windows (patch management).

Couvre :
- patch_catalog : validation des KB, modes, échappement PowerShell (injection) ;
- endpoints : install (admin), rescan, bulk-install, lecture /patch ;
- cycle complet via le pipeline de commandes : statut dérivé (exit 0 -> done,
  3010 -> reboot_pending) ;
- rétro-compatibilité avec les agents ne remontant que les compteurs.

SQLite en mémoire (TestConfig), comme test_api.py.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app import patch_catalog as pc  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.extensions import db  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures (autonomes — mêmes conventions que tests/test_api.py)
# --------------------------------------------------------------------------
@pytest.fixture()
def app():
    application = create_app(TestConfig)
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _reset_enroll_rate_limit():
    from app import api_agent, web

    api_agent._enroll_hits.clear()
    web._login_failures.clear()
    yield


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_session(client, app):
    resp = client.post(
        "/login",
        data={"email": TestConfig.ADMIN_EMAIL, "password": TestConfig.ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    return client


def _enroll(client, machine_id="MACHINE-PATCH-001"):
    resp = client.post(
        "/api/v1/enroll",
        json={
            "enrollment_token": TestConfig.ENROLLMENT_TOKEN,
            "machine_id": machine_id,
            "hostname": "PC-PATCH-01",
            "os_version": "Windows 11 Pro 26100",
            "agent_version": "1.2.0",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    return data["agent_id"], data["agent_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ==========================================================================
# Tests unitaires patch_catalog
# ==========================================================================
def test_build_install_critical():
    shell, text, timeout = pc.build_install("critical")
    assert shell == "powershell"
    assert timeout == pc.PATCH_TIMEOUT == 1800
    assert "Microsoft.Update.Session" in text
    assert "exit 3010" in text  # signalisation du redémarrage requis
    assert "Critical" in text and "Important" in text


def test_build_install_selected_escapes_kb():
    shell, text, timeout = pc.build_install("selected", ["kb5034441", "KB5012170"])
    # KB normalisés en majuscules, injectés comme littéraux PowerShell.
    assert "'KB5034441'" in text
    assert "'KB5012170'" in text


def test_build_install_invalid_mode():
    with pytest.raises(ValueError):
        pc.build_install("nimporte")


def test_build_install_selected_requires_kb():
    with pytest.raises(ValueError):
        pc.build_install("selected", [])


def test_build_install_rejects_malformed_kb():
    # Tentative d'injection : refusée par la validation AVANT tout encodage.
    with pytest.raises(ValueError):
        pc.build_install("selected", ["KB123'; Remove-Item C:\\ -Recurse"])
    with pytest.raises(ValueError):
        pc.build_install("selected", ["5034441"])  # préfixe KB manquant
    with pytest.raises(ValueError):
        pc.build_install("selected", ["KB12"])  # trop court


def test_valid_kb():
    assert pc.valid_kb("KB5034441")
    assert pc.valid_kb("kb5034441")
    assert not pc.valid_kb("KB12")
    assert not pc.valid_kb("hello")


def test_ps_lit_doubles_quotes():
    assert pc._ps_lit("a'b") == "'a''b'"


def test_build_rescan_is_readonly():
    shell, text, timeout = pc.build_rescan()
    assert shell == "powershell"
    # Lecture seule : pas d'appel d'installation.
    assert "CreateUpdateInstaller" not in text
    assert "Search('IsInstalled=0 and IsHidden=0')" in text
    # Émet le bloc JSON parsable par l'UI (liste cochable).
    assert pc.RESCAN_JSON_MARKER in text
    assert "ConvertTo-Json" in text


# ==========================================================================
# Endpoints
# ==========================================================================
def _post_security(client, agent_id, token, windows_update):
    payload = {"hardware": {}, "software": [], "security": {
        "defender": {"enabled": True, "realtime": True},
        "windows_update": windows_update,
    }}
    resp = client.post(f"/api/v1/agents/{agent_id}/inventory", json=payload, headers=_auth(token))
    assert resp.status_code == 200


def test_patch_install_creates_command_job_and_audit(client, admin_session):
    agent_id, token = _enroll(client)
    resp = admin_session.post(
        f"/api/v1/agents/{agent_id}/patch/install", json={"mode": "critical"}
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    data = resp.get_json()
    assert "command_id" in data and "patch_job_id" in data

    # La commande est bien en file pour l'agent.
    poll = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    cmds = poll.get_json()["commands"]
    assert len(cmds) == 1 and cmds[0]["id"] == data["command_id"]
    assert cmds[0]["shell"] == "powershell"

    # Audit présent (l'endpoint /audit renvoie une liste).
    audit = admin_session.get("/api/v1/audit?limit=50").get_json()
    actions = [e["action"] for e in audit]
    assert "patch.install" in actions


def test_patch_install_requires_admin(client):
    agent_id, token = _enroll(client)
    resp = client.post(f"/api/v1/agents/{agent_id}/patch/install", json={"mode": "critical"})
    assert resp.status_code in (401, 403)


def test_patch_install_invalid_mode(client, admin_session):
    agent_id, _ = _enroll(client)
    resp = admin_session.post(f"/api/v1/agents/{agent_id}/patch/install", json={"mode": "bidon"})
    assert resp.status_code == 400


def test_patch_install_selected_invalid_kb(client, admin_session):
    agent_id, _ = _enroll(client)
    resp = admin_session.post(
        f"/api/v1/agents/{agent_id}/patch/install",
        json={"mode": "selected", "kb_list": ["pas-un-kb"]},
    )
    assert resp.status_code == 400


def test_patch_cycle_done(client, admin_session):
    """install -> poll -> résultat exit 0 -> /patch montre le job 'done'."""
    agent_id, token = _enroll(client, "MACHINE-PATCH-DONE")
    cid = admin_session.post(
        f"/api/v1/agents/{agent_id}/patch/install", json={"mode": "all"}
    ).get_json()["command_id"]
    client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))  # dispatch
    client.post(
        f"/api/v1/commands/{cid}/result",
        json={"exit_code": 0, "stdout": "ok", "stderr": "", "duration_seconds": 12.0},
        headers=_auth(token),
    )
    patch = admin_session.get(f"/api/v1/agents/{agent_id}/patch").get_json()
    assert patch["jobs"][0]["status"] == "done"
    assert patch["reboot_pending"] is False


def test_patch_cycle_reboot_pending(client, admin_session):
    """Un exit_code 3010 dérive le statut en 'reboot_pending'."""
    agent_id, token = _enroll(client, "MACHINE-PATCH-REBOOT")
    cid = admin_session.post(
        f"/api/v1/agents/{agent_id}/patch/install", json={"mode": "all"}
    ).get_json()["command_id"]
    client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    client.post(
        f"/api/v1/commands/{cid}/result",
        json={"exit_code": 3010, "stdout": "REDEMARRAGE REQUIS", "stderr": "", "duration_seconds": 30.0},
        headers=_auth(token),
    )
    patch = admin_session.get(f"/api/v1/agents/{agent_id}/patch").get_json()
    assert patch["jobs"][0]["status"] == "reboot_pending"
    assert patch["reboot_pending"] is True


def test_patch_get_enriched_and_retrocompat(client, admin_session):
    agent_id, token = _enroll(client, "MACHINE-PATCH-GET")

    # Format enrichi (agent 1.2.0).
    _post_security(client, agent_id, token, {
        "pending_count": 2, "pending_critical": 1,
        "last_search_at": "2026-06-23T08:00:00+00:00",
        "updates": [
            {"kb": "KB5034441", "title": "Mise à jour de sécurité", "severity": "Critical",
             "size_mb": 12.3, "type": "Security Updates", "reboot_required": True},
        ],
    })
    p = admin_session.get(f"/api/v1/agents/{agent_id}/patch").get_json()
    assert p["pending_count"] == 2 and p["pending_critical"] == 1
    assert len(p["updates"]) == 1 and p["updates"][0]["kb"] == "KB5034441"

    # Rétro-compat : ancien agent (compteur seul, pas de clé 'updates').
    _post_security(client, agent_id, token, {"pending_count": 5, "pending_critical": 2})
    p2 = admin_session.get(f"/api/v1/agents/{agent_id}/patch").get_json()
    assert p2["pending_count"] == 5
    assert p2["updates"] == []  # pas de plantage, liste vide


def test_patch_bulk_install_by_agent_ids(client, admin_session):
    a1, t1 = _enroll(client, "MACHINE-BULK-1")
    a2, t2 = _enroll(client, "MACHINE-BULK-2")
    resp = admin_session.post(
        "/api/v1/patch/bulk-install",
        json={"agent_ids": [a1, a2], "mode": "critical"},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    data = resp.get_json()
    assert data["count"] == 2
    # Une commande en file par poste.
    assert len(client.get(f"/api/v1/agents/{a1}/commands", headers=_auth(t1)).get_json()["commands"]) == 1
    assert len(client.get(f"/api/v1/agents/{a2}/commands", headers=_auth(t2)).get_json()["commands"]) == 1


def test_patch_bulk_install_requires_target(client, admin_session):
    resp = admin_session.post("/api/v1/patch/bulk-install", json={"mode": "critical"})
    assert resp.status_code == 400


def test_patch_rescan_creates_command(client, admin_session):
    agent_id, token = _enroll(client, "MACHINE-RESCAN")
    resp = admin_session.post(f"/api/v1/agents/{agent_id}/patch/rescan", json={})
    assert resp.status_code == 201
    cid = resp.get_json()["command_id"]
    cmds = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token)).get_json()["commands"]
    assert cmds[0]["id"] == cid
