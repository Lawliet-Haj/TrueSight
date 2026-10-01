"""Tests des CODES DE SECOURS de la double authentification.

Le 29/09/2026, le seul superadmin a perdu son téléphone : plus de code, donc
plus d'accès, et aucun chemin de récupération prévu dans TrueSight. Les codes de
secours comblent ce trou : dix codes à usage unique, remis UNE fois à
l'activation, acceptés à l'écran de connexion à la place du code du téléphone.

    cd server && python -m pytest tests/test_mfa_recovery.py -q
"""
import json
import os
import re
import sys

import pyotp
import pytest

# Permet d'importer le paquet ``app`` depuis le dossier server/.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from app.config import TestConfig  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture()
def app():
    """Application de test isolée (SQLite mémoire, sans thread de fond)."""
    application = create_app(TestConfig)
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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _login_password(client):
    """Première étape : e-mail + mot de passe. Renvoie la réponse."""
    return client.post(
        "/login",
        data={"email": TestConfig.ADMIN_EMAIL, "password": TestConfig.ADMIN_PASSWORD},
        follow_redirects=False,
    )


def _enable_mfa(client):
    """Active la double authentification sur le compte admin ; renvoie (secret, codes)."""
    assert _login_password(client).status_code in (302, 303)
    setup = client.post("/api/v1/settings/mfa/setup").get_json()
    resp = client.post("/api/v1/settings/mfa/enable", json={"code": pyotp.TOTP(setup["secret"]).now()})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    codes = resp.get_json().get("recovery_codes")
    client.get("/logout")
    return setup["secret"], codes


def _second_step(client, code):
    """Connexion complète : mot de passe, puis le code à l'étape MFA."""
    first = _login_password(client)
    assert first.status_code in (302, 303) and "/mfa" in first.headers["Location"]
    return client.post("/mfa", data={"code": code}, follow_redirects=False)


def _connected(client):
    return client.get("/api/v1/settings/mfa", headers={"Accept": "application/json"}).status_code == 200


def _remaining(client):
    return client.get("/api/v1/settings/mfa").get_json()["recovery_codes_remaining"]


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_l_activation_remet_dix_codes_distincts(client):
    """À l'activation, dix codes lisibles sont remis — une seule fois."""
    _, codes = _enable_mfa(client)
    assert isinstance(codes, list) and len(codes) == 10
    assert len(set(codes)) == 10
    for c in codes:
        # 2 groupes de 5, alphabet sans I, L, O, U (confusions de lecture).
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}", c), c


def test_aucun_code_n_est_stocke_en_clair(app, client):
    """La base ne garde que des empreintes : elle ne permet pas de relire un code."""
    _, codes = _enable_mfa(client)
    with app.app_context():
        user = db.session.query(User).filter_by(email=TestConfig.ADMIN_EMAIL).one()
        stored = json.dumps(user.mfa_recovery_codes)
    assert len(user.mfa_recovery_codes) == 10
    for c in codes:
        assert c.replace("-", "") not in stored and c not in stored


def test_un_code_de_secours_ouvre_la_session(client):
    """Téléphone perdu : un code de secours remplace le code du téléphone."""
    _, codes = _enable_mfa(client)

    resp = _second_step(client, codes[0])

    assert resp.status_code in (302, 303), resp.get_data(as_text=True)
    assert _connected(client)
    assert _remaining(client) == 9


def test_un_code_de_secours_ne_sert_qu_une_fois(client):
    """Un code consommé ne rouvre plus rien : sinon ce serait un mot de passe bis."""
    _, codes = _enable_mfa(client)
    assert _second_step(client, codes[0]).status_code in (302, 303)
    client.get("/logout")

    resp = _second_step(client, codes[0])

    assert resp.status_code == 401
    assert not _connected(client)


def test_la_saisie_tolere_minuscules_espaces_et_tiret_absent(client):
    """On recopie un code à la main, souvent en minuscules et sans le tiret."""
    _, codes = _enable_mfa(client)

    resp = _second_step(client, "  " + codes[1].replace("-", "").lower() + " ")

    assert resp.status_code in (302, 303), resp.get_data(as_text=True)
    assert _connected(client)


