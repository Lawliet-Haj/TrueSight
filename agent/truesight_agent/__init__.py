"""Paquet de l'agent Windows TrueSight.

L'agent interroge (poll) le serveur en HTTPS sortant ; il n'écoute jamais.
Il collecte l'inventaire matériel/logiciel, envoie des métriques (heartbeat),
récupère les commandes en attente et renvoie leurs résultats.
"""

# Version de l'agent. 1.3.1 : connexion wss au relais ré-essayée (4 tentatives +
# backoff) — fiabilise la prise de main quand la connexion au relais est lente/
# instable depuis le poste. 1.3.0 : transfert de fichiers pendant la prise de main
# (explorateur, download trame 0x20, upload base64 ; droits de l'utilisateur
# connecté). 1.2.0 : collecte enrichie des correctifs Windows en attente (KB,
# titre, sévérité, taille, type, redémarrage requis). 1.1.3 : écoute du son
# système (WASAPI loopback). 1.1.2 : navigation à distance (curseur, verrou
# saisie, Ctrl+Alt+Suppr, lock sortie, écran de confidentialité). 1.1.1 : capture
# DXGI (écran noir au login). Un numéro supérieur déclenche l'auto-update.
__version__ = "1.3.1"

# Nom du service Windows (référencé par service.py et install-service.ps1).
SERVICE_NAME = "TrueSightAgent"
