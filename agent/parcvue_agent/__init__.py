"""Paquet de l'agent Windows ParcVue.

L'agent interroge (poll) le serveur en HTTPS sortant ; il n'écoute jamais.
Il collecte l'inventaire matériel/logiciel, envoie des métriques (heartbeat),
récupère les commandes en attente et renvoie leurs résultats.
"""

# Version de l'agent (cf. SPEC : AGENT_VERSION = "1.0.0").
__version__ = "1.0.0"

# Nom du service Windows (référencé par service.py et install-service.ps1).
SERVICE_NAME = "ParcVueAgent"
