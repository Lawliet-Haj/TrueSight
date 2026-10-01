"""Tests : EN PRISE DE MAIN, LES CLICS TOMBENT SUR L'ÉCRAN QUE L'ON VOIT.

Le 30/09/2026, sur un poste à DEUX écrans : l'opérateur voit bien le bureau du
poste, mais seuls les clics sur la BARRE DES TÂCHES ont l'air de passer — tout
le reste ne répond pas. Windows 11 dessine une barre des tâches au même endroit
sur chaque écran : un clic envoyé au mauvais écran y retrouve donc un bouton,
alors que partout ailleurs il tombe dans le vide.

La cause : la prise de main élevée capture via DXGI, qui numérote les SORTIES
d'un adaptateur, tandis que les clics étaient placés d'après la liste GDI (mss),
celle que voit le viewer. Rien n'impose le même ordre aux deux ; pire,
``capture_dxgi.create`` se rabat sur la sortie 0 quand l'index demandé n'existe
pas sur l'adaptateur. On regardait donc un écran et on cliquait sur l'autre.

Le correctif : ancrer l'injection sur la position que DXGI donne lui-même pour
l'image qu'il duplique (``DesktopCoordinates``), jamais sur un numéro d'écran.

    cd agent && python -m pytest tests -q
"""
import json
import logging
import os
import sys

import pytest

RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, RACINE)

from truesight_agent.remote import capture as capture_mod  # noqa: E402
from truesight_agent.remote import capture_dxgi  # noqa: E402
from truesight_agent.remote import inject as inject_mod  # noqa: E402
from truesight_agent.remote import session as session_mod  # noqa: E402


# --------------------------------------------------------------------------
# Le poste de l'incident : deux écrans côte à côte, et un ordre DXGI qui est
# l'INVERSE de l'ordre GDI — exactement ce qui fait rater les clics.
# --------------------------------------------------------------------------
GAUCHE = {"index": 0, "left": 0, "top": 0, "width": 1920, "height": 1080}
DROITE = {"index": 1, "left": 1920, "top": 0, "width": 1920, "height": 1080}
# Écran virtuel Windows (left, top, width, height) : les deux écrans réunis.
BUREAU = (0, 0, 3840, 1080)


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Desc:
    def __init__(self, rect):
        self.DesktopCoordinates = rect


class _Output:
    def __init__(self, rect):
        self.desc = _Desc(rect)


class CameraFactice:
    """Imite la caméra dxcam : elle sait dire OÙ est l'écran qu'elle duplique."""

    def __init__(self, ecran):
        self._output = _Output(_Rect(
            ecran["left"], ecran["top"],
            ecran["left"] + ecran["width"], ecran["top"] + ecran["height"],
        ))


@pytest.fixture()
def poste(monkeypatch):
    """Le poste à deux écrans : liste GDI figée et écran virtuel connu."""
    monkeypatch.setattr(capture_mod, "list_monitors", lambda: [dict(GAUCHE), dict(DROITE)])
    monkeypatch.setattr(inject_mod, "_virtual_screen_rect", lambda: BUREAU)


def _session(**kw):
    return session_mod.RemoteSession("jeton", "wss://exemple.invalid/remote", **kw)


def _prise_de_main_elevee(monkeypatch, camera, ecran_demande=0):
    """Démarre la capture élevée (DXGI) jusqu'à la caméra, puis rend la session.

    Les écritures réseau n'ont pas lieu : la caméra factice demande l'arrêt dès
    sa création, si bien que la boucle s'arrête avant la première trame. Ce qui
    nous intéresse est justement posé AVANT : l'ancrage des clics.
    """
    from truesight_agent.remote import desktop as desk

    monkeypatch.setattr(desk, "attach_thread_to_input_desktop", lambda: "Default")
    monkeypatch.setattr(desk, "current_input_desktop_name", lambda: "Default")
    monkeypatch.setattr(capture_dxgi, "is_available", lambda: True)
    monkeypatch.setattr(capture_dxgi, "grab", lambda cam: None)

    sess = _session(desktop_follow=True)
    if ecran_demande:
        sess._capturer.set_monitor(ecran_demande)

    def _create(idx):
        sess._stop.set()  # aucune trame ne sera même tentée
        return camera

    monkeypatch.setattr(capture_dxgi, "create", _create)
    sess._send_loop_unattended()
    return sess


def _point_clique(injecteur, nx, ny):
    """Où tombe, EN PIXELS DU BUREAU WINDOWS, un clic normalisé (nx, ny)."""
    abs_x, abs_y = injecteur._to_absolute(nx, ny)
    vleft, vtop, vwidth, vheight = BUREAU
    return (vleft + abs_x * vwidth / 65535.0, vtop + abs_y * vheight / 65535.0)


# --------------------------------------------------------------------------
# Ce que DXGI sait dire de l'écran qu'il capture
# --------------------------------------------------------------------------
def test_la_position_de_l_ecran_capture_vient_de_la_camera():
    """DXGI donne la position exacte de l'image qu'il duplique."""
    rect = capture_dxgi.captured_screen_rect(CameraFactice(DROITE))

    assert rect == {"left": 1920, "top": 0, "width": 1920, "height": 1080}


