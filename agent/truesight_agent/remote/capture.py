"""Capture d'écran + encodage JPEG pour le bureau à distance (R1).

Responsabilités :
  - capturer l'écran via ``mss`` (rapide, sans dépendance lourde) ;
  - énumérer les moniteurs et permettre d'en choisir un ;
  - downscale optionnel (cap de largeur, utile sur liaison lente) ;
  - encoder en JPEG via ``PyTurboJPEG`` (libjpeg-turbo) si disponible, sinon
    repli ``Pillow`` ;
  - sauter la trame si elle est identique à la précédente (hash) — un bureau
    statique ne génère donc presque aucune donnée ;
  - produire un flux de trames au format du CONTRAT REMOTE : en-tête 8 octets
    (cf. ``truesight_agent.remote.__init__``) suivi des octets JPEG.

Cible : ~15-20 images/s. Le module est tolérant aux erreurs : une trame qui
échoue est journalisée et ignorée, la capture continue.

NB : ``mss`` capture le bureau de la **session courante**. Lancé depuis un
service SYSTEM (session 0), il ne verra pas le bureau utilisateur — c'est
pourquoi ``launcher`` relance un helper dans la session interactive (voir
``launcher.py``).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import struct
import sys
import time
from typing import Iterator

from . import (
    DEFAULT_TILE_SIZE,
    FRAME_HEADER_SIZE,
    MSG_TYPE_FULL_FRAME,
    MSG_TYPE_TILED_FRAME,
    PROTOCOL_VERSION,
)

_logger = logging.getLogger("truesight.remote.capture")

# Bornes de qualité JPEG (CONTRAT : set_quality 1..100).
_MIN_QUALITY = 1
_MAX_QUALITY = 100
_DEFAULT_QUALITY = 70

# Cap de largeur par défaut pour le downscale (0 = pas de downscale).
_DEFAULT_MAX_WIDTH = 1600

# Cadence cible (images/s). On ne dépasse pas pour ménager CPU/réseau.
_DEFAULT_TARGET_FPS = 18.0

# Imports d'encodage tolérants : mss est requis pour capturer, Pillow ou
# PyTurboJPEG pour encoder. On détecte au runtime ce qui est disponible.
try:
    import mss  # type: ignore
    _MSS_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001 - on remonte l'absence proprement.
    mss = None  # type: ignore
    _MSS_AVAILABLE = False
    _logger.warning("mss indisponible (%s) : la capture d'écran est inopérante.", _exc)

def _locate_turbojpeg_dll() -> str | None:
    """Localise ``turbojpeg.dll`` (libjpeg-turbo) pour un build figé ou un poste.

    Ordre : variable ``TURBOJPEG_DLL`` → dossier du bundle PyInstaller
    (``sys._MEIPASS``) → dossier de l'exe (+ ``_internal``) → installation système
    standard. Renvoie None si introuvable (PyTurboJPEG tentera sa détection auto,
    puis on se rabat sur Pillow).
    """
    candidates: list[str] = []
    env = os.environ.get("TURBOJPEG_DLL")
    if env:
        candidates.append(env)
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        candidates.append(os.path.join(mei, "turbojpeg.dll"))
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "turbojpeg.dll"))
        candidates.append(os.path.join(exe_dir, "_internal", "turbojpeg.dll"))
    except Exception:  # noqa: BLE001
        pass
    candidates.append(r"C:\libjpeg-turbo64\bin\turbojpeg.dll")
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


try:
    from turbojpeg import TurboJPEG, TJPF_BGRX  # type: ignore
    _dll = _locate_turbojpeg_dll()
    _TURBOJPEG = TurboJPEG(_dll) if _dll else TurboJPEG()
    _TURBOJPEG_AVAILABLE = True
    _logger.info("Encodage JPEG via PyTurboJPEG (libjpeg-turbo, dll=%s).", _dll or "auto")
except Exception as _exc:  # noqa: BLE001 - PyTurboJPEG optionnel.
    _TURBOJPEG = None  # type: ignore
    TJPF_BGRX = None  # type: ignore
    _TURBOJPEG_AVAILABLE = False
    _logger.info("PyTurboJPEG indisponible (%s) : encodage Pillow.", _exc)

try:
    from PIL import Image  # type: ignore
    _PIL_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    Image = None  # type: ignore
    _PIL_AVAILABLE = False
    if not _TURBOJPEG_AVAILABLE:
        _logger.warning("Pillow indisponible (%s) et pas de PyTurboJPEG : encodage impossible.", _exc)


def is_available() -> bool:
    """True si la capture est possible (mss + au moins un encodeur JPEG)."""
    return _MSS_AVAILABLE and (_TURBOJPEG_AVAILABLE or _PIL_AVAILABLE)


def list_monitors() -> list[dict]:
    """Énumère les moniteurs disponibles.

    Renvoie une liste de dicts ``{"index", "left", "top", "width", "height"}``.
    L'index 0 est conventionnellement « tous les écrans réunis » côté mss ; on
    expose ici les écrans individuels (index 1..N de mss), réindexés à partir
    de 0 pour le viewer (le viewer envoie ``set_monitor`` avec ce 0-based).
    """
    if not _MSS_AVAILABLE:
        return []
    monitors: list[dict] = []
    try:
        with mss.mss() as sct:
            # sct.monitors[0] = bounding box de tous les écrans ; [1:] = écrans réels.
            for idx, mon in enumerate(sct.monitors[1:]):
                monitors.append({
                    "index": idx,
                    "left": int(mon["left"]),
                    "top": int(mon["top"]),
                    "width": int(mon["width"]),
                    "height": int(mon["height"]),
                })
    except Exception as exc:  # noqa: BLE001
        _logger.error("Énumération des moniteurs impossible : %s", exc)
    return monitors


def _clamp_quality(quality: int) -> int:
    """Borne la qualité JPEG dans 1..100."""
    try:
        q = int(quality)
    except (TypeError, ValueError):
        return _DEFAULT_QUALITY
    return max(_MIN_QUALITY, min(_MAX_QUALITY, q))


def _encode_jpeg(raw_bgra: bytes, width: int, height: int, quality: int) -> bytes | None:
    """Encode une image BGRA brute (mss) en JPEG.

    Utilise PyTurboJPEG si disponible (rapide), sinon Pillow. Renvoie les
    octets JPEG, ou None en cas d'échec (la trame sera sautée).
    """
    if _TURBOJPEG_AVAILABLE and _TURBOJPEG is not None:
        try:
            # mss fournit du BGRA ; on indique le format de pixel d'entrée.
            return _TURBOJPEG.encode(
                _bgra_to_numpy(raw_bgra, width, height),
                quality=quality,
                pixel_format=TJPF_BGRX,
            )
        except Exception as exc:  # noqa: BLE001 - repli Pillow.
            _logger.debug("Encodage TurboJPEG échoué (%s), repli Pillow.", exc)

    if _PIL_AVAILABLE and Image is not None:
        try:
            import io
            # mss : ordre des octets BGRA. Pillow lit en 'raw' avec mode 'RGB'
            # et décodeur 'BGRX' (ignore l'alpha) → conversion correcte des couleurs.
            image = Image.frombytes("RGB", (width, height), raw_bgra, "raw", "BGRX")
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            return buffer.getvalue()
        except Exception as exc:  # noqa: BLE001
            _logger.error("Encodage Pillow échoué : %s", exc)
            return None

    return None


def _bgra_to_numpy(raw_bgra: bytes, width: int, height: int):
    """Convertit le buffer BGRA brut en tableau numpy HxWx4 (pour TurboJPEG)."""
    import numpy as np  # numpy est tiré par PyTurboJPEG.
    arr = np.frombuffer(raw_bgra, dtype=np.uint8)
    return arr.reshape((height, width, 4))


def _downscale_bgra(raw_bgra: bytes, width: int, height: int, max_width: int):
    """Réduit l'image si sa largeur dépasse ``max_width`` (ratio conservé).

    Renvoie ``(raw_bgra, width, height)`` éventuellement réduits. Nécessite
    Pillow ; sans Pillow, on renvoie l'image inchangée.
    """
    if max_width <= 0 or width <= max_width:
        return raw_bgra, width, height
    if not _PIL_AVAILABLE or Image is None:
        return raw_bgra, width, height
    try:
        scale = max_width / float(width)
        new_w = max_width
        new_h = max(1, int(round(height * scale)))
        image = Image.frombytes("RGB", (width, height), raw_bgra, "raw", "BGRX")
        image = image.resize((new_w, new_h), Image.BILINEAR)
        # On repasse en BGRX pour conserver un chemin d'encodage homogène.
        return image.tobytes("raw", "BGRX"), new_w, new_h
    except Exception as exc:  # noqa: BLE001
        _logger.debug("Downscale impossible (%s), trame pleine résolution.", exc)
        return raw_bgra, width, height


def build_frame(jpeg_bytes: bytes, width: int, height: int, monitor_index: int, flags: int = 0) -> bytes:
    """Assemble l'en-tête binaire 8 octets + les octets JPEG (CONTRAT REMOTE).

    [0x01][0x00][width u16 LE][height u16 LE][monitor u8][flags u8] + JPEG.
    """
    # Les champs largeur/hauteur sont des uint16 ; on borne pour éviter un
    # overflow struct sur des résolutions exotiques (très improbable après downscale).
    safe_w = max(0, min(0xFFFF, int(width)))
    safe_h = max(0, min(0xFFFF, int(height)))
    safe_mon = max(0, min(0xFF, int(monitor_index)))
    safe_flags = max(0, min(0xFF, int(flags)))
    header = struct.pack(
        "<BBHHBB",
        PROTOCOL_VERSION,
        MSG_TYPE_FULL_FRAME,
        safe_w,
        safe_h,
        safe_mon,
        safe_flags,
    )
    return header + jpeg_bytes


def build_tiled_frame(width: int, height: int, monitor_index: int, tiles: list, flags: int = 0) -> bytes:
    """Assemble une trame tuilée : en-tête 10 octets + N sous-blocs de tuiles.

    ``tiles`` : liste de tuples ``(x, y, w, h, jpeg_bytes)``.
    En-tête : [0x01][0x02][width u16][height u16][monitor u8][flags u8][count u16].
    Par tuile : [x u16][y u16][w u16][h u16][jpeg_len u32] + JPEG.
    """
    safe_w = max(0, min(0xFFFF, int(width)))
    safe_h = max(0, min(0xFFFF, int(height)))
    safe_mon = max(0, min(0xFF, int(monitor_index)))
    safe_flags = max(0, min(0xFF, int(flags)))
    parts = [struct.pack(
        "<BBHHBBH",
        PROTOCOL_VERSION,
        MSG_TYPE_TILED_FRAME,
        safe_w,
        safe_h,
        safe_mon,
        safe_flags,
        min(0xFFFF, len(tiles)),
    )]
    for (x, y, w, h, jpeg) in tiles:
        parts.append(struct.pack("<HHHHI", x, y, w, h, len(jpeg)))
        parts.append(jpeg)
    return b"".join(parts)


class ScreenCapturer:
    """Capture d'écran réutilisable, paramétrable à chaud.

    Thread-safe pour les *réglages* (qualité, moniteur, keyframe) : la boucle
    d'envoi (session) lit l'état au début de chaque trame. La capture mss
    elle-même doit rester sur un seul thread (instance ``mss`` non partagée).
    """

    def __init__(
        self,
        quality: int = _DEFAULT_QUALITY,
        max_width: int = _DEFAULT_MAX_WIDTH,
        target_fps: float = _DEFAULT_TARGET_FPS,
        monitor_index: int = 0,
    ) -> None:
        self.quality = _clamp_quality(quality)
        self.max_width = max(0, int(max_width))
        self.target_fps = max(1.0, float(target_fps))
        self.monitor_index = max(0, int(monitor_index))
        # Demande de keyframe : force l'envoi d'une trame PLEINE (et réinitialise
        # les empreintes de tuiles) à la prochaine capture.
        self._force_keyframe = True
        # Diff par tuiles : empreinte de chaque tuile déjà envoyée + taille courante.
        self.tile_size = DEFAULT_TILE_SIZE
        self._tile_hashes: dict[tuple[int, int], str] = {}
        self._frame_size: tuple[int, int] | None = None

    # -- Réglages pilotés par le viewer ---------------------------------------
    def set_quality(self, quality: int) -> None:
        """Applique une nouvelle qualité JPEG (1..100)."""
        self.quality = _clamp_quality(quality)
        _logger.info("Qualité JPEG réglée à %d.", self.quality)

    def set_fps(self, fps) -> None:
        """Règle la cadence cible (images/s, bornée 1..60). Piloté par le viewer."""
        try:
            self.target_fps = max(1.0, min(60.0, float(fps)))
        except (TypeError, ValueError):
            return
        _logger.info("Cadence cible réglée à %.0f i/s.", self.target_fps)

    def set_max_width(self, width) -> None:
        """Règle le cap de largeur du downscale (0 = pleine résolution) ; force une keyframe."""
        try:
            self.max_width = max(0, int(width))
        except (TypeError, ValueError):
            return
        self._force_keyframe = True
        _logger.info("Largeur max réglée à %d px.", self.max_width)

    def set_monitor(self, index: int) -> None:
        """Change le moniteur capturé (0-based) ; force une keyframe."""
        try:
            self.monitor_index = max(0, int(index))
        except (TypeError, ValueError):
            return
        self._force_keyframe = True
        _logger.info("Moniteur capturé réglé sur l'index %d.", self.monitor_index)

    def request_keyframe(self) -> None:
        """Force l'envoi de la prochaine trame (utilisé à la connexion du viewer)."""
        self._force_keyframe = True

    def current_monitor_geometry(self) -> dict | None:
        """Géométrie du moniteur courant (pour mettre l'injection à l'échelle)."""
        monitors = list_monitors()
        if not monitors:
            return None
        idx = self.monitor_index if self.monitor_index < len(monitors) else 0
        return monitors[idx]

    # -- Génération du flux de trames -----------------------------------------
    def frames(self, stop_check) -> Iterator[bytes]:
        """Générateur de trames binaires prêtes à émettre (en-tête + JPEG).

        ``stop_check`` : callable renvoyant True quand il faut arrêter.

        Saute les trames identiques (sauf keyframe demandée). Régule la cadence
        à ``target_fps``. Ne lève jamais : une trame en erreur est ignorée.
        """
        if not is_available():
            _logger.error("Capture indisponible (mss/encodeur manquant) : aucun flux.")
            return

        try:
            with mss.mss() as sct:
                while not stop_check():
                    loop_start = time.monotonic()
                    frame = self._grab_and_encode(sct)
                    if frame is not None:
                        yield frame
                    # Régulation de cadence : on relit target_fps à chaque tour
                    # (réglable à chaud via set_fps) et on dort le reste de l'intervalle.
                    elapsed = time.monotonic() - loop_start
                    remaining = (1.0 / self.target_fps) - elapsed
                    if remaining > 0:
                        time.sleep(remaining)
        except Exception as exc:  # noqa: BLE001 - la capture ne crashe jamais.
            _logger.error("Boucle de capture interrompue : %s", exc)

    def _grab_and_encode(self, sct) -> bytes | None:
        """Capture un écran et renvoie soit une trame PLEINE (keyframe), soit une
        trame TUILÉE ne contenant que les régions modifiées, soit None si rien n'a
        changé / en cas d'échec."""
        try:
            monitors = sct.monitors[1:]  # écrans réels (hors bounding box global)
            if not monitors:
                return None
            idx = self.monitor_index if self.monitor_index < len(monitors) else 0
            mon = monitors[idx]
            shot = sct.grab(mon)
            width, height = shot.width, shot.height
            raw = bytes(shot.raw)  # BGRA
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Capture d'une trame échouée : %s", exc)
            return None

        # Downscale éventuel (l'image « de travail » = celle réellement envoyée).
        raw, width, height = _downscale_bgra(raw, width, height, self.max_width)

        # Changement de résolution → keyframe + remise à zéro des empreintes.
        if self._frame_size != (width, height):
            self._frame_size = (width, height)
            self._force_keyframe = True
            self._tile_hashes = {}

        keyframe = self._force_keyframe
        self._force_keyframe = False

        if keyframe:
            # Trame PLEINE (robuste) + (ré)initialise les empreintes de tuiles.
            jpeg = _encode_jpeg(raw, width, height, self.quality)
            if jpeg is None:
                return None
            self._index_tiles(raw, width, height)
            return build_frame(jpeg, width, height, idx)

        # Sinon : ne renvoyer que les tuiles modifiées (delta).
        return self._encode_changed_tiles(raw, width, height, idx)

    # -- Diff par tuiles ------------------------------------------------------
    def _iter_tiles(self, width: int, height: int):
        """Itère les coordonnées de tuiles (col, row, x, y, w, h) couvrant l'image."""
        ts = self.tile_size
        row = 0
        y = 0
        while y < height:
            th = min(ts, height - y)
            col = 0
            x = 0
            while x < width:
                tw = min(ts, width - x)
                yield col, row, x, y, tw, th
                x += ts
                col += 1
            y += ts
            row += 1

    def _index_tiles(self, raw: bytes, width: int, height: int) -> None:
        """Recalcule l'empreinte de toutes les tuiles (après un keyframe)."""
        self._tile_hashes = {}
        if not (_PIL_AVAILABLE and Image is not None):
            return
        try:
            img = Image.frombytes("RGB", (width, height), raw, "raw", "BGRX")
            for col, row, x, y, tw, th in self._iter_tiles(width, height):
                crop = img.crop((x, y, x + tw, y + th))
                self._tile_hashes[(col, row)] = hashlib.blake2b(crop.tobytes(), digest_size=12).hexdigest()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Indexation des tuiles impossible (%s).", exc)
            self._tile_hashes = {}

    def _encode_changed_tiles(self, raw: bytes, width: int, height: int, idx: int) -> bytes | None:
        """Compare chaque tuile à son empreinte ; encode + renvoie les tuiles modifiées."""
        if not (_PIL_AVAILABLE and Image is not None):
            # Sans Pillow, pas de découpe en tuiles : repli sur une trame pleine.
            jpeg = _encode_jpeg(raw, width, height, self.quality)
            return build_frame(jpeg, width, height, idx) if jpeg else None
        try:
            img = Image.frombytes("RGB", (width, height), raw, "raw", "BGRX")
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Image PIL impossible (%s).", exc)
            return None

        changed = []
        for col, row, x, y, tw, th in self._iter_tiles(width, height):
            crop = img.crop((x, y, x + tw, y + th))
            digest = hashlib.blake2b(crop.tobytes(), digest_size=12).hexdigest()
            if self._tile_hashes.get((col, row)) == digest:
                continue
            self._tile_hashes[(col, row)] = digest
            try:
                buf = io.BytesIO()
                crop.save(buf, format="JPEG", quality=self.quality)
                changed.append((x, y, tw, th, buf.getvalue()))
            except Exception as exc:  # noqa: BLE001
                _logger.debug("Encodage d'une tuile échoué (%s).", exc)

        if not changed:
            return None
        return build_tiled_frame(width, height, idx, changed)
