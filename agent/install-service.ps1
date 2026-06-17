# ============================================================================
# TrueSight - Installation de l'agent Windows en service
# ----------------------------------------------------------------------------
# Ce script (à exécuter en ADMINISTRATEUR) :
#   1. crée le dossier C:\ProgramData\TrueSight ;
#   2. y copie l'exécutable truesight-agent.exe et le fichier config.ini ;
#   3. installe le service Windows "TrueSightAgent" (compte LocalSystem / SYSTEM) ;
#   4. configure le redémarrage automatique en cas d'échec ;
#   5. démarre le service.
#
# Paramètres :
#   -ExePath   Chemin de l'exécutable (défaut : .\dist\truesight-agent.exe)
#   -ConfigPath Chemin du config.ini source (défaut : .\config.ini puis config.example.ini)
#
# Exemple :
#   .\install-service.ps1
#   .\install-service.ps1 -ExePath "C:\build\truesight-agent.exe" -ConfigPath "C:\gpo\config.ini"
# ============================================================================

param(
    [string]$ExePath,
    [string]$ConfigPath
)

# Arrête le script à la première erreur.
$ErrorActionPreference = "Stop"

# --- Constantes ----------------------------------------------------------------
$ServiceName = "TrueSightAgent"
$DataDir     = "C:\ProgramData\TrueSight"     # données (restreint SYSTEM+Admins)
$AppDir      = "C:\Program Files\TrueSight"   # binaires (lecture+exécution pour tous)
$scriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition

# --- 1. Vérifie les droits administrateur -------------------------------------
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal   = New-Object Security.Principal.WindowsPrincipal($currentUser)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Ce script doit être exécuté en tant qu'administrateur." -ForegroundColor Red
    exit 1
}

Write-Host "=== Installation de l'agent TrueSight ===" -ForegroundColor Cyan

# --- 2. Résout les chemins source ---------------------------------------------
if (-not $ExePath) {
    # Build onedir : l'exe se trouve dans dist\truesight-agent\.
    $ExePath = Join-Path $scriptDir "dist\truesight-agent\truesight-agent.exe"
}
if (-not (Test-Path $ExePath)) {
    Write-Host "Exécutable introuvable : $ExePath" -ForegroundColor Red
    Write-Host "Lancer d'abord .\build.ps1 pour produire dist\truesight-agent\." -ForegroundColor Yellow
    exit 1
}

if (-not $ConfigPath) {
    # On privilégie un config.ini déjà renseigné, sinon l'exemple.
    $candidate = Join-Path $scriptDir "config.ini"
    if (Test-Path $candidate) {
        $ConfigPath = $candidate
    } else {
        $ConfigPath = Join-Path $scriptDir "config.example.ini"
    }
}
if (-not (Test-Path $ConfigPath)) {
    Write-Host "Fichier de configuration introuvable : $ConfigPath" -ForegroundColor Red
    exit 1
}

# --- 3. Dossiers : données (restreint) + application (Program Files) -----------
Write-Host "Création des dossiers..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path $AppDir  | Out-Null

# Données restreintes : SYSTEM + Administrateurs uniquement (le state.json contient
# le token de l'agent ; en cas de repli DPAPI indisponible il serait en clair).
Write-Host "Restriction des droits d'accès au dossier de données..." -ForegroundColor Yellow
& icacls $DataDir /inheritance:r | Out-Null
& icacls $DataDir /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
# $AppDir hérite des droits de « Program Files » (Utilisateurs : lecture+exécution).
# On ne le restreint PAS : c'est indispensable pour que le service (SYSTEM) puisse
# relancer le helper de bureau à distance dans la session utilisateur.

$targetExe    = Join-Path $AppDir  "truesight-agent.exe"
$targetConfig = Join-Path $DataDir "config.ini"
$srcAppDir    = Split-Path -Parent $ExePath   # dossier onedir (dist\truesight-agent)

# --- 4. Arrête / supprime un service existant AVANT de copier ------------------
# (sinon les fichiers de l'application sont verrouillés par le service en cours).
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service existant détecté : arrêt et suppression..." -ForegroundColor Yellow
    if ($existing.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $targetExe) { & $targetExe remove | Out-Null }
    # Suppression robuste par nom (couvre un ancien service dont l'exe était
    # ailleurs, ex. migration depuis C:\ProgramData\TrueSight vers Program Files).
    & sc.exe delete $ServiceName 2>$null | Out-Null
    Start-Sleep -Seconds 2
}

