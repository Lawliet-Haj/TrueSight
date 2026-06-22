"""Paquet de l'agent Windows TrueSight.

L'agent interroge (poll) le serveur en HTTPS sortant ; il n'écoute jamais.
Il collecte l'inventaire matériel/logiciel, envoie des métriques (heartbeat),
récupère les commandes en attente et renvoie leurs résultats.
"""

# Version de l'agent. 1.1.3 : écoute du son système du poste (WASAPI loopback,
# PyAudioWPatch) pendant la prise de main. 1.1.2 : navigation à distance (curseur,
# verrou saisie, Ctrl+Alt+Suppr, lock sortie, écran de confidentialité). 1.1.1 :
# capture DXGI (écran noir au login). Un numéro supérieur déclenche l'auto-update.
__version__ = "1.1.3"

# Nom du service Windows (référencé par service.py et install-service.ps1).
SERVICE_NAME = "TrueSightAgent"
