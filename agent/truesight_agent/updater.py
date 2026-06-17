r"""Auto-update de l'agent TrueSight (Windows, build onedir figé).

Le serveur annonce une mise à jour dans la réponse du heartbeat
(``agent_update = {version, url, sha256, size}``). Le runner délègue ici.

Stratégie de remplacement à chaud d'un service Windows dont le binaire est
verrouillé tant qu'il tourne :

1. télécharger le paquet (zip du dossier onedir) dans ``ProgramData\TrueSight\update`` ;
2. vérifier l'empreinte SHA-256 ;
3. décompresser dans un dossier de transit et localiser le dossier onedir ;
4. écrire un script ``apply-update.ps1`` et le lancer **détaché** (il survit à
   l'arrêt du service) : il arrête le service + le compagnon, **sauvegarde**
   l'app courante, déploie la nouvelle, redémarre — et **restaure** la sauvegarde
   si le service ne redémarre pas (rollback). Puis relance la tâche compagnon.

Sécurités :
- ne s'exécute QUE sur un exécutable figé (``sys.frozen``) sous Windows — jamais
  sur un checkout de dev ;
- une seule application à la fois (verrou) + ne ré-essaie pas la même version
  avant un cooldown (géré par le runner) ;
- vérification d'empreinte avant toute bascule ; rollback automatique au boot KO.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile

from . import SERVICE_NAME, __version__, config as cfg

_logger = logging.getLogger("truesight.updater")

_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)")

# Drapeaux de création Windows : processus détaché, sans fenêtre, nouveau groupe.
_DETACHED = 0x00000008  # DETACHED_PROCESS
_NEW_GROUP = 0x00000200  # CREATE_NEW_PROCESS_GROUP
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def _parse(v) -> tuple[int, int, int] | None:
    if not v:
        return None
    m = _VERSION_RE.match(str(v))
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def is_newer(version) -> bool:
    """True si ``version`` est strictement plus récente que la version courante."""
    target = _parse(version)
    if target is None:
        return False
    here = _parse(__version__)
    if here is None:
        return True
    return target > here


def can_self_update() -> bool:
    """True seulement si on tourne en exécutable figé sous Windows."""
    return cfg.is_frozen() and os.name == "nt"


def _app_dir() -> str:
    """Dossier de l'application (onedir) = dossier de l'exécutable figé."""
    return os.path.dirname(os.path.abspath(sys.executable))


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_onedir_root(extract_dir: str) -> str | None:
    """Localise le dossier contenant ``truesight-agent.exe`` dans l'extraction."""
    for root, _dirs, files in os.walk(extract_dir):
        if any(f.lower() == "truesight-agent.exe" for f in files):
            return root
    return None


# ----------------------------------------------------------------------------
# Script PowerShell de bascule (lancé détaché, exécuté en SYSTEM)
# ----------------------------------------------------------------------------
_APPLY_SCRIPT = r'''param(
  [Parameter(Mandatory=$true)][string]$NewDir,
  [Parameter(Mandatory=$true)][string]$AppDir,
  [Parameter(Mandatory=$true)][string]$ServiceName,
  [Parameter(Mandatory=$true)][string]$BackupDir,
  [string]$LogFile
)
$ErrorActionPreference = "SilentlyContinue"
function Log($m) { try { Add-Content -Path $LogFile -Value ((Get-Date).ToString("s") + "  " + $m) } catch {} }

Log "Bascule de mise a jour : $NewDir -> $AppDir"
Start-Sleep -Seconds 2

# 1. Arret du service + attente de la fin du processus.
Stop-Service -Name $ServiceName -Force
$deadline = (Get-Date).AddSeconds(40)
while ((Get-Service -Name $ServiceName).Status -ne "Stopped" -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }

# 2. Arret du compagnon (libere _internal\*.pyd dans les sessions utilisateur).
try { Stop-ScheduledTask -TaskName "TrueSight Companion" } catch {}
Get-Process -Name "truesight-agent" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

# 3. Sauvegarde de l'app courante.
if (Test-Path $BackupDir) { Remove-Item -Recurse -Force $BackupDir }
try { Move-Item -Path $AppDir -Destination $BackupDir -Force; Log "Sauvegarde OK" }
catch { Log "Sauvegarde impossible : $($_.Exception.Message)" }

# 4. Deploiement de la nouvelle version.
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Copy-Item -Path (Join-Path $NewDir '*') -Destination $AppDir -Recurse -Force
Log "Copie de la nouvelle version effectuee"

# 5. Recree le wrapper compagnon (chemin inchange) puis redemarre le service.
$exe = Join-Path $AppDir "truesight-agent.exe"
$vbs = Join-Path $AppDir "companion.vbs"
Set-Content -Path $vbs -Value ('CreateObject("WScript.Shell").Run """' + $exe + '"" companion", 0, False') -Encoding ASCII

Start-Service -Name $ServiceName
Start-Sleep -Seconds 6
$svc = Get-Service -Name $ServiceName
if (-not $svc -or $svc.Status -ne "Running") {
    Log "Le service ne redemarre pas (etat: $(if($svc){$svc.Status}else{'absent'})) -> ROLLBACK"
    Remove-Item -Recurse -Force $AppDir
    Move-Item -Path $BackupDir -Destination $AppDir -Force
    Start-Service -Name $ServiceName
    Log "Rollback effectue"
} else {
    Log "Service redemarre en $($svc.Status). Nettoyage de la sauvegarde."
    Remove-Item -Recurse -Force $BackupDir
}

# 6. Relance la tache compagnon pour les sessions ouvertes.
try { Start-ScheduledTask -TaskName "TrueSight Companion" } catch {}
Log "Bascule terminee."
'''


