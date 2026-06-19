"""Paquet de l'agent Windows TrueSight.

L'agent interroge (poll) le serveur en HTTPS sortant ; il n'écoute jamais.
Il collecte l'inventaire matériel/logiciel, envoie des métriques (heartbeat),
récupère les commandes en attente et renvoie leurs résultats.
"""

# Version de l'agent. 1.1.0 : prise de main non-assistée (bureau d'entrée / écran
# de connexion via helper SYSTEM). Un numéro supérieur déclenche l'auto-update des
# postes déjà enrôlés.
__version__ = "1.1.0"

# Nom du service Windows (référencé par service.py et install-service.ps1).
SERVICE_NAME = "TrueSightAgent"