def test_o_et_i_recopies_a_la_place_de_0_et_1_sont_acceptes(app, client):
    """Les codes n'utilisent jamais O, I ni L : recopiés à la main, ils valent 0 et 1."""
    import hashlib

    _enable_mfa(client)
    # Un code connu, posé en base sous sa forme stockée (SHA-256 de la forme
    # canonique, calculée ici à la main) : il contient des 0 et des 1.
    with app.app_context():
        user = db.session.query(User).filter_by(email=TestConfig.ADMIN_EMAIL).one()
        user.mfa_recovery_codes = [hashlib.sha256(b"A0B1CD0E1F").hexdigest()]
        db.session.commit()

    resp = _second_step(client, "AOBIC-DOELF")  # O pour 0, I et L pour 1

    assert resp.status_code in (302, 303), resp.get_data(as_text=True)
    assert _connected(client)


def test_un_mauvais_code_de_secours_est_refuse_et_compte_dans_la_limite(client):
    """Un code inventé est refusé, et chaque essai compte : pas de devinette illimitée."""
    _enable_mfa(client)
    assert _login_password(client).status_code in (302, 303)
    faux = "ABCDE-FGHJK"

    statuts = [client.post("/mfa", data={"code": faux}).status_code for _ in range(10)]
    bloque = client.post("/mfa", data={"code": faux}).status_code

    assert statuts == [401] * 10
    assert bloque == 429
    assert not _connected(client)


def test_regenerer_remplace_les_anciens_codes(client):
    """Régénérer (mot de passe exigé) invalide tous les anciens codes."""
    _, anciens = _enable_mfa(client)
    assert _second_step(client, anciens[0]).status_code in (302, 303)
    resp = client.post("/api/v1/settings/mfa/recovery-codes", json={"password": TestConfig.ADMIN_PASSWORD})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    nouveaux = resp.get_json()["recovery_codes"]
    assert len(nouveaux) == 10 and not set(nouveaux) & set(anciens)
    assert _remaining(client) == 10
    client.get("/logout")

    assert _second_step(client, anciens[1]).status_code == 401
    assert _second_step(client, nouveaux[0]).status_code in (302, 303)


def test_regenerer_exige_le_bon_mot_de_passe(client):
    """Une session laissée ouverte ne suffit pas à s'approprier de nouveaux codes."""
    _, codes = _enable_mfa(client)
    assert _second_step(client, codes[0]).status_code in (302, 303)

    resp = client.post("/api/v1/settings/mfa/recovery-codes", json={"password": "faux"})

    assert resp.status_code == 401
    assert _remaining(client) == 9  # rien n'a été régénéré


def test_desactiver_efface_les_codes(app, client):
    """Désactiver efface les codes EN BASE, pas seulement leur usage.

    La réactivation en tire de nouveaux et masquerait l'oubli : on vérifie donc
    la base elle-même. Des codes laissés en place redeviendraient valables le
    jour où une autre voie réactiverait la double authentification sans les
    renouveler.
    """
    secret, anciens = _enable_mfa(client)
    assert _second_step(client, pyotp.TOTP(secret).now()).status_code in (302, 303)
    assert client.post("/api/v1/settings/mfa/disable", json={"password": TestConfig.ADMIN_PASSWORD}).status_code == 200

    with app.app_context():
        user = db.session.query(User).filter_by(email=TestConfig.ADMIN_EMAIL).one()
        assert not user.mfa_recovery_codes

    client.get("/logout")
    _enable_mfa(client)
    assert _second_step(client, anciens[2]).status_code == 401


def test_un_compte_sans_codes_est_signale_a_zero(app, client):
    """Compte activé AVANT les codes de secours (le cas du 29/09) : zéro code, affiché comme tel."""
    secret, _ = _enable_mfa(client)
    with app.app_context():
        user = db.session.query(User).filter_by(email=TestConfig.ADMIN_EMAIL).one()
        user.mfa_recovery_codes = None
        db.session.commit()
    assert _second_step(client, pyotp.TOTP(secret).now()).status_code in (302, 303)

    assert _remaining(client) == 0


def test_le_code_du_telephone_fonctionne_toujours(client):
    """Garde-fou : ajouter les codes de secours ne doit pas casser le chemin normal."""
    secret, _ = _enable_mfa(client)

    resp = _second_step(client, pyotp.TOTP(secret).now())

    assert resp.status_code in (302, 303), resp.get_data(as_text=True)
    assert _connected(client)
    assert _remaining(client) == 10  # le code du téléphone ne consomme aucun code de secours
