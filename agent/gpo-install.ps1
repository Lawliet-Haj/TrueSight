# ============================================================================
# TrueSight - Déploiement de masse de l'agent (GPO / Intune)
# ----------------------------------------------------------------------------
# Script de DÉMARRAGE machine (exécuté en SYSTEM au boot par une GPO « Scripts
# de démarrage », ou par Intune). Idempotent : il peut tourner à chaque boot.
#
#   1. copie truesight-agent.exe + config.ini depuis un partage réseau ;
#   2. installe le service "TrueSightAgent" (LocalSystem) s'il est absent ;
#   3. met à jour l'exe si le partage en contient une version différente
#      (mécanisme de mise à jour simple en attendant l'auto-update intégré) ;
#   4. (ré)démarre le service.
#
# Préparer le partage (lecture seule pour « Ordinateurs du domaine ») :
#   \\SERVEUR\Partage\TrueSight\truesight-agent.exe   (produit par build.ps1)
#   \\SERVEUR\Partage\TrueSight\config.ini            (url + enrollment_token + verify_tls)
#
# GPO : Configuration ordinateur > Stratégies > Paramètres Windows > Scripts
#       (démarrage/arrêt) > Démarrage > Ajouter > gpo-install.ps1
#       avec le paramètre :  -Source \\SERVEUR\Partage\TrueSight
# ============================================================================

param(
    [string]$Source = "\\SERVEUR\Partage\TrueSight"
)

$ErrorActionPreference = "Stop"
$ServiceName = "TrueSightAgent"
$DataDir     = "C:\ProgramData\TrueSight"
$srcExe = Join-Path $Source "truesight-agent.exe"
$srcCfg = Join-Path $Source "config.ini"
$dstExe = Join-Path $DataDir "truesight-agent.exe"
$dstCfg = Join-Path $DataDir "config.ini"

function Log($m) { Write-Host "[TrueSight GPO] $m" }

if (-not (Test-Path $srcExe)) { Log "Exe source introuvable : $srcExe"; exit 1 }

# --- Dossier de données + ACL (SYSTEM + Administrateurs uniquement) -----------
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
& icacls $DataDir /inheritance:r | Out-Null
& icacls $DataDir /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

# --- config.ini : poussé une seule fois (ne pas écraser un existant local) -----
if ((-not (Test-Path $dstCfg)) -and (Test-Path $srcCfg)) {
    Copy-Item $srcCfg $dstCfg -Force
    Log "config.ini déployé."
}

# --- Faut-il copier l'exe ? (absent ou version différente) --------------------
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

# --- Arrêt si nécessaire pour remplacer l'exe (fichier verrouillé si en cours) -
if ($svc -and $svc.Status -eq "Running" -and $needCopy) {
    Log "Arrêt du service pour mise à jour…"
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

if ($needCopy) {
    Copy-Item $srcExe $dstExe -Force
    Log "Exe déployé / mis à jour."
}

# --- Installation du service s'il est absent ----------------------------------
if (-not $svc) {
    Log "Installation du service…"
    & $dstExe --startup auto install
    & sc.exe config $ServiceName obj= "LocalSystem" | Out-Null
    & sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null
}

# --- Démarrage -----------------------------------------------------------------
Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Log "Service en cours d'exécution."
    exit 0
}
Log "AVERTISSEMENT : le service n'est pas démarré (état : $(if ($svc) { $svc.Status } else { 'absent' }))."
exit 1
