# ============================================================================
# TrueSight - Déploiement de masse de l'agent (GPO / Intune) — build onedir
# ----------------------------------------------------------------------------
# Script de DÉMARRAGE machine (exécuté en SYSTEM au boot par une GPO « Scripts
# de démarrage », ou par Intune). Idempotent : il peut tourner à chaque boot.
#
#   1. déploie l'application (dossier onedir) dans C:\Program Files\TrueSight ;
#   2. pousse config.ini (une fois) dans C:\ProgramData\TrueSight (restreint) ;
#   3. installe le service "TrueSightAgent" (LocalSystem) s'il est absent ;
#   4. met à jour l'app si le partage contient une version différente ;
#   5. (re)démarre le service.
#
# Préparer le partage (lecture seule pour « Ordinateurs du domaine ») :
#   \\SERVEUR\Partage\TrueSight\truesight-agent\   (dossier onedir produit par build.ps1
#                                                   = dist\truesight-agent\)
#   \\SERVEUR\Partage\TrueSight\config.ini         (url + enrollment_token + verify_tls)
#
# GPO : Configuration ordinateur > Stratégies > Paramètres Windows > Scripts
#       (démarrage/arrêt) > Démarrage > Ajouter > gpo-install.ps1
#       Paramètre :  -Source \\SERVEUR\Partage\TrueSight
# ============================================================================

param(
    [string]$Source = "\\SERVEUR\Partage\TrueSight"
)

$ErrorActionPreference = "Stop"
$ServiceName = "TrueSightAgent"
$DataDir     = "C:\ProgramData\TrueSight"
$AppDir      = "C:\Program Files\TrueSight"
$srcApp = Join-Path $Source "truesight-agent"          # dossier onedir source
$srcCfg = Join-Path $Source "config.ini"
$srcExe = Join-Path $srcApp "truesight-agent.exe"
$dstExe = Join-Path $AppDir "truesight-agent.exe"
$dstCfg = Join-Path $DataDir "config.ini"

function Log($m) { Write-Host "[TrueSight GPO] $m" }

if (-not (Test-Path $srcExe)) { Log "App source introuvable : $srcExe"; exit 1 }

# --- Dossiers : données (restreint) + application (Program Files) --------------
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path $AppDir  | Out-Null
& icacls $DataDir /inheritance:r | Out-Null
& icacls $DataDir /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
# $AppDir : on garde les droits hérités de Program Files (Utilisateurs : R+X) —
# requis pour que le service relance le helper de bureau à distance en session user.

# --- config.ini : poussé une seule fois (ne pas écraser un existant local) ------
if ((-not (Test-Path $dstCfg)) -and (Test-Path $srcCfg)) {
    Copy-Item $srcCfg $dstCfg -Force
    Log "config.ini déployé."
}

# --- Faut-il (re)déployer l'app ? (absente ou exe différent) -------------------
$needCopy = $true
if (Test-Path $dstExe) {
    try {
        $ha = (Get-FileHash $srcExe -Algorithm SHA256).Hash
        $hb = (Get-FileHash $dstExe -Algorithm SHA256).Hash
        if ($ha -eq $hb) { $needCopy = $false }
    } catch { $needCopy = $true }
}

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if (-not $needCopy -and $svc -and $svc.Status -eq "Running") {
    Log "À jour et en cours d'exécution — rien à faire."
    exit 0
}

# --- Arrêt si nécessaire pour remplacer les fichiers (verrouillés si en cours) -
if ($svc -and $svc.Status -eq "Running" -and $needCopy) {
    Log "Arrêt du service pour mise à jour…"
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

if ($needCopy) {
    # Arrête le compagnon (sinon ses _internal\*.pyd verrouillent la copie).
    try { Stop-ScheduledTask -TaskName "TrueSight Companion" -ErrorAction SilentlyContinue } catch {}
    Get-Process -Name "truesight-agent" -ErrorAction SilentlyContinue |
        Where-Object { $_.SessionId -ne 0 } | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Log "Déploiement de l'application dans $AppDir…"
    Get-ChildItem -Path $AppDir -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item -Path (Join-Path $srcApp '*') -Destination $AppDir -Recurse -Force
}

# --- Installation du service s'il est absent ----------------------------------
if (-not $svc) {
    Log "Installation du service…"
    & $dstExe --startup auto install
    & sc.exe config $ServiceName obj= "LocalSystem" | Out-Null
    & sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null
}

# --- Tâche « compagnon » (session utilisateur : terminal + bureau à distance) ---
# (Re)créée à chaque passage (idempotent). Wrapper VBS pour une fenêtre cachée.
$vbs = Join-Path $AppDir "companion.vbs"
$vbsContent = 'CreateObject("WScript.Shell").Run """' + $dstExe + '"" companion", 0, False'
Set-Content -Path $vbs -Value $vbsContent -Encoding ASCII
try {
    $compAction   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"' + $vbs + '"')
    $compTrigger  = New-ScheduledTaskTrigger -AtLogOn
    $compSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -Hidden
    $compPrincipal = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited
    Register-ScheduledTask -TaskName "TrueSight Companion" -Action $compAction -Trigger $compTrigger `
        -Settings $compSettings -Principal $compPrincipal -Force | Out-Null
    Log "Tâche compagnon installée/à jour."
} catch {
    Log "AVERTISSEMENT : tâche compagnon non installée ($($_.Exception.Message))."
}

# --- Démarrage -----------------------------------------------------------------
Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Log "Service en cours d'exécution."
    exit 0
}
Log "AVERTISSEMENT : service non démarré (état : $(if ($svc) { $svc.Status } else { 'absent' }))."
exit 1
