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
    "DEFAULT_TILE_SIZE",
]
