# TrueSight Agent — déploiement sur le parc (production)

Procédure pour passer du poste pilote au déploiement de masse en **service Windows
SYSTEM** (élevé dès l'installation : CMD/PowerShell admin, démarrage auto, reprise
sur échec). Voir aussi `PERSISTENCE.md` (Mode B) et `install-service.ps1`.

## 1. Construire l'exécutable (sur un poste de build)

```powershell
cd agent
py -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt              # inclut pyinstaller, pywin32, pywinpty, mss, pillow…
.\build.ps1                                  # -> dist\truesight-agent\ (dossier onedir)
.\dist\truesight-agent\truesight-agent.exe --version   # doit afficher : TrueSight Agent <version>
```

> **`--onedir`** (et NON `--onefile`) : produit un **dossier** `dist\truesight-agent\`
> (exe + `_internal\`). Pas d'extraction temporaire au lancement → fiable quand le
> service SYSTEM relance le helper de bureau à distance dans la session utilisateur
> (l'onefile échouait dans ce contexte → écran noir).
>
> Point d'entrée : `run_agent.py` (import ABSOLU). **Ne pas** pointer PyInstaller
> sur `truesight_agent\__main__.py` directement (imports relatifs → erreur).

**Build validé le 2026-06-17** sur Windows 11 (Python 3.12) : exe console
(`--version`/`--enroll-only`), mode service, et helper de bureau à distance OK.

## 1bis. Structure d'installation (séparation app / données)
- **Application** → `C:\Program Files\TrueSight\` (dossier onedir, droits hérités
  = Utilisateurs en lecture+exécution → le service peut relancer le helper en
  session utilisateur).
- **Données** → `C:\ProgramData\TrueSight\` (config.ini, state.json, logs ;
  **restreint SYSTEM+Administrateurs**, car state.json contient le token agent).

## 1ter. Architecture « service + compagnon »
- **Service SYSTEM** (`TrueSightAgent`) = unique agent enrôlé : supervision +
  commandes/scripts/actions rapides (en SYSTEM, admin total).
- **Compagnon** (tâche planifiée `TrueSight Companion`, au logon, session de chaque
  utilisateur, droits limités) = exécute les sessions **interactives** (terminal +
  bureau à distance) DANS la session de l'utilisateur, où ConPTY et la capture sont
  fiables. `install-service.ps1` / `gpo-install.ps1` créent cette tâche (lancée
  cachée via `companion.vbs` → `truesight-agent.exe companion`).
- Le service pilote le compagnon par un **named pipe** local (`\\.\pipe\TrueSightRemoteSession`) :
  il lui pousse `{token, ws_url, kind, shell, verify_tls}`. Le compagnon ne s'enrôle
  pas et n'interroge pas le serveur → un seul jeton, aucun conflit. Si aucun
  compagnon n'écoute (personne de connecté), le service se rabat sur l'ancien
  helper `CreateProcessAsUser` (bureau à distance OK).
- Log compagnon : `%LOCALAPPDATA%\TrueSight\companion.log`.

## 2. Préparer le partage de déploiement

Sur un partage réseau en **lecture seule** pour « Ordinateurs du domaine » :
```
\\SERVEUR\Partage\TrueSight\truesight-agent\   (= le dossier onedir dist\truesight-agent\)
\\SERVEUR\Partage\TrueSight\config.ini
```
`config.ini` (poussé une fois par poste) :
```ini
[server]
url = https://srv778935.hstgr.cloud
enrollment_token = <ENROLLMENT_TOKEN du .env serveur>
verify_tls = true

