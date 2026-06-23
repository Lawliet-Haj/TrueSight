"""Module « Bureau à distance » de l'agent TrueSight.

Ce paquet isole toute la logique de prise de contrôle du bureau (R1 + R2) :

  - ``capture``  : capture d'écran (mss) + encodage JPEG (PyTurboJPEG ou Pillow),
                   énumération des moniteurs, saut des trames identiques ;
  - ``inject``   : injection souris/clavier via ``SendInput`` (ctypes) ;
  - ``session``  : client WebSocket synchrone (websocket-client) qui apparie la
                   capture (agent → viewer) et l'injection (viewer → agent) ;
  - ``launcher`` : démarrage d'une session, avec bascule vers un *helper* lancé
                   dans la session interactive si l'agent tourne en service
                   SYSTEM (session 0, qui ne « voit » pas le bureau utilisateur).

Conforme au CONTRAT REMOTE (REMOTE.md) : trames binaires en-tête 8 octets +
JPEG, messages d'entrée JSON normalisés (souris 0..1), transport wss.

Tout le module est *jamais-crash* : une erreur de session ne doit pas faire
tomber l'agent de supervision.
"""

from __future__ import annotations

# En-tête binaire des trames agent → viewer (cf. CONTRAT REMOTE / REMOTE.md §4).
#   octet 0 : version de protocole (0x01)
#   octet 1 : type de message (0x00 = trame pleine, R1)
#   octets 2-3 : largeur  (uint16, little-endian)
#   octets 4-5 : hauteur  (uint16, little-endian)
#   octet 6 : index du moniteur capturé (uint8)
#   octet 7 : drapeaux (uint8, réservé)
PROTOCOL_VERSION = 0x01
MSG_TYPE_FULL_FRAME = 0x00
FRAME_HEADER_SIZE = 8

# Trame « tuilée » (R2 — envoi par régions modifiées). En-tête 10 octets :
#   octet 0 : version (0x01)
#   octet 1 : type (0x02 = trame tuilée / delta)
#   octets 2-3 : largeur totale  (uint16 LE)
#   octets 4-5 : hauteur totale  (uint16 LE)
#   octet 6 : index du moniteur (uint8)
#   octet 7 : drapeaux (uint8, réservé)
#   octets 8-9 : nombre de tuiles (uint16 LE)
# Puis, pour chaque tuile : [x u16][y u16][w u16][h u16][jpeg_len u32] + octets JPEG.
MSG_TYPE_TILED_FRAME = 0x02
TILED_HEADER_SIZE = 10
TILE_SUBHEADER_SIZE = 12

# Trame AUDIO (écoute du son système, agent → viewer). En-tête 8 octets :
#   octet 0 : version (0x01)
#   octet 1 : type (0x10 = audio PCM)
#   octets 2-5 : fréquence d'échantillonnage (uint32 LE)
#   octet 6 : nombre de canaux (uint8, 1 = mono)
#   octet 7 : drapeaux (uint8, réservé)
# Puis : échantillons PCM 16 bits signés (little-endian), entrelacés si stéréo.
MSG_TYPE_AUDIO = 0x10
AUDIO_HEADER_SIZE = 8

# Trame FICHIER (transfert de fichiers, download agent → viewer). En-tête 11 octets :
#   octet 0 : version (0x01)
#   octet 1 : type (0x20 = chunk de fichier)
#   octets 2-5 : id de transfert (uint32 LE)
#   octets 6-9 : numéro de séquence du chunk (uint32 LE, à partir de 0)
#   octet 10 : drapeaux (uint8 ; bit0 = dernier chunk)
# Puis : octets bruts du fichier (le payload).
# L'UPLOAD (viewer → agent) passe en JSON base64 (le viewer n'envoie que du texte ;
# cf. session._recv_loop), donc seul le DOWNLOAD utilise cette trame binaire.
MSG_TYPE_FILE_CHUNK = 0x20
FILE_CHUNK_HEADER_SIZE = 11
FILE_CHUNK_FLAG_LAST = 0x01
# Taille d'un chunk de download (octets bruts) — bien sous MAX_MESSAGE_BYTES (16 Mio)
# du relais. 256 Kio : bon compromis débit / micro-jitter vidéo (verrou d'envoi partagé).
FILE_CHUNK_SIZE = 256 * 1024
# Garde-fou applicatif : taille max d'un fichier transféré (download ET upload).
MAX_FILE_BYTES = 1024 * 1024 * 1024  # 1 Gio

# Taille de tuile par défaut (carré). Compromis : assez gros pour limiter le
# surcoût d'en-têtes JPEG, assez petit pour ne renvoyer que de petites régions.
DEFAULT_TILE_SIZE = 256

__all__ = [
    "PROTOCOL_VERSION",
    "MSG_TYPE_FULL_FRAME",
    "FRAME_HEADER_SIZE",
    "MSG_TYPE_TILED_FRAME",
    "TILED_HEADER_SIZE",
    "TILE_SUBHEADER_SIZE",
    "MSG_TYPE_AUDIO",
    "AUDIO_HEADER_SIZE",
    "MSG_TYPE_FILE_CHUNK",
    "FILE_CHUNK_HEADER_SIZE",
    "FILE_CHUNK_FLAG_LAST",
    "FILE_CHUNK_SIZE",
    "MAX_FILE_BYTES",
    "DEFAULT_TILE_SIZE",
]
