"""Paquet de l'agent Windows TrueSight.

L'agent interroge (poll) le serveur en HTTPS sortant ; il n'écoute jamais.
Il collecte l'inventaire matériel/logiciel, envoie des métriques (heartbeat),
récupère les commandes en attente et renvoie leurs résultats.
"""

# Version de l'agent. 1.4.5 : CORRECTIF AUTO-UPDATE — le script de bascule ne
# recevait pas les garde-fous validés sur l'installeur : Start-Service sans
# ré-essais (le SCM refuse souvent le 1er démarrage) faisait conclure à un échec
# et déclenchait un ROLLBACK silencieux, donc la mise à jour ne prenait jamais.
# Ajout : service désactivé avant arrêt (l'action de reprise le relançait en
# pleine bascule), taskkill /F /T, attente des processus, copie et démarrage
# ré-essayés, journal explicite, script écrit avec BOM UTF-8.
# 1.4.4 : long-polling des commandes (le serveur relâche
# l'agent dès qu'il y a du travail → prise en main quasi immédiate ; repli
# automatique sur le sondage simple si un intermédiaire coupe les requêtes longues).
# 1.4.3 : presse-papiers partagé (texte) pendant le bureau à
# distance — lecture/écriture du presse-papiers du poste (CF_UNICODETEXT).
# 1.4.2 : encodage JPEG accéléré — turbojpeg.dll (libjpeg-turbo)
# EMBARQUÉE dans le paquet (avant : repli Pillow 5-10× plus lent → latence et CPU
# inutiles sur le bureau à distance). 1.4.1 : décodage UTF-8 des sorties PowerShell des collecteurs
# (services + Defender) — corrige un UnicodeDecodeError (cp1252) sur les noms de
# services accentués. 1.4.0 : collecte de l'état des services Windows au heartbeat
# (supervision + auto-remédiation côté serveur). 1.3.2 : préférence IPv4 pour les connexions
# (corrige les échecs ~50 % quand le serveur est en double pile A+AAAA et que l'IPv6
# du poste est cassée — cause de l'« écran noir » intermittent). 1.3.1 : connexion
# wss au relais ré-essayée (4 tentatives + backoff). 1.3.0 : transfert de fichiers
# (explorateur, download trame 0x20, upload base64 ; droits de l'utilisateur
# 1.4.7 : l'auto-update pouvait enfin s'appliquer — le script de bascule etait
# lance avec DETACHED_PROCESS, ce qui prive powershell.exe de console : le
# processus mourait sans executer une ligne (donc sans journal). Depuis toujours.
# 1.4.6 : CORRECTIF MAJEUR de la prise en main — la socket TLS n'est plus lue
# et écrite par deux threads à la fois (OpenSSL ne le supporte pas : l'état TLS
# se corrompait et la session tombait au hasard, de 5 s à quelques minutes).
# Ajoute aussi le bridage automatique selon le débit réellement obtenu et un cap
# de résolution sur le plus grand côté (les écrans portrait n'étaient pas réduits).
# connecté). 1.2.0 : collecte enrichie des correctifs Windows en attente (KB,
# titre, sévérité, taille, type, redémarrage requis). 1.1.3 : écoute du son
# système (WASAPI loopback). 1.1.2 : navigation à distance (curseur, verrou
# saisie, Ctrl+Alt+Suppr, lock sortie, écran de confidentialité). 1.1.1 : capture
# DXGI (écran noir au login). Un numéro supérieur déclenche l'auto-update.
__version__ = "1.4.7"

# Nom du service Windows (référencé par service.py et install-service.ps1).
SERVICE_NAME = "TrueSightAgent"
