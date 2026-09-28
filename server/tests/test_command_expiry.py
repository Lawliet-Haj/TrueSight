"""Tests de l'expiration des commandes perdues.

Une commande passe à ``dispatched`` quand l'agent vient la chercher. Si l'agent
disparaît pendant l'exécution (écran bleu, redémarrage, coupure réseau), aucun
résultat n'arrive jamais : sans expiration côté serveur, la commande restait
« en cours » pour toujours. Vécu le 28/09/2026 sur PF1D0V19 (écran bleu 8 s
après la réception de la commande) et plus tôt sur DESKTOP-V6RUH08.

Conséquences observées : l'interface affiche une commande éternellement en
cours, un lot ne se termine jamais, et l'auto-remédiation d'un service refuse
toute nouvelle tentative tant qu'une commande précédente est « en vol ».

Le temps est injecté (``now=``) : aucun test ne dort.

    cd server && python -m pytest tests/test_command_expiry.py -q
"""
import os
import sys
import uuid
from datetime import timedelta, timezone

import pytest

# Permet d'importer le paquet ``app`` depuis le dossier server/.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app import tasks  # noqa: E402
from app.command_expiry import expire_lost_commands  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Command  # noqa: E402

# Délai de grâce imposé par les tests : les échéances attendues ci-dessous sont
# calculées À LA MAIN à partir de cette valeur, jamais lues dans le code testé.
GRACE = 60


