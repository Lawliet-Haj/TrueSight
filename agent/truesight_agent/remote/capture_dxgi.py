"""Capture via DXGI Desktop Duplication (dxcam) — pour le BUREAU SÉCURISÉ.

Pourquoi ce module ?
--------------------
La capture « assistée » (cf. ``capture.py``) repose sur ``mss`` (GDI / BitBlt).
Or, sur le **bureau sécurisé** de Windows — écran de **connexion**, écran de
**verrouillage** et invites **UAC** — ``BitBlt`` renvoie une image **NOIRE**
(limitation connue de GDI). C'est exactement le bureau que capture le mode
NON-ASSISTÉ (helper SYSTEM attaché au bureau ``Winlogon`` quand personne n'est
connecté) → écran noir côté viewer.

L'API **DXGI Desktop Duplication**, elle, sait lire ce bureau sécurisé lorsque le
process tourne en **SYSTEM** et que le **thread appelant est attaché au bureau
d'entrée actif** (cf. ``desktop.attach_thread_to_input_desktop`` AVANT
``create``). C'est l'approche standard des outils de prise de main non-assistée.
Ce module n'est utilisé QUE par le mode non-assisté (``session._send_loop_unattended``).

Contrainte forte (dxcam + comtypes)
-----------------------------------
Recréer **ou** libérer la caméra DXGI dans le même process provoque un **crash COM**
(``access violation`` au moment du GC de comtypes — observé de façon reproductible).
Conséquences sur le design :

  1. On crée **UNE SEULE** caméra par process helper et on la **réutilise** pour
     toute la session ; on ne la libère **jamais**.
  2. À la moindre **bascule de bureau** (login terminé : ``Winlogon`` → ``Default``)
     ou changement d'écran, on **arrête** la session au lieu de recréer la
     duplication ; le viewer se reconnecte et un **nouveau** helper repart sur le
     bon bureau.
  3. Le helper doit **sortir via ``os._exit()``** (cf. ``__main__``) pour
     court-circuiter le GC comtypes responsable du crash.

Tolérant : tout échec renvoie ``None`` / ``False``, ne lève jamais. Si dxcam est
absent (machine sans GPU WDDM, import en échec…), ``is_available()`` est False et
``session`` se rabat sur ``mss`` (le bureau sécurisé restera noir, mais le bureau
utilisateur reste capturable).
"""

from __future__ import annotations

import logging
import threading

_logger = logging.getLogger("truesight.remote.capture_dxgi")

# Import tolérant : dxcam tire numpy + comtypes et un module compilé (.pyd).
try:
    import dxcam  # type: ignore
    _DXCAM_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 - on remonte l'absence proprement.
    dxcam = None  # type: ignore
    _DXCAM_AVAILABLE = False
    _logger.info("dxcam indisponible (%s) : capture DXGI désactivée (repli mss/GDI).", _exc)

# Vrai dès qu'une caméra DXGI a été créée dans CE process : signale à __main__
# qu'il faut sortir via os._exit() (le GC comtypes crasherait sinon).
_camera_created = False
_lock = threading.Lock()


def is_available() -> bool:
    """True si la capture DXGI (dxcam) est importable sur ce poste."""
    return _DXCAM_AVAILABLE


def camera_was_created() -> bool:
    """True si une caméra DXGI a été créée dans ce process.

    Le helper l'interroge pour décider d'une **sortie dure** (``os._exit``) : la
    libération/finalisation comtypes provoque sinon un crash (access violation).
    """
    return _camera_created


def create(output_idx: int = 0):
    """Crée la caméra DXGI (couleur BGRA) sur le bureau du thread courant.

    À appeler APRÈS ``desktop.attach_thread_to_input_desktop()`` : la duplication
    est liée au bureau actif au moment de l'appel. UNE SEULE caméra par process
    (cf. en-tête du module). Renvoie l'objet caméra, ou ``None`` en cas d'échec.
    """
    global _camera_created
    if not _DXCAM_AVAILABLE:
        return None
    with _lock:
        idx = max(0, int(output_idx))
        cam = None
        try:
            cam = dxcam.create(output_idx=idx, output_color="BGRA")
        except Exception as exc:  # noqa: BLE001
            _logger.error("Création DXGI (écran %s) échouée : %s", idx, exc)
            cam = None
        # Repli sur l'écran principal si l'index demandé n'a pas de sortie DXGI.
        if cam is None and idx != 0:
            try:
                cam = dxcam.create(output_idx=0, output_color="BGRA")
            except Exception as exc:  # noqa: BLE001
                _logger.error("Création DXGI (écran 0, repli) échouée : %s", exc)
                cam = None
        if cam is not None:
            _camera_created = True
    return cam


def captured_screen_rect(cam) -> dict | None:
    """Position RÉELLE, sur le bureau Windows, de l'écran que cette caméra duplique.

    Renvoie ``{"left", "top", "width", "height"}`` en pixels, ou ``None`` si DXGI
    ne la donne pas (caméra absente, description illisible, rectangle vide).

    POURQUOI CETTE FONCTION : un NUMÉRO d'écran ne dit pas quel écran est
    capturé. DXGI numérote les SORTIES d'un adaptateur ; la liste présentée au
    viewer vient, elle, de GDI (``capture.list_monitors``). Rien n'impose le
    même ordre aux deux, et ``create`` ci-dessus se rabat de surcroît sur la
    sortie 0 quand l'index demandé n'existe pas sur l'adaptateur. Placer les
    clics d'après l'index revenait donc à viser un autre écran que celui
    affiché — vécu le 30/09/2026 sur un poste à deux écrans : seule la barre des
    tâches, que Windows 11 dessine au même endroit sur chaque écran, semblait
    réagir. ``DesktopCoordinates`` est la seule donnée qui désigne l'écran
    effectivement dupliqué ; c'est elle qui doit ancrer l'injection.
    """
    try:
        out = getattr(cam, "_output", None)
        if out is None:
            out = getattr(cam, "output", None)
        rect = getattr(getattr(out, "desc", None), "DesktopCoordinates", None)
        if rect is None:
            return None
        left, top = int(rect.left), int(rect.top)
        width, height = int(rect.right) - left, int(rect.bottom) - top
    except Exception as exc:  # noqa: BLE001 - on ne fait jamais tomber la session.
        _logger.debug("Position de l'écran capturé illisible : %s", exc)
        return None
    if width <= 0 or height <= 0:
        _logger.debug("Position de l'écran capturé incohérente (%dx%d).", width, height)
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def grab(cam):
    """Capture une trame. Renvoie ``(raw_bgra: bytes, width, height)`` ou ``None``.

    ``None`` signifie « pas de nouvelle trame depuis la précédente » (DXGI détecte
    lui-même l'absence de changement → bande passante quasi nulle sur un écran de
    connexion statique) **ou** un échec de capture (loggé en debug, non fatal).
    """
    if cam is None:
        return None
    try:
        frame = cam.grab()
    except Exception as exc:  # noqa: BLE001
        _logger.debug("DXGI grab échoué : %s", exc)
        return None
    if frame is None:
        return None
    try:
        # dxcam (output_color="BGRA") renvoie un ndarray HxWx4 ; .tobytes() donne
        # des octets BGRA contigus, compatibles avec capture._encode_jpeg (BGRX).
        height = int(frame.shape[0])
        width = int(frame.shape[1])
        return frame.tobytes(), width, height
    except Exception as exc:  # noqa: BLE001
        _logger.debug("Conversion d'une trame DXGI échouée : %s", exc)
        return None
