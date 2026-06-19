"""Paquet de l'agent Windows TrueSight.

L'agent interroge (poll) le serveur en HTTPS sortant ; il n'écoute jamais.
Il collecte l'inventaire matériel/logiciel, envoie des métriques (heartbeat),
récupère les commandes en attente et renvoie leurs résultats.
"""

# Version de l'agent. 1.1.1 : capture DXGI Desktop Duplication pour le mode
# non-assisté → corrige l'ÉCRAN NOIR à l'écran de connexion (GDI/mss ne capture
# pas le bureau sécurisé). 1.1.0 : prise de main non-assistée (helper SYSTEM).
# Un numéro supérieur déclenche l'auto-update des postes déjà enrôlés.
__version__ = "1.1.1"

# Nom du service Windows (référencé par service.py et install-service.ps1).
SERVICE_NAME = "TrueSightAgent"
