"""Paquet de l'agent Windows TrueSight.

L'agent interroge (poll) le serveur en HTTPS sortant ; il n'écoute jamais.
Il collecte l'inventaire matériel/logiciel, envoie des métriques (heartbeat),
récupère les commandes en attente et renvoie leurs résultats.
"""

# Version de l'agent. 1.1.2 : navigation à distance — surcouche curseur,
# verrouillage de la saisie locale (BlockInput), Ctrl+Alt+Suppr (SendSAS),
# verrouillage du poste à la déconnexion, écran de confidentialité (voile noir
# local exclu de la capture). 1.1.1 : capture DXGI (corrige l'écran noir au
# login). Un numéro supérieur déclenche l'auto-update des postes enrôlés.
__version__ = "1.1.2"

# Nom du service Windows (référencé par service.py et install-service.ps1).
SERVICE_NAME = "TrueSightAgent"