@pytest.fixture()
def app():
    """Application de test isolée (SQLite mémoire, sans thread de fond)."""
    application = create_app(TestConfig)
    application.config["COMMAND_RESULT_GRACE_SECONDS"] = GRACE
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Compteurs mémoire de module : sans remise à zéro, l'ordre des tests compte."""
    from app import api_agent, web

    api_agent._enroll_hits.clear()
    web._login_failures.clear()
    yield


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_session(client):
    """Client connecté en admin (sans MFA)."""
    resp = client.post(
        "/login",
        data={"email": TestConfig.ADMIN_EMAIL, "password": TestConfig.ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    return client


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _enroll(client, machine_id):
    """Enrôle un agent (nom d'hôte = machine_id) et renvoie (agent_id, token)."""
    resp = client.post(
        "/api/v1/enroll",
        json={
            "enrollment_token": TestConfig.ENROLLMENT_TOKEN,
            "machine_id": machine_id,
            "hostname": machine_id,
            "os_version": "Windows 11 Pro 26100",
            "agent_version": "1.5.13",
        },
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()
    return data["agent_id"], data["agent_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _queue(admin, agent_id, timeout, text="Get-Date"):
    """L'admin met une commande en file ; renvoie son identifiant."""
    resp = admin.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"shell": "powershell", "command_text": text, "timeout_seconds": timeout},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()["command_id"]


def _pull(client, agent_id, token):
    """L'agent vient chercher son travail : ses commandes passent à ``dispatched``."""
    resp = client.get(f"/api/v1/agents/{agent_id}/commands", headers=_auth(token))
    assert resp.status_code == 200
    return [c["id"] for c in resp.get_json()["commands"]]


def _post_result(client, token, command_id, stdout, exit_code=0):
    resp = client.post(
        f"/api/v1/commands/{command_id}/result",
        headers=_auth(token),
        json={"exit_code": exit_code, "stdout": stdout, "stderr": "", "duration_seconds": 2.0},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)


def _aware(ts):
    # SQLite rend des dates naïves, PostgreSQL des dates avec fuseau.
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _dispatched_at(app, command_id):
    with app.app_context():
        return _aware(db.session.get(Command, uuid.UUID(command_id)).dispatched_at)


def _expire_at(app, when):
    """Lance l'expiration comme si l'horloge du serveur marquait ``when``."""
    with app.app_context():
        return expire_lost_commands(now=when)


def _status(admin, command_id):
    """Ce que voit l'opérateur : la fiche de la commande au tableau de bord."""
    resp = admin.get(f"/api/v1/commands/{command_id}")
    assert resp.status_code == 200
    return resp.get_json()


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_commande_sans_resultat_passe_en_timeout_avec_explication(app, client, admin_session):
    """L'agent a reçu la commande puis a disparu : elle ne doit pas rester « en cours »."""
    agent_id, token = _enroll(client, "PERDUE-01")
    cid = _queue(admin_session, agent_id, timeout=90)
    assert _pull(client, agent_id, token) == [cid]
    t0 = _dispatched_at(app, cid)

    expired = _expire_at(app, t0 + timedelta(seconds=90 + GRACE + 1))

    data = _status(admin_session, cid)
    assert data["status"] == "timeout"
    assert data["completed_at"] is not None
    assert data["result"]["exit_code"] is None
    assert "aucun résultat reçu" in data["result"]["stderr"]
    assert expired == 1


def test_le_delai_de_grace_laisse_le_temps_au_resultat_d_arriver(app, client, admin_session):
    """Juste après le délai de la commande, le résultat peut encore être en route."""
    agent_id, token = _enroll(client, "GRACE-01")
    cid = _queue(admin_session, agent_id, timeout=90)
    _pull(client, agent_id, token)
    t0 = _dispatched_at(app, cid)

    _expire_at(app, t0 + timedelta(seconds=90 + GRACE - 1))
    assert _status(admin_session, cid)["status"] == "dispatched"

    _expire_at(app, t0 + timedelta(seconds=90 + GRACE + 1))
    assert _status(admin_session, cid)["status"] == "timeout"


def test_une_commande_en_file_n_expire_pas_avant_son_tour(app, client, admin_session):
    """L'agent exécute les commandes d'un même envoi L'UNE APRÈS L'AUTRE.

    Elles reçoivent le même ``dispatched_at``, mais la seconde ne démarre qu'à la
    fin de la première. L'échéance naïve (dispatched_at + son propre délai +
    grâce) la déclarerait perdue alors qu'elle attend simplement son tour — vécu
    le 28/09/2026 avec deux relevés envoyés ensemble au même poste.
    """
    agent_id, token = _enroll(client, "FILE-01")
    c1 = _queue(admin_session, agent_id, timeout=90, text="Get-Date")
    c2 = _queue(admin_session, agent_id, timeout=120, text="Get-Process")
    # L'horloge de Windows peut donner le même instant aux deux créations : on
    # rend l'ordre explicite pour que l'envoi (trié par created_at) soit c1, c2.
    with app.app_context():
        premiere = db.session.get(Command, uuid.UUID(c1))
        premiere.created_at = premiere.created_at - timedelta(seconds=1)
        db.session.commit()
    assert _pull(client, agent_id, token) == [c1, c2]  # un seul envoi, dans cet ordre
    t0 = _dispatched_at(app, c1)

    # c1 : 90 + 60 = 150 s.  c2 attend la fin de c1, puis 120 + 60 → 150 + 180 = 330 s.
    _expire_at(app, t0 + timedelta(seconds=151))
    assert _status(admin_session, c1)["status"] == "timeout"
    assert _status(admin_session, c2)["status"] == "dispatched"

    _expire_at(app, t0 + timedelta(seconds=329))
    assert _status(admin_session, c2)["status"] == "dispatched"

    _expire_at(app, t0 + timedelta(seconds=331))
    assert _status(admin_session, c2)["status"] == "timeout"


def test_la_commande_suivante_garde_son_budget_quand_la_premiere_a_reussi(app, client, admin_session):
    """Le cas courant : la première commande de l'envoi s'est terminée normalement.

    Son temps d'exécution a quand même retardé le départ de la seconde. Ne
    compter que les commandes encore « dispatched » l'oublierait et déclarerait
    la seconde perdue trop tôt.
    """
    agent_id, token = _enroll(client, "FILE-02")
    c1 = _queue(admin_session, agent_id, timeout=90, text="Get-Date")
    c2 = _queue(admin_session, agent_id, timeout=120, text="Get-Process")
    with app.app_context():
        premiere = db.session.get(Command, uuid.UUID(c1))
        premiere.created_at = premiere.created_at - timedelta(seconds=1)
        db.session.commit()
    assert _pull(client, agent_id, token) == [c1, c2]
    t0 = _dispatched_at(app, c1)
    _post_result(client, token, c1, stdout="ok")

    # Échéance naïve de c2 : 120 + 60 = 180 s. La bonne : 150 + 180 = 330 s.
    _expire_at(app, t0 + timedelta(seconds=181))
    assert _status(admin_session, c2)["status"] == "dispatched"

    _expire_at(app, t0 + timedelta(seconds=331))
    assert _status(admin_session, c2)["status"] == "timeout"


def test_un_resultat_tardif_remplace_le_constat_de_perte(app, client, admin_session):
    """CHOIX ASSUMÉ : la perte n'est qu'une présomption.

    L'agent retente l'envoi de son résultat plusieurs minutes quand le réseau
    flanche. S'il finit par arriver, c'est lui qui fait foi : l'opérateur doit
    voir la vraie sortie, pas notre supposition.
    """
    agent_id, token = _enroll(client, "TARDIF-01")
    cid = _queue(admin_session, agent_id, timeout=90)
    _pull(client, agent_id, token)
    t0 = _dispatched_at(app, cid)
    _expire_at(app, t0 + timedelta(seconds=90 + GRACE + 1))
    assert _status(admin_session, cid)["status"] == "timeout"  # la perte a bien été constatée

    _post_result(client, token, cid, stdout="sortie reelle")

    data = _status(admin_session, cid)
    assert data["status"] == "done"
    assert data["result"]["stdout"] == "sortie reelle"
    assert "aucun résultat reçu" not in data["result"]["stderr"]


def test_une_commande_terminee_n_est_jamais_requalifiee(app, client, admin_session):
    """Un résultat déjà reçu ne doit jamais être écrasé par le constat de perte."""
    agent_id, token = _enroll(client, "FINIE-01")
    cid = _queue(admin_session, agent_id, timeout=90)
    _pull(client, agent_id, token)
    t0 = _dispatched_at(app, cid)
    _post_result(client, token, cid, stdout="ok")

    _expire_at(app, t0 + timedelta(days=1))

    data = _status(admin_session, cid)
    assert data["status"] == "done"
    assert data["result"]["stdout"] == "ok"
    assert data["result"]["stderr"] == ""


def test_une_commande_jamais_transmise_attend_le_retour_du_poste(app, client, admin_session):
    """Poste éteint : la commande n'a jamais été remise, elle attend son retour.

    C'est le comportement documenté (mode opératoire §1) : seule une commande
    REMISE à un agent qui ne répond plus est perdue.
    """
    agent_id, _ = _enroll(client, "ETEINT-01")
    cid = _queue(admin_session, agent_id, timeout=90)
    with app.app_context():
        creee = _aware(db.session.get(Command, uuid.UUID(cid)).created_at)

    _expire_at(app, creee + timedelta(days=7))

    assert _status(admin_session, cid)["status"] == "pending"


def test_le_secret_d_une_commande_perdue_est_purge(app, client, admin_session):
    """Le mot de passe d'une création de compte est purgé à la réception du résultat.

    Une commande perdue n'en reçoit jamais : sans purge à l'expiration, le
    secret resterait en base indéfiniment.
    """
    agent_id, token = _enroll(client, "SECRET-01")
    secret = "Sup3rSecretX!"
    resp = admin_session.post(
        f"/api/v1/agents/{agent_id}/accounts/create",
        json={"username": "techuser", "password": secret},
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    cid = resp.get_json()["command_id"]
    _pull(client, agent_id, token)
    with app.app_context():
        cmd = db.session.get(Command, uuid.UUID(cid))
        assert secret in cmd.command_text  # précondition : le secret est bien là
        timeout = cmd.timeout_seconds
    t0 = _dispatched_at(app, cid)

    _expire_at(app, t0 + timedelta(seconds=timeout + GRACE + 1))

    with app.app_context():
        assert secret not in db.session.get(Command, uuid.UUID(cid)).command_text


def test_un_lot_se_termine_quand_un_poste_disparait(app, client, admin_session):
    """Un poste qui s'éteint pendant l'exécution ne doit pas bloquer le lot entier."""
    a1, t1 = _enroll(client, "LOT-VIVANT")
    a2, t2 = _enroll(client, "LOT-DISPARU")
    sent = admin_session.post("/api/v1/agents/bulk", json={
        "agent_ids": [a1, a2], "kind": "command", "shell": "powershell",
        "command_text": "Get-Date", "timeout_seconds": 60,
    })
    assert sent.status_code == 201, sent.get_data(as_text=True)
    batch = sent.get_json()["batch_id"]
    (c1,) = _pull(client, a1, t1)
    _post_result(client, t1, c1, stdout="ok")
    (c2,) = _pull(client, a2, t2)  # reçue… puis le poste s'éteint
    t0 = _dispatched_at(app, c2)
    avant = admin_session.get(f"/api/v1/command-batches/{batch}").get_json()
    assert avant["tally"] == {"pending": 1, "done": 1, "failed": 0}

    _expire_at(app, t0 + timedelta(seconds=60 + GRACE + 1))

    apres = admin_session.get(f"/api/v1/command-batches/{batch}").get_json()
    assert apres["tally"] == {"pending": 0, "done": 1, "failed": 1}
    disparu = next(i for i in apres["items"] if i["command_id"] == c2)
    assert "aucun résultat reçu" in disparu["stderr"]


def test_le_cycle_de_fond_expire_les_commandes_perdues(app, client, admin_session):
    """Un poste mort ne viendra jamais déclencher l'expiration lui-même.

    C'est le cycle de fond (toutes les 60 s en production) qui fait le ménage.
    """
    agent_id, token = _enroll(client, "FOND-01")
    cid = _queue(admin_session, agent_id, timeout=90)
    _pull(client, agent_id, token)
    with app.app_context():
        cmd = db.session.get(Command, uuid.UUID(cid))
        cmd.dispatched_at = cmd.dispatched_at - timedelta(hours=1)  # remise il y a une heure
        db.session.commit()

    tasks.run_once(app)

    assert _status(admin_session, cid)["status"] == "timeout"