class CameraCapricieuse:
    """Caméra qui se fâche quand on l'interroge (sortie DXGI perdue en route).

    La capture tolère tout : une position illisible ne doit pas remonter en
    exception, sinon elle coupe la boucle non-assistée — donc la session — alors
    qu'il ne manque qu'un repère de géométrie.
    """

    @property
    def _output(self):
        raise OSError("sortie DXGI perdue")


@pytest.mark.parametrize("camera", [
    None,                                   # pas de caméra du tout
    object(),                               # caméra sans sortie exploitable
    CameraFactice({"left": 0, "top": 0, "width": 0, "height": 0}),  # rectangle vide
    CameraCapricieuse(),                    # lecture de la sortie en erreur
])
def test_une_camera_muette_ne_fait_pas_tomber_la_session(camera):
    """Faute de position lisible, on répond « je ne sais pas » — jamais une exception."""
    assert capture_dxgi.captured_screen_rect(camera) is None


# --------------------------------------------------------------------------
# Le correctif lui-même
# --------------------------------------------------------------------------
def test_la_prise_de_main_elevee_ancre_les_clics_sur_l_ecran_capture(monkeypatch, poste):
    """L'écran 0 de DXGI est ici celui de DROITE : les clics doivent y aller.

    La liste GDI, elle, place l'écran 0 à GAUCHE. C'est tout le défaut : suivre
    l'index revenait à cliquer sur l'autre écran que celui affiché.
    """
    sess = _prise_de_main_elevee(monkeypatch, CameraFactice(DROITE))

    assert sess._injector._monitor == {
        "left": 1920, "top": 0, "width": 1920, "height": 1080,
    }


def test_un_clic_au_centre_tombe_au_centre_de_l_ecran_affiche(monkeypatch, poste):
    """Le geste de l'opérateur, bout en bout : milieu de l'image = milieu de l'écran vu."""
    sess = _prise_de_main_elevee(monkeypatch, CameraFactice(DROITE))

    x, y = _point_clique(sess._injector, 0.5, 0.5)

    assert 1920 <= x < 3840, "le clic est parti sur l'écran de gauche"
    assert abs(x - 2880) <= 1 and abs(y - 540) <= 1


def test_la_surcouche_curseur_suit_le_meme_ecran(monkeypatch, poste):
    """Le curseur distant est dessiné par le viewer : même repère, sinon il dérive."""
    sess = _prise_de_main_elevee(monkeypatch, CameraFactice(DROITE))

    assert sess._injection_geometry() == {
        "left": 1920, "top": 0, "width": 1920, "height": 1080,
    }


def test_le_changement_d_ecran_du_viewer_ne_rebascule_pas_sur_la_liste_gdi(monkeypatch, poste):
    """Changer d'écran termine la session ; d'ici là, les clics restent sur l'écran capturé.

    Le message du viewer ne doit surtout pas replacer l'injection d'après la
    liste GDI : ce serait réintroduire le défaut le temps que la session se
    referme, c'est-à-dire pile au moment où l'opérateur clique encore.
    """
    sess = _prise_de_main_elevee(monkeypatch, CameraFactice(DROITE), ecran_demande=1)

    sess._handle_text(json.dumps({"t": "set_monitor", "i": 0}))

    assert sess._injector._monitor["left"] == 1920


def test_sans_position_dxgi_on_retombe_sur_la_liste_gdi(monkeypatch, poste):
    """Caméra qui ne dit rien : on garde l'ancien comportement, sans planter."""
    sess = _prise_de_main_elevee(monkeypatch, object())

    assert sess._injector._monitor == GAUCHE


def test_le_journal_signale_un_ecran_qui_ne_correspond_pas_a_son_numero(monkeypatch, poste, caplog):
    """Le poste doit dire lui-même qu'il est de ceux que le défaut touchait.

    Établir ce décalage a demandé un aller-retour avec l'opérateur (« je peux
    cliquer sur la barre des tâches mais pas autre part ») : sans cette ligne,
    le prochain poste concerné coûterait le même diagnostic.
    """
    with caplog.at_level(logging.WARNING, logger="truesight.remote.session"):
        _prise_de_main_elevee(monkeypatch, CameraFactice(DROITE))  # le numéro 0 vaut GAUCHE

    assert any("n'est PAS celui que porte ce numéro" in r.getMessage() for r in caplog.records)


def test_aucune_alerte_quand_l_ecran_correspond_a_son_numero(monkeypatch, poste, caplog):
    """Un poste sain ne crie pas au loup à chaque prise de main."""
    with caplog.at_level(logging.WARNING, logger="truesight.remote.session"):
        _prise_de_main_elevee(monkeypatch, CameraFactice(GAUCHE))  # le numéro 0 vaut bien GAUCHE

    assert not any("n'est PAS celui que porte ce numéro" in r.getMessage() for r in caplog.records)


def test_le_mode_assiste_continue_de_suivre_la_liste_gdi(poste):
    """Sans DXGI (prise de main assistée), capture et clics partagent déjà la liste GDI."""
    sess = _session()
    sess._capturer.set_monitor(1)

    assert sess._injection_geometry() == DROITE
