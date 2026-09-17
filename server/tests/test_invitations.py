"""Tests du parcours d'invitation d'un compte.

Ce que ces tests protègent, concrètement :

- un compte est créé SANS que personne ne choisisse de mot de passe à la place
  de l'intéressé, et il est INUTILISABLE tant que celui-ci n'a pas répondu ;
- le lien ne sert qu'UNE fois, et la base ne contient que son empreinte ;
- une réinitialisation FERME les sessions déjà ouvertes (sans quoi couper un
  accès compromis ne coupe rien du tout).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.extensions import db  # noqa: E402
from app.invitations import _hash  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture()
def app():
    application = create_app(TestConfig)
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def _login_admin(client, app):
    """Ouvre une session super-admin (le compte initial du seed)."""
    resp = client.post(
        "/login",
        data={"email": app.config["ADMIN_EMAIL"], "password": app.config["ADMIN_PASSWORD"]},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.data[:200]


def _invite(client, email="nouvelle@medicofi.fr", name="Nouvelle Personne", role="viewer"):
    resp = client.post("/api/v1/users", json={"email": email, "name": name, "role": role})
    assert resp.status_code == 201, resp.data[:300]
    return resp.get_json()


def test_creation_sans_mot_de_passe_produit_un_lien(client, app):
    """Créer un compte ne demande AUCUN mot de passe et renvoie un lien utilisable."""
    _login_admin(client, app)
    data = _invite(client)

    assert data["name"] == "Nouvelle Personne"
    assert data["invited"] is True
    # L'envoi n'est pas configuré en test : le lien doit donc revenir à l'écran,
    # c'est exactement le repli attendu en production quand Brevo refuse.
    assert data["invitation"]["ok"] is False
    assert "/invitation/" in data["invitation"]["lien"]

    token = data["invitation"]["lien"].rsplit("/", 1)[-1]
    with app.app_context():
        user = db.session.query(User).filter_by(email="nouvelle@medicofi.fr").one()
        # Le jeton EN CLAIR n'est jamais stocké : seule son empreinte l'est.
        assert user.invite_token_hash == _hash(token)
        assert user.invite_token_hash != token
        assert user.invite_expires_at is not None


def test_le_compte_invite_ne_peut_pas_se_connecter(client, app):
    """Tant que la personne n'a pas répondu, le compte n'ouvre rien."""
    _login_admin(client, app)
    _invite(client, email="muet@medicofi.fr", name="Muet")
    client.get("/logout")

    # Le mot de passe posé est aléatoire : aucune valeur devinable ne passe.
    # (Le refus se manifeste par un 401 ou un ré-affichage du formulaire ; ce
    # qui compte est qu'AUCUNE session ne s'ouvre — c'est ce qu'on vérifie.)
    for tentative in ("", "password", "changeme", "Nouvelle Personne"):
        resp = client.post(
            "/login", data={"email": "muet@medicofi.fr", "password": tentative}
        )
        assert resp.status_code in (200, 401), resp.status_code
        page = client.get("/agents")
        assert page.status_code in (302, 303)
        assert "/login" in page.headers.get("Location", "")


def test_le_lien_ne_sert_qu_une_fois(client, app):
    """Le jeton est consommé : rejouer le lien ne redonne pas la main."""
    _login_admin(client, app)
    data = _invite(client, email="unique@medicofi.fr", name="Unique")
    token = data["invitation"]["lien"].rsplit("/", 1)[-1]
    client.get("/logout")

    page = client.get(f"/invitation/{token}")
    assert page.status_code == 200
    assert b"choisissez votre mot de passe" in page.data.lower()

    ok = client.post(
        f"/invitation/{token}",
        data={"password": "monMotDePasse1", "confirm": "monMotDePasse1"},
    )
    assert ok.status_code in (302, 303)

    # Deuxième usage : refusé, et on est renvoyé vers la connexion.
    rejoue = client.post(
        f"/invitation/{token}",
        data={"password": "autreMotDePasse1", "confirm": "autreMotDePasse1"},
    )
    assert rejoue.status_code in (302, 303)
    assert "/login" in rejoue.headers.get("Location", "")

    # Le mot de passe choisi, lui, fonctionne.
    client.get("/logout")
    resp = client.post(
        "/login", data={"email": "unique@medicofi.fr", "password": "monMotDePasse1"}
    )
    assert resp.status_code in (302, 303)


def test_mot_de_passe_trop_faible_refuse(client, app):
    """Les règles minimales sont appliquées côté serveur, pas seulement en HTML."""
    _login_admin(client, app)
    data = _invite(client, email="faible@medicofi.fr", name="Faible")
    token = data["invitation"]["lien"].rsplit("/", 1)[-1]
    client.get("/logout")

    for mauvais in ("court1", "sanschiffres", "1234567890"):
        resp = client.post(
            f"/invitation/{token}", data={"password": mauvais, "confirm": mauvais}
        )
        assert resp.status_code in (302, 303)
        assert f"/invitation/{token}" in resp.headers.get("Location", "")

    # Le jeton n'a pas été consommé par ces échecs.
    assert client.get(f"/invitation/{token}").status_code == 200


def test_jeton_inconnu_ou_expire_renvoie_a_la_connexion(client, app):
    """Un lien invalide ne dit pas si le compte existe."""
    resp = client.get("/invitation/jeton-inexistant")
    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers.get("Location", "")


def test_reinitialisation_ferme_les_sessions_ouvertes(client, app):
    """Couper un accès doit couper aussi ce qui est déjà ouvert."""
    _login_admin(client, app)
    data = _invite(client, email="reinit@medicofi.fr", name="Reinit")
    token = data["invitation"]["lien"].rsplit("/", 1)[-1]
    user_id = data["id"]
    client.get("/logout")

    # La personne choisit son mot de passe : sa session s'ouvre.
    victime = app.test_client()
    victime.post(
        f"/invitation/{token}", data={"password": "premierChoix1", "confirm": "premierChoix1"}
    )
    assert victime.get("/agents").status_code == 200

    # L'administrateur réinitialise l'accès.
    _login_admin(client, app)
    resp = client.post(f"/api/v1/users/{user_id}/reset-password", json={})
    assert resp.status_code == 200

    # La session déjà ouverte ne doit plus passer.
    apres = victime.get("/agents")
    assert apres.status_code in (302, 303)
    assert "/login" in apres.headers.get("Location", "")


def test_renvoyer_une_invitation_invalide_la_precedente(client, app):
    """Repousser un lien doit périmer l'ancien, sinon deux liens vivent en parallèle."""
    _login_admin(client, app)
    data = _invite(client, email="double@medicofi.fr", name="Double")
    ancien = data["invitation"]["lien"].rsplit("/", 1)[-1]

    resp = client.post(f"/api/v1/users/{data['id']}/invite", json={})
    assert resp.status_code == 200
    nouveau = resp.get_json()["invitation"]["lien"].rsplit("/", 1)[-1]
    assert nouveau != ancien

    client.get("/logout")
    assert client.get(f"/invitation/{ancien}").status_code in (302, 303)
    assert client.get(f"/invitation/{nouveau}").status_code == 200


def test_email_obligatoire_et_nom_obligatoire(client, app):
    """Un compte sans nom n'est pas traçable : on le refuse."""
    _login_admin(client, app)
    assert client.post("/api/v1/users", json={"email": "x@y.fr"}).status_code == 400
    assert client.post(
        "/api/v1/users", json={"email": "pas-un-email", "name": "X"}
    ).status_code == 400