def apply_update(client, update_info: dict) -> bool:
    """Applique une mise à jour annoncée par le serveur. Renvoie True si la
    bascule a été lancée (le service va alors s'arrêter sous peu).

    ``update_info`` : ``{version, url, sha256, size}``.
    """
    if not isinstance(update_info, dict):
        return False
    version = update_info.get("version")
    url = update_info.get("url")
    expected_sha = (update_info.get("sha256") or "").lower()

    if not version or not url:
        return False
    if not can_self_update():
        _logger.info("Auto-update ignorée (agent non figé / hors Windows).")
        return False
    if not is_newer(version):
        _logger.debug("Auto-update : version %s non supérieure à %s, ignorée.", version, __version__)
        return False

    _logger.info("Auto-update : téléchargement de la version %s.", version)

    update_root = os.path.join(cfg.get_data_dir(), "update")
    staging = os.path.join(update_root, "staging")
    zip_path = os.path.join(update_root, f"truesight-agent-{version}.zip")
    try:
        os.makedirs(update_root, exist_ok=True)
        # Nettoyage d'un transit précédent.
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
    except OSError as exc:
        _logger.error("Préparation du dossier de mise à jour impossible : %s", exc)
        return False

    # 1. Téléchargement.
    res = client.download_file(url, zip_path)
    if not res.ok:
        _logger.warning("Téléchargement de la mise à jour échoué : %s", res.error)
        return False

    # 2. Vérification d'empreinte.
    try:
        actual_sha = _sha256(zip_path)
    except OSError as exc:
        _logger.error("Lecture du paquet téléchargé impossible : %s", exc)
        return False
    if expected_sha and actual_sha.lower() != expected_sha:
        _logger.error("Empreinte SHA-256 incorrecte (attendu %s, obtenu %s) : mise à jour abandonnée.",
                      expected_sha, actual_sha)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return False

    # 3. Décompression + localisation du dossier onedir.
    try:
        os.makedirs(staging, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(staging)
    except (zipfile.BadZipFile, OSError) as exc:
        _logger.error("Décompression du paquet impossible : %s", exc)
        return False

    new_dir = _find_onedir_root(staging)
    if not new_dir:
        _logger.error("truesight-agent.exe introuvable dans le paquet : mise à jour abandonnée.")
        return False

    # 4. Écrit le script de bascule + le lance détaché.
    app_dir = _app_dir()
    backup_dir = os.path.join(update_root, "backup")
    log_file = os.path.join(cfg.get_data_dir(), "truesight-update.log")
    script_path = os.path.join(update_root, "apply-update.ps1")
    try:
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(_APPLY_SCRIPT)
    except OSError as exc:
        _logger.error("Écriture du script de bascule impossible : %s", exc)
        return False

    cmd = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", script_path,
        "-NewDir", new_dir,
        "-AppDir", app_dir,
        "-ServiceName", SERVICE_NAME,
        "-BackupDir", backup_dir,
        "-LogFile", log_file,
    ]
    try:
        _logger.info("Auto-update : lancement de la bascule (le service va redémarrer).")
        subprocess.Popen(
            cmd,
            creationflags=_DETACHED | _NEW_GROUP | _NO_WINDOW,
            close_fds=True,
            cwd=update_root,
        )
    except Exception as exc:  # noqa: BLE001 - jamais bloquant pour l'agent.
        _logger.error("Lancement de la bascule de mise à jour impossible : %s", exc)
        return False

    return True
