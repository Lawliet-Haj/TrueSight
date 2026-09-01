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
# NE PAS utiliser DETACHED_PROCESS (0x8) pour lancer le script de bascule :
# powershell.exe est une application CONSOLE, et son hôte 5.1 n'arrive pas à
# s'initialiser sans console. Le processus est bien créé (CreateProcess rend un
# PID, Popen ne lève pas) puis meurt AUSSITÔT sans exécuter une seule ligne —
# donc sans même écrire son journal. C'est ce qui rendait la panne indéchiffrable.
#
# Mesuré (01/09/2026, script témoin écrivant un fichier) :
#   DETACHED|NEW_GROUP|NO_WINDOW ... aucun témoin  (le script ne tourne pas)
#   DETACHED|NEW_GROUP ........... aucun témoin
#   NO_WINDOW|NEW_GROUP .......... témoin écrit
#   NEW_CONSOLE|NEW_GROUP ........ témoin écrit
#
# CREATE_NO_WINDOW suffit : le processus a une console, simplement pas de fenêtre
# visible — ce qui est le but. CREATE_NEW_PROCESS_GROUP l'isole des Ctrl+C/Break
# du parent, utile puisque ce parent (le service) va être arrêté par le script.
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

Log "=== Bascule de mise a jour : $NewDir -> $AppDir"
Start-Sleep -Seconds 2

# Arret COMPLET de l'agent. Reprend les correctifs valides sur l'installeur :
#  - DESACTIVER le service avant de l'arreter, sinon son action de reprise sur
#    echec (sc failure ... restart/5000) le RELANCE en pleine bascule et
#    reverrouille _internal\*.pyd ;
#  - taskkill /F /T plutot que Stop-Process : ce dernier echoue en silence sur
#    l'hote de service en etat "stop-pending" ;
#  - ATTENDRE la disparition effective des processus (handles liberes).
function Stop-AllAgent {
    & sc.exe config $ServiceName start= disabled | Out-Null
    Stop-Service -Name $ServiceName -Force
    $deadline = (Get-Date).AddSeconds(40)
    while ((Get-Service -Name $ServiceName).Status -ne "Stopped" -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    try { Stop-ScheduledTask -TaskName "TrueSight Companion" } catch {}
    if (Get-Process -Name "truesight-agent" -ErrorAction SilentlyContinue) {
        & taskkill.exe /F /T /IM "truesight-agent.exe" | Out-Null
    }
    for ($i = 0; $i -lt 40; $i++) {
        if (-not (Get-Process -Name "truesight-agent" -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    Log "ATTENTION : des processus truesight-agent subsistent"
    return $false
}

Log "1. Arret du service et des processus"
[void](Stop-AllAgent)

# 2. Sauvegarde de l'app courante (permet le rollback).
if (Test-Path $BackupDir) { Remove-Item -Recurse -Force $BackupDir }
Move-Item -Path $AppDir -Destination $BackupDir -Force
if (Test-Path $BackupDir) { Log "2. Sauvegarde OK" } else { Log "2. Sauvegarde IMPOSSIBLE (fichiers verrouilles ?)" }

# 3. Deploiement, avec re-essais : un fichier peut rester verrouille un instant
#    apres la fin du processus, ou etre tenu par l'antivirus.
$exe = Join-Path $AppDir "truesight-agent.exe"
$copied = $false
for ($attempt = 1; $attempt -le 4; $attempt++) {
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Copy-Item -Path (Join-Path $NewDir '*') -Destination $AppDir -Recurse -Force
    if (Test-Path $exe) { $copied = $true; break }
    Log "3. Copie tentative $attempt echouee, nouvel essai"
    [void](Stop-AllAgent)
    Start-Sleep -Seconds 3
}

if (-not $copied) {
    # Rien n'a pu etre deploye : on remet l'ancienne version en service.
    Log "3. ECHEC de la copie -> restauration de la version precedente"
    if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
    if (Test-Path $BackupDir) { Move-Item -Path $BackupDir -Destination $AppDir -Force }
    & sc.exe config $ServiceName start= auto | Out-Null
    Start-Service -Name $ServiceName
    Log "=== Bascule abandonnee (version precedente restauree)"
    exit 1
}
Log "3. Copie de la nouvelle version effectuee"

# 4. Recree le wrapper compagnon (chemin inchange).
$vbs = Join-Path $AppDir "companion.vbs"
Set-Content -Path $vbs -Value ('CreateObject("WScript.Shell").Run """' + $exe + '"" companion", 0, False') -Encoding ASCII

# 5. Redemarrage AVEC RE-ESSAIS. Juste apres une manipulation de service, le SCM
#    refuse souvent le 1er Start-Service : sans ces essais, on concluait a tort
#    a un echec et on faisait un ROLLBACK : la mise a jour ne prenait JAMAIS.
& sc.exe config $ServiceName start= auto | Out-Null
$started = $false
for ($i = 1; $i -le 6; $i++) {
    Start-Service -Name $ServiceName
    Start-Sleep -Seconds 3
    $svc = Get-Service -Name $ServiceName
    if ($svc -and $svc.Status -eq "Running") { $started = $true; break }
    Log "5. Demarrage tentative $i : etat $(if($svc){$svc.Status}else{'absent'})"
}

if (-not $started) {
    Log "5. Le service ne redemarre pas -> ROLLBACK"
    Remove-Item -Recurse -Force $AppDir
    Move-Item -Path $BackupDir -Destination $AppDir -Force
    & sc.exe config $ServiceName start= auto | Out-Null
    for ($i = 1; $i -le 6; $i++) {
        Start-Service -Name $ServiceName
        Start-Sleep -Seconds 3
        if ((Get-Service -Name $ServiceName).Status -eq "Running") { break }
    }
    Log "=== Rollback effectue (ancienne version en service)"
} else {
    Log "5. Service redemarre. Nettoyage de la sauvegarde."
    Remove-Item -Recurse -Force $BackupDir
    Log "=== Bascule REUSSIE"
}

# 6. Relance la tache compagnon pour les sessions ouvertes.
try { Start-ScheduledTask -TaskName "TrueSight Companion" } catch {}
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
        # utf-8-sig = UTF-8 AVEC BOM : indispensable ici. Le script est lancé par
        # « powershell.exe -File » (PowerShell 5.1), qui lit un fichier UTF-8
        # SANS BOM comme du cp1252 : le moindre caractère accentué corromprait
        # alors l'analyse du script et la bascule échouerait avant de commencer.
        # Le BOM rend le script correct quel que soit son contenu.
        with open(script_path, "w", encoding="utf-8-sig") as fh:
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
            creationflags=_NO_WINDOW | _NEW_GROUP,
            close_fds=True,
            cwd=update_root,
        )
    except Exception as exc:  # noqa: BLE001 - jamais bloquant pour l'agent.
        _logger.error("Lancement de la bascule de mise à jour impossible : %s", exc)
        return False

    return True