# --- 4bis. Arrête le COMPAGNON (sinon _internal\*.pyd verrouillés) -------------
Write-Host "Arrêt du compagnon (sessions utilisateur)..." -ForegroundColor Yellow
try { Stop-ScheduledTask -TaskName "TrueSight Companion" -ErrorAction SilentlyContinue } catch {}
Get-Process -Name "truesight-agent" -ErrorAction SilentlyContinue |
    Where-Object { $_.SessionId -ne 0 } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# --- 5. Déploie l'application (dossier onedir complet) + la configuration -------
Write-Host "Déploiement de l'application dans $AppDir..." -ForegroundColor Yellow
Get-ChildItem -Path $AppDir -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item -Path (Join-Path $srcAppDir '*') -Destination $AppDir -Recurse -Force

if (-not (Test-Path $targetExe)) {
    Write-Host "Exécutable introuvable après copie : $targetExe" -ForegroundColor Red
    exit 1
}

# config.ini → dossier de données (restreint). On n'écrase PAS un existant (GPO).
if (-not (Test-Path $targetConfig)) {
    Write-Host "Copie de la configuration..." -ForegroundColor Yellow
    Copy-Item -Path $ConfigPath -Destination $targetConfig -Force
} else {
    Write-Host "config.ini déjà présent dans $DataDir : conservé." -ForegroundColor Yellow
}

# --- 5. Installe le service via l'exécutable (pywin32) -------------------------
# L'exe gère lui-même 'install' (HandleCommandLine de win32serviceutil).
# --startup auto : démarrage automatique au boot.
Write-Host "Installation du service $ServiceName..." -ForegroundColor Yellow
& $targetExe --startup auto install
if ($LASTEXITCODE -ne 0) {
    Write-Host "Échec de l'installation du service (code $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}

# --- 6. Compte du service : LocalSystem (SYSTEM) -------------------------------
# Nécessaire pour l'inventaire complet et l'exécution des commandes (cf. DESIGN 6.4).
Write-Host "Configuration du compte de service (LocalSystem)..." -ForegroundColor Yellow
& sc.exe config $ServiceName obj= "LocalSystem" | Out-Null

# --- 7. Redémarrage automatique en cas d'échec --------------------------------
# reset= 86400 : réinitialise le compteur d'échecs après 1 jour.
# actions=restart/5000 : redémarre 5 s après chaque échec (3 tentatives).
Write-Host "Configuration de la reprise sur échec..." -ForegroundColor Yellow
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null

# --- 7bis. Tâche « compagnon » en session utilisateur -------------------------
# Le service (SYSTEM) ne peut pas lancer de terminal interactif fiable en session 0.
# Un compagnon tourne donc dans la session de CHAQUE utilisateur (au logon) et
# exécute les sessions interactives (terminal + bureau à distance) ; le service
# le pilote via un named pipe local. Fenêtre cachée via un wrapper VBS (l'exe est
# une appli console).
Write-Host "Installation de la tâche compagnon (session utilisateur)..." -ForegroundColor Yellow
$vbs = Join-Path $AppDir "companion.vbs"
$vbsContent = 'CreateObject("WScript.Shell").Run """' + $targetExe + '"" companion", 0, False'
Set-Content -Path $vbs -Value $vbsContent -Encoding ASCII

try {
    $compAction   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"' + $vbs + '"')
    $compTrigger  = New-ScheduledTaskTrigger -AtLogOn
    $compSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -Hidden
    # Groupe « Utilisateurs » (S-1-5-32-545) : s'exécute pour tout utilisateur au
    # logon, DANS sa session, en droits limités.
    $compPrincipal = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited
    Register-ScheduledTask -TaskName "TrueSight Companion" -Action $compAction -Trigger $compTrigger `
        -Settings $compSettings -Principal $compPrincipal -Force | Out-Null
    Write-Host "Tâche compagnon installée (démarre au prochain logon)." -ForegroundColor Green
} catch {
    Write-Host "AVERTISSEMENT : tâche compagnon non installée ($($_.Exception.Message))." -ForegroundColor Yellow
}

# --- 8. Démarre le service -----------------------------------------------------
Write-Host "Démarrage du service..." -ForegroundColor Yellow
Start-Service -Name $ServiceName

Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host "=== Service TrueSight installé et démarré ===" -ForegroundColor Green
    Write-Host "Dossier de données : $DataDir" -ForegroundColor Green
    Write-Host "Journal : $DataDir\truesight-agent.log" -ForegroundColor Green
} else {
    $status = if ($svc) { $svc.Status } else { "absent" }
    Write-Host "ÉCHEC : le service est installé mais son état est : $status" -ForegroundColor Red
    Write-Host "Consulter $DataDir\truesight-agent.log pour le diagnostic." -ForegroundColor Yellow
    exit 1
}
