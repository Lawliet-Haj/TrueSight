"""Capture du son système (WASAPI loopback) pour l'écoute distante.

Capte ce qui sort des haut-parleurs du poste (loopback) via **PyAudioWPatch**
(PortAudio). Permet à l'admin d'« écouter » le poste pendant la prise de main,
comme Ninja.

Cela n'a de sens qu'en **session utilisateur ouverte** (rien ne joue à l'écran de
connexion) : la capture tourne donc dans le helper de session utilisateur, jamais
dans le helper SYSTEM/non-assisté.

Chaîne : loopback PCM int16 (fréquence du périphérique, stéréo) → downmix MONO +
décimation 2:1 si possible (48 kHz → 24 kHz) → trames binaires sur la WebSocket →
le viewer rejoue via l'API Web Audio.

Tolérant : toute erreur (lib absente, pas de périphérique, session 0) → écoute
indisponible, jamais fatal pour la session.
"""

from __future__ import annotations

import logging
import struct

from . import AUDIO_HEADER_SIZE, MSG_TYPE_AUDIO, PROTOCOL_VERSION  # noqa: F401

_logger = logging.getLogger("truesight.remote.audio")

# Import tolérant : PyAudioWPatch embarque _portaudiowpatch (PortAudio statique).
try:
    import pyaudiowpatch as _pyaudio  # type: ignore
    _AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    _pyaudio = None  # type: ignore
    _AVAILABLE = False
    _logger.info("PyAudioWPatch indisponible (%s) : écoute audio désactivée.", _exc)

# Taille de bloc de lecture (frames) : ~43 ms à 48 kHz — compromis latence/charge.
_BLOCK_FRAMES = 2048


def is_available() -> bool:
    """True si la capture audio (loopback) est importable sur ce poste."""
    return _AVAILABLE


def build_audio_frame(pcm_mono: bytes, sample_rate: int) -> bytes:
    """Assemble l'en-tête 8 octets (CONTRAT REMOTE, type 0x10) + PCM mono int16."""
    header = struct.pack(
        "<BBIBB", PROTOCOL_VERSION, MSG_TYPE_AUDIO,
        int(sample_rate) & 0xFFFFFFFF, 1, 0,
    )
    return header + pcm_mono


class AudioCapture:
    """Capture loopback du périphérique de sortie par défaut (mono, int16)."""

    def __init__(self) -> None:
        self._pa = None
        self._stream = None
        self.sample_rate = 0       # fréquence émise (après décimation)
        self._src_channels = 0
        self._decimate = 1

    def start(self) -> bool:
        """Ouvre le flux loopback du haut-parleur par défaut. True si actif."""
        if not _AVAILABLE:
            return False
        try:
            self._pa = _pyaudio.PyAudio()
            wasapi = self._pa.get_host_api_info_by_type(_pyaudio.paWASAPI)
            spk = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            loop = None
            for dev in self._pa.get_loopback_device_info_generator():
                if spk["name"] in dev["name"]:
                    loop = dev
                    break
            if loop is None:  # repli : premier loopback disponible.
                loop = next(self._pa.get_loopback_device_info_generator(), None)
            if loop is None:
                _logger.info("Aucun périphérique loopback : écoute indisponible.")
                self.stop()
                return False

            self._src_channels = int(loop["maxInputChannels"]) or 2
            rate = int(loop["defaultSampleRate"]) or 48000
            # Décimation 2:1 (48 kHz → 24 kHz) pour ~moitié de bande passante.
            self._decimate = 2 if (rate % 2 == 0 and rate >= 32000) else 1
            self.sample_rate = rate // self._decimate
            self._stream = self._pa.open(
                format=_pyaudio.paInt16,
                channels=self._src_channels,
                rate=rate,
                frames_per_buffer=_BLOCK_FRAMES,
                input=True,
                input_device_index=loop["index"],
            )
            _logger.info(
                "Écoute audio active : %s (%d Hz x%d → %d Hz mono).",
                loop["name"], rate, self._src_channels, self.sample_rate,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.info("Démarrage de l'écoute audio impossible : %s", exc)
            self.stop()
            return False

    def read_mono(self) -> bytes | None:
        """Lit un bloc et renvoie le PCM int16 MONO (downmix + décimation), ou None."""
        if self._stream is None:
            return None
        try:
            import numpy as np
            raw = self._stream.read(_BLOCK_FRAMES, exception_on_overflow=False)
            arr = np.frombuffer(raw, dtype=np.int16)
            if self._src_channels > 1:
                arr = arr.reshape(-1, self._src_channels)
                # Downmix mono en int32 pour éviter le débordement, puis int16.
                mono = arr.astype(np.int32).mean(axis=1).astype(np.int16)
            else:
                mono = arr
            if self._decimate == 2 and mono.size >= 2:
                # Moyenne de paires = anti-repliement léger avant décimation.
                even = mono.size - (mono.size % 2)
                m = mono[:even].astype(np.int32)
                mono = ((m[0::2] + m[1::2]) // 2).astype(np.int16)
            return mono.tobytes()
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Lecture audio échouée : %s", exc)
            return None

    def stop(self) -> None:
        """Ferme le flux et libère PortAudio."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        pa = self._pa
        self._pa = None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:  # noqa: BLE001
                pass
