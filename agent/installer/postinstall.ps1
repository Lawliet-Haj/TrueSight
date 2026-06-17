# ============================================================================
# TrueSight - Post-installation (appelé par l'installeur Inno Setup)
# ----------------------------------------------------------------------------
# Les fichiers de l'agent sont DÉJÀ déposés dans $AppDir par l'installeur. Ce
# script ne fait QUE la configuration système : config.ini (ProgramData
# restreint), service Windows SYSTEM + reprise sur échec, tâche compagnon, et
# démarrage. Exécuté élevé (l'installeur exige les droits admin).
#
# Robustesse : tout est journalisé dans C:\ProgramData\TrueSight\postinstall.log ;
# les opérations accessoires (icacls, .vbs, tâche planifiée) n'interrompent pas
# l'installation ; en cas d'erreur, on tente malgré tout de démarrer le service
# pour ne jamais laisser le poste hors-ligne.
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
$logFile     = Join-Path $DataDir "postinstall.log"

# Dossier de données d'abord (pour pouvoir journaliser).
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

function Log($m) {
    $line = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") + "  " + $m
    Write-Host "[TrueSight postinstall] $m"
    try { Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue } catch {}
}

Log "=== postinstall démarré (AppDir=$AppDir, ServerUrl=$ServerUrl, jeton fourni=$([bool]$Token)) ==="

try {
    if (-not (Test-Path $exe)) { throw "truesight-agent.exe introuvable dans $AppDir." }

    # --- 1. Dossier de données restreint (SYSTEM + Administrateurs) — non bloquant.
    try {
        & icacls $DataDir /inheritance:r 2>&1 | Out-Null
        & icacls $DataDir /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" 2>&1 | Out-Null
    } catch { Log "AVERT : restriction icacls partielle ($($_.Exception.Message))." }

    # --- 2. config.ini : écrit si URL+jeton fournis, sinon on conserve l'existant.
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
        Log "AVERT : aucune configuration fournie et aucun config.ini existant."
    } else {
        Log "config.ini existant conservé."
    }

    # --- 3. Service : conserver si présent (arrêt + reconfiguration), sinon installer.
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        Log "Service présent (état $($existing.Status)) : arrêt + reconfiguration (sans suppression)."
        if ($existing.Status -ne "Stopped") {
            Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        }
        for ($i = 0; $i -lt 20; $i++) {
            $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if (-not $s -or $s.Status -eq "Stopped") { break }
            Start-Sleep -Milliseconds 500
        }
    } else {
        Log "Installation du service."
        & $exe --startup auto install 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Échec de l'installation du service (code $LASTEXITCODE)." }
    }
    # Reconfiguration idempotente (native : non bloquant).
    & sc.exe config $ServiceName start= auto obj= "LocalSystem" 2>&1 | Out-Null
    & sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 2>&1 | Out-Null

    # --- 4. Tâche compagnon (session utilisateur) — non bloquant.
    $vbs = Join-Path $AppDir "companion.vbs"
    try {
        Set-Content -Path $vbs -Value ('CreateObject("WScript.Shell").Run """' + $exe + '"" companion", 0, False') -Encoding ASCII
    } catch { Log "AVERT : écriture de companion.vbs impossible ($($_.Exception.Message))." }
    try {
        $a  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"' + $vbs + '"')
        $t  = New-ScheduledTaskTrigger -AtLogOn
        $st = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -Hidden
        $pr = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited
        Register-ScheduledTask -TaskName "TrueSight Companion" -Action $a -Trigger $t -Settings $st -Principal $pr -Force | Out-Null
        Log "Tâche compagnon installée."
    } catch { Log "AVERT : tâche compagnon non installée ($($_.Exception.Message))." }

    # --- 5. Démarrage du service (avec ré-essais : SCM parfois pas prêt).
    $started = $false
    for ($i = 1; $i -le 8; $i++) {
        try {
            Start-Service -Name $ServiceName -ErrorAction Stop
            $started = $true
            break
        } catch {
            Log "Démarrage : tentative $i échouée ($($_.Exception.Message)) — nouvel essai dans 3 s."
            Start-Sleep -Seconds 3
        }
    }
    Start-Sleep -Seconds 2
    try { Start-ScheduledTask -TaskName "TrueSight Companion" -ErrorAction SilentlyContinue } catch {}

    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
        Log "OK : service démarré. Le poste apparaîtra en ligne sous ~30 s."
    } else {
        Log "ÉCHEC : service non démarré (état : $(if ($svc) { $svc.Status } else { 'absent' }))."
    }
}
catch {
    Log "ERREUR FATALE : $($_.Exception.Message)"
    Log "Trace : $($_.ScriptStackTrace)"
    # Filet de sécurité : on tente de (re)démarrer le service pour ne pas laisser
    # le poste hors-ligne suite à une erreur d'une étape de configuration.
    try { Start-Service -Name $ServiceName -ErrorAction SilentlyContinue } catch {}
    exit 1
}

Log "=== postinstall terminé ==="
exit 0
