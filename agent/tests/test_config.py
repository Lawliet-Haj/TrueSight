"""Tests de la configuration de l'agent — les VALEURS PAR DÉFAUT.

Ces tests existent pour une raison précise : la prise de main élevée était
désactivée par défaut et posée après coup, par commande, sur les postes en
service. Tout poste enrôlé ensuite arrivait donc sans, et le symptôme se
redécouvrait machine par machine — « je vois la fenêtre, mes clics ne passent
pas ». Le défaut est désormais actif ; ce fichier empêche qu'il reparte en
arrière sans que personne ne le voie.

    cd agent && python -m pytest tests -q
"""
import os
import sys

import pytest

RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, RACINE)

from truesight_agent import config as cfg  # noqa: E402


MINIMAL = """[server]
url = https://exemple.invalid
enrollment_token = jeton-de-test

[agent]
heartbeat_interval = 45
"""


def _ecrire(tmp_path, contenu):
    fichier = tmp_path / "config.ini"
    fichier.write_text(contenu, encoding="utf-8")
    return str(fichier)


def test_mode_eleve_actif_par_defaut(tmp_path):
    """Un config.ini qui ne dit rien doit donner une prise de main ÉLEVÉE.

    C'est le cas d'un poste fraîchement installé : personne ne va y poser
    l'option à la main, elle doit être là dès l'enrôlement.
    """
    conf = cfg.load_config(_ecrire(tmp_path, MINIMAL))
    assert conf.remote_elevated is True


def test_un_refus_explicite_est_respecte(tmp_path):
    """« false » écrit dans le fichier doit gagner sur le défaut.

    Sans cela on ne pourrait plus exclure un poste particulier — pour
    reproduire un comportement, ou contourner une protection locale.
    """
    conf = cfg.load_config(_ecrire(tmp_path, MINIMAL + "remote_elevated = false\n"))
    assert conf.remote_elevated is False


@pytest.mark.parametrize("valeur", ["true", "True", "1", "yes", "on"])
def test_formes_acceptees_pour_activer(tmp_path, valeur):
    """Les écritures usuelles d'un booléen passent (fichier édité à la main)."""
    conf = cfg.load_config(_ecrire(tmp_path, MINIMAL + f"remote_elevated = {valeur}\n"))
    assert conf.remote_elevated is True


def test_les_autres_defauts_ne_bougent_pas(tmp_path):
    """Garde-fou : ce changement ne doit pas en emporter d'autres au passage."""
    conf = cfg.load_config(_ecrire(tmp_path, MINIMAL))
    assert conf.terminal_system is True      # terminal avec les droits SYSTEM
    assert conf.remote_unattended is True    # prise de main sans session ouverte
    assert conf.verify_tls is True           # jamais de TLS non vérifié par défaut
    assert conf.server_url == "https://exemple.invalid"
    assert conf.heartbeat_interval == 45


def test_tls_non_verifie_reste_un_choix_explicite(tmp_path):
    """On peut le désactiver, mais il faut l'écrire — ça ne s'obtient pas par omission."""
    contenu = MINIMAL.replace(
        "enrollment_token = jeton-de-test",
        "enrollment_token = jeton-de-test\nverify_tls = false",
    )
    assert cfg.load_config(_ecrire(tmp_path, contenu)).verify_tls is False
