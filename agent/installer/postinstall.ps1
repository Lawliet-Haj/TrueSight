# ============================================================================
# TrueSight - Post-installation (appele par l'installeur Inno Setup)
# ----------------------------------------------------------------------------
# IMPORTANT : ce fichier doit rester en PUR ASCII (aucun accent). Windows
# PowerShell 5.1 lance par "powershell -File" lit un .ps1 sans BOM en ANSI ;
# un caractere accentu casserait le parsing (chaine/accolades) -> exit 1.
#
# Les fichiers de l'agent sont DEJA deposes dans $AppDir par l'installeur. Ce
# script fait la configuration systeme : config.ini (ProgramData restreint),
# service Windows SYSTEM + reprise sur echec, tache compagnon, demarrage.
# Tout est journalise dans C:\ProgramData\TrueSight\postinstall.log ; les
# operations accessoires n'interrompent pas l'installation ; en cas d'erreur on
# tente quand meme de demarrer le service (le poste ne reste jamais hors-ligne).
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

# Dossier de donnees d'abord (pour pouvoir journaliser).
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

function Log($m) {
    $line = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") + "  " + $m
    Write-Host "[TrueSight postinstall] $m"
    try { Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue } catch {}
}

Log "=== postinstall demarre (AppDir=$AppDir, ServerUrl=$ServerUrl, jeton fourni=$([bool]$Token)) ==="

try {
    if (-not (Test-Path $exe)) { throw "truesight-agent.exe introuvable dans $AppDir." }

    # --- 1. Dossier de donnees restreint (SYSTEM + Administrateurs) - non bloquant.
    try {
        & icacls $DataDir /inheritance:r 2>&1 | Out-Null
        & icacls $DataDir /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" 2>&1 | Out-Null
    } catch { Log "AVERT : restriction icacls partielle ($($_.Exception.Message))." }

    # --- 2. config.ini : ecrit si URL+jeton fournis, sinon on conserve l'existant.
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
        Log "config.ini ecrit ($ServerUrl)."
    } elseif (-not (Test-Path $cfgPath)) {
        Log "AVERT : aucune configuration fournie et aucun config.ini existant."
    } else {
        Log "config.ini existant conserve."
    }

    # --- 3. Service : conserver si present (arret + reconfiguration), sinon installer.
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        Log "Service present (etat $($existing.Status)) : arret + reconfiguration (sans suppression)."
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
        if ($LASTEXITCODE -ne 0) { throw "Echec de l'installation du service (code $LASTEXITCODE)." }
    }
    # Reconfiguration idempotente (native : non bloquant). On repasse en start= auto
    # (l'installeur a desactive le service le temps de remplacer les fichiers).
    & sc.exe config $ServiceName start= auto obj= "LocalSystem" 2>&1 | Out-Null
    & sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 2>&1 | Out-Null

    # --- 4. Tache compagnon (session utilisateur) - non bloquant.
    $vbs = Join-Path $AppDir "companion.vbs"
    try {
        Set-Content -Path $vbs -Value ('CreateObject("WScript.Shell").Run """' + $exe + '"" companion", 0, False') -Encoding ASCII
    } catch { Log "AVERT : ecriture de companion.vbs impossible ($($_.Exception.Message))." }
    try {
        $a  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"' + $vbs + '"')
        $t  = New-ScheduledTaskTrigger -AtLogOn
        $st = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -Hidden
        $pr = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited
        Register-ScheduledTask -TaskName "TrueSight Companion" -Action $a -Trigger $t -Settings $st -Principal $pr -Force | Out-Null
        Log "Tache compagnon installee."
    } catch { Log "AVERT : tache compagnon non installee ($($_.Exception.Message))." }

    # --- 5. Demarrage du service (avec re-essais : SCM parfois pas pret).
    $started = $false
    for ($i = 1; $i -le 8; $i++) {
        try {
            Start-Service -Name $ServiceName -ErrorAction Stop
            $started = $true
            break
        } catch {
            Log "Demarrage : tentative $i echouee ($($_.Exception.Message)) - nouvel essai dans 3 s."
            Start-Sleep -Seconds 3
        }
    }
    Start-Sleep -Seconds 2
    try { Start-ScheduledTask -TaskName "TrueSight Companion" -ErrorAction SilentlyContinue } catch {}

    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
        Log "OK : service demarre. Le poste apparaitra en ligne sous ~30 s."
    } else {
        Log "ECHEC : service non demarre (etat : $(if ($svc) { $svc.Status } else { 'absent' }))."
    }
}
catch {
    Log "ERREUR FATALE : $($_.Exception.Message)"
    Log "Trace : $($_.ScriptStackTrace)"
    # Filet de securite : on tente de (re)demarrer le service pour ne pas laisser
    # le poste hors-ligne suite a une erreur d'une etape de configuration.
    try { & sc.exe config $ServiceName start= auto obj= "LocalSystem" 2>&1 | Out-Null } catch {}
    try { Start-Service -Name $ServiceName -ErrorAction SilentlyContinue } catch {}
    exit 1
}

Log "=== postinstall termine ==="
exit 0
