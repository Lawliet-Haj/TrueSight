# ============================================================================
# TrueSight - Post-installation (appelé par l'installeur Inno Setup)
# ----------------------------------------------------------------------------
# Les fichiers de l'agent sont DÉJÀ déposés dans $AppDir par l'installeur. Ce
# script ne fait QUE la configuration système : config.ini (ProgramData
# restreint), service Windows SYSTEM + reprise sur échec, tâche compagnon, et
# démarrage. Exécuté élevé (l'installeur exige les droits admin).
# ============================================================================
param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [string]$ServerUrl = "",
    [string]$Token = "",
    [string]$VerifyTls = "true"
)

$ErrorActionPreference = "Stop"
$ServiceName = "TrueSightAgent"
$DataDir     = "C:\ProgramData\TrueSight"
$exe         = Join-Path $AppDir "truesight-agent.exe"

function Log($m) { Write-Host "[TrueSight postinstall] $m" }

if (-not (Test-Path $exe)) {
    Write-Error "truesight-agent.exe introuvable dans $AppDir."
    exit 1
}

# --- 1. Dossier de données restreint (SYSTEM + Administrateurs) ---------------
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
& icacls $DataDir /inheritance:r | Out-Null
& icacls $DataDir /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

# --- 2. config.ini ------------------------------------------------------------
# Écrit depuis les paramètres si fournis (URL + jeton) ; sinon on CONSERVE un
# config.ini existant (réinstallation / mise à jour sans re-saisie).
$cfgPath = Join-Path $DataDir "config.ini"
if ($ServerUrl -and $Token) {
    $cfg = @"
[server]
url = $ServerUrl
enrollment_token = $Token
verify_tls = $VerifyTls

[agent]
heartbeat_interval = 30
command_poll_interval = 8
inventory_interval_hours = 12
"@
    Set-Content -Path $cfgPath -Value $cfg -Encoding UTF8
    Log "config.ini écrit ($ServerUrl)."
} elseif (-not (Test-Path $cfgPath)) {
    Log "AVERTISSEMENT : aucune configuration fournie et aucun config.ini existant — l'agent ne s'enrôlera pas tant que $cfgPath n'est pas renseigné."
} else {
    Log "config.ini existant conservé."
}

# --- 3. (Ré)installation du service (LocalSystem) -----------------------------
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Status -ne "Stopped") { Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue }
    & $exe remove | Out-Null
    & sc.exe delete $ServiceName 2>$null | Out-Null
    Start-Sleep -Seconds 2
}
& $exe --startup auto install
if ($LASTEXITCODE -ne 0) { Write-Error "Échec de l'installation du service (code $LASTEXITCODE)."; exit 1 }
& sc.exe config $ServiceName obj= "LocalSystem" | Out-Null
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null

# --- 4. Tâche compagnon (session utilisateur : terminal + bureau à distance) --
$vbs = Join-Path $AppDir "companion.vbs"
Set-Content -Path $vbs -Value ('CreateObject("WScript.Shell").Run """' + $exe + '"" companion", 0, False') -Encoding ASCII
try {
    $a = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"' + $vbs + '"')
    $t = New-ScheduledTaskTrigger -AtLogOn
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -Hidden
    $p = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited
    Register-ScheduledTask -TaskName "TrueSight Companion" -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null
    Log "Tâche compagnon installée."
} catch {
    Log "AVERTISSEMENT : tâche compagnon non installée ($($_.Exception.Message))."
}

# --- 5. Démarrage -------------------------------------------------------------
Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
try { Start-ScheduledTask -TaskName "TrueSight Companion" -ErrorAction SilentlyContinue } catch {}

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Log "Service démarré. Le poste apparaîtra en ligne sous ~30 s."
} else {
    Log "AVERTISSEMENT : service non démarré (état : $(if ($svc) { $svc.Status } else { 'absent' }))."
}
