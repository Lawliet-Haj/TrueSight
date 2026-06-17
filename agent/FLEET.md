# TrueSight Agent — déploiement sur le parc (production)

Procédure pour passer du poste pilote au déploiement de masse en **service Windows
SYSTEM** (élevé dès l'installation : CMD/PowerShell admin, démarrage auto, reprise
sur échec). Voir aussi `PERSISTENCE.md` (Mode B) et `install-service.ps1`.

## 1. Construire l'exécutable (sur un poste de build)

```powershell
cd agent
py -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # inclut pyinstaller, pywin32, pywinpty, mss, pillow…
.\build.ps1                              # -> dist\truesight-agent.exe (~19 Mo, --onefile)
.\dist\truesight-agent.exe --version     # doit afficher : TrueSight Agent <version>
```

> Point d'entrée du build : `run_agent.py` (import ABSOLU du paquet). **Ne pas**
> pointer PyInstaller sur `truesight_agent\__main__.py` directement (imports
> relatifs → « relative import with no known parent package »).

**Build validé le 2026-06-16** sur Windows 11 (Python 3.12) : l'exe démarre, mode
console (`--version`, `--enroll-only`) et mode service opérationnels.

## 2. Préparer le partage de déploiement

Sur un partage réseau en **lecture seule** pour « Ordinateurs du domaine » :
```
\\SERVEUR\Partage\TrueSight\truesight-agent.exe
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
.\install-service.ps1 -ExePath .\dist\truesight-agent.exe -ConfigPath .\config.ini
```

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

## Points à valider / à venir
- **Bureau à distance en session 0** : le service tourne en SYSTEM ; la capture du
  bureau utilisateur passe par un *helper* relancé dans la session interactive
  (`remote/launcher.py`, `CreateProcessAsUser`) — **à valider sur 1-2 postes** avant
  généralisation (le terminal et les commandes, eux, tournent en SYSTEM sans souci).
- **PyTurboJPEG** : non embarqué (encodage Pillow). Pour un remote plus rapide,
  décommenter `PyTurboJPEG`/`numpy` dans `requirements.txt` et fournir la DLL
  native `libjpeg-turbo` au build (PyInstaller `--add-binary`).
- **Auto-update intégré** : prévu (l'agent vérifierait/téléchargerait une nouvelle
  version depuis le serveur). En attendant, la MAJ passe par le partage GPO (§3.B).