[agent]
heartbeat_interval = 30
command_poll_interval = 8
inventory_interval_hours = 12
```

## 3. Déployer (deux options)

**A. Manuel / quelques postes** — sur le poste cible, PowerShell **admin** :
```powershell
.\install-service.ps1 -ExePath .\dist\truesight-agent\truesight-agent.exe -ConfigPath .\config.ini
```
(copie tout le dossier onedir dans `C:\Program Files\TrueSight`, installe le
service LocalSystem, démarre.)

**B. Parc entier (GPO / Intune)** — script de **démarrage machine** (exécuté SYSTEM) :
```
gpo-install.ps1 -Source \\SERVEUR\Partage\TrueSight
```
Idempotent : installe le service s'il manque, met à jour l'exe si le partage en a
une version différente (mécanisme de MAJ simple), (re)démarre le service. Le
`config.ini` n'est poussé qu'une fois (un fichier local existant est conservé).

## 4. Vérifier
- `Get-Service TrueSightAgent` → `Running` ;
- le poste apparaît **en ligne** dans le dashboard sous ~30 s ;
- `C:\ProgramData\TrueSight\truesight-agent.log` pour le diagnostic.

## 5. Déploiement « zéro fichier » par le dashboard (lien d'installation)

Alternative au partage réseau : depuis le dashboard, **Réglages > Déploiement &
mises à jour > Générer un lien** (admin / superadmin). On obtient une commande à
lancer dans un **PowerShell administrateur** sur le poste cible :

```powershell
powershell -ExecutionPolicy Bypass -Command "iwr -useb https://srv778935.hstgr.cloud/install.ps1?t=<jeton> | iex"
```

Le script télécharge le paquet courant + un `config.ini` (URL serveur +
`enrollment_token`, servi par HTTPS contre le jeton — il n'apparaît jamais dans
l'URL copiée), installe le service SYSTEM + la tâche compagnon et démarre. Le lien
est **expirable** et **révocable** (Réglages), et chaque installation est auditée
(`install.config`).

## 6. Publier un paquet & auto-update

1. `build.ps1` produit `dist\truesight-agent-<version>.zip` (avec `version.txt` +
   SHA-256 affiché).
2. **Réglages > Déploiement & mises à jour > Téléverser** (superadmin) : déposer
   ce .zip ; il devient la **version courante** (servie au lien d'installation
   ET à l'auto-update).
3. Les agents enrôlés détectent la nouvelle version au heartbeat (champ
   `agent_update`), téléchargent le paquet (Bearer agent), vérifient le SHA-256,
   puis basculent via un script détaché qui **sauvegarde** l'app, déploie la
   nouvelle, redémarre le service — et **restaure** automatiquement (rollback) si
   le service ne repart pas. Log : `C:\ProgramData\TrueSight\truesight-update.log`.

> Geler le parc : `AGENT_AUTO_UPDATE_ENABLED=false` (env serveur) → plus aucune
> annonce de mise à jour. La version courante est stockée sur le volume Docker
> `agent_releases` (`/var/lib/truesight/releases`).
>
> Pour monter de version : éditer `truesight_agent/__init__.py` (`__version__`)
> AVANT `build.ps1` — c'est la version comparée pour décider de la bascule (et
> écrite dans `version.txt`).

## 7. Encodage accéléré (PyTurboJPEG, optionnel)

`requirements.txt` inclut `PyTurboJPEG` + `numpy`. Si la DLL native
`turbojpeg.dll` (libjpeg-turbo) est présente sur le poste de **build** (installer
libjpeg-turbo, ou définir `TURBOJPEG_DLL`), `build.ps1` l'embarque (`--add-binary`)
et l'agent l'utilise en priorité (encodage ~5-10× plus rapide). Sinon, repli
automatique sur Pillow — le build reste valide. Au runtime, l'agent localise la
DLL embarquée (`_internal\turbojpeg.dll`) sans configuration.

## Points à valider / à venir
- **Bureau à distance en session 0** : le service (SYSTEM) relance un *helper* dans
  la session interactive (`remote/launcher.py`, `CreateProcessAsUser`) ou pilote le
  compagnon. Validé. (Terminal et commandes tournent en SYSTEM sans helper.)
- **Auto-update** : validé côté serveur (publication, version courante, annonce,
  téléchargement Bearer — couvert par les tests). La bascule + rollback (script
  détaché SYSTEM) sont à valider sur le poste pilote à la première montée de
  version réelle.
