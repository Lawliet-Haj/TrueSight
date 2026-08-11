"""Presse-papiers du poste (TEXTE) pour le bureau à distance.

Lecture/écriture du presse-papiers Windows via ``win32clipboard`` (pywin32, déjà
embarqué dans le paquet). **Texte uniquement** (``CF_UNICODETEXT``) : pas
d'images ni de listes de fichiers — cela borne le volume transféré et évite toute
exfiltration involontaire de pièces jointes par simple copie.

Ce module tourne dans la **session de l'utilisateur** (compagnon / helper) : il
voit donc bien le presse-papiers de la personne devant le poste. Il n'est pas
utilisable à l'écran de connexion (aucune session ouverte) : l'appelant garde la
responsabilité de ce contrôle (cf. ``session._desktop_follow``).

Tolérance aux pannes : le presse-papiers Windows est une ressource **partagée et
verrouillable** — une autre application peut le tenir ouvert au moment précis où
on y accède. On ré-essaie donc brièvement, et toute erreur renvoie un échec
propre (``None`` / ``False``) plutôt qu'une exception qui casserait la session.
"""

from __future__ import annotations

import logging
import time

_logger = logging.getLogger("truesight.remote.clipboard")

# Plafond de texte échangé (caractères). Large pour un usage de dépannage
# (scripts, chemins, journaux) tout en bornant le trafic et la mémoire.
_MAX_CHARS = 200_000

# Ouverture du presse-papiers : ré-essais courts si une autre app le verrouille.
_OPEN_RETRIES = 6
_OPEN_DELAY_S = 0.05


def _win32():
    """Renvoie ``(win32clipboard, win32con)`` ou ``(None, None)`` si indisponible."""
    try:
        import win32clipboard  # type: ignore
        import win32con  # type: ignore
        return win32clipboard, win32con
    except Exception as exc:  # noqa: BLE001 - pywin32 absent / non Windows.
        _logger.info("Presse-papiers indisponible (%s).", exc)
        return None, None


def is_available() -> bool:
    """True si le presse-papiers est accessible sur ce poste."""
    wc, _ = _win32()
    return wc is not None


def _open(wc) -> bool:
    """Ouvre le presse-papiers avec ré-essais. True si ouvert (à refermer !)."""
    for _ in range(_OPEN_RETRIES):
        try:
            wc.OpenClipboard()
            return True
        except Exception:  # noqa: BLE001 - verrouillé par une autre app.
            time.sleep(_OPEN_DELAY_S)
    _logger.info("Presse-papiers verrouillé par une autre application.")
    return False


def _close(wc) -> None:
    try:
        wc.CloseClipboard()
    except Exception:  # noqa: BLE001 - jamais bloquant.
        pass


def get_text() -> str | None:
    """Texte du presse-papiers du poste.

    - ``str`` (éventuellement vide) en cas de succès ; ``""`` si le presse-papiers
      ne contient pas de texte (image, fichiers…) — ce n'est pas une erreur ;
    - ``None`` si le presse-papiers est inaccessible.
    """
    wc, wcon = _win32()
    if wc is None:
        return None
    if not _open(wc):
        return None
    try:
        if not wc.IsClipboardFormatAvailable(wcon.CF_UNICODETEXT):
            return ""
        data = wc.GetClipboardData(wcon.CF_UNICODETEXT)
    except Exception as exc:  # noqa: BLE001
        _logger.info("Lecture du presse-papiers impossible (%s).", exc)
        return None
    finally:
        _close(wc)

    if not isinstance(data, str):
        return ""
    if len(data) > _MAX_CHARS:
        _logger.info("Presse-papiers tronqué (%s caractères).", len(data))
        return data[:_MAX_CHARS]
    return data


def set_text(text: str) -> bool:
    """Écrit ``text`` dans le presse-papiers du poste. True si succès."""
    if not isinstance(text, str):
        return False
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS]

    wc, wcon = _win32()
    if wc is None:
        return False
    if not _open(wc):
        return False
    try:
        # EmptyClipboard est requis avant SetClipboardData (et nous en donne la
        # propriété) ; sinon SetClipboardData échoue.
        wc.EmptyClipboard()
        wc.SetClipboardData(wcon.CF_UNICODETEXT, text)
        return True
    except Exception as exc:  # noqa: BLE001
        _logger.info("Écriture du presse-papiers impossible (%s).", exc)
        return False
    finally:
        _close(wc)
