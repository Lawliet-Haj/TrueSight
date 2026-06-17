"""Génère le script d'installation « en ligne » de l'agent (bootstrap PowerShell).

Servi par ``GET /install.ps1?t=<jeton>`` : l'admin lance le one-liner dans un
PowerShell ÉLEVÉ sur le poste cible ; le script télécharge le paquet de l'agent
et un ``config.ini`` (URL serveur + enrollment_token) puis installe le service
SYSTEM + la tâche compagnon — exactement comme ``install-service.ps1``, mais sans
fichier à copier au préalable.

Le script est rendu à partir d'un gabarit avec deux substitutions littérales
(``__BASE__`` et ``__TOKEN__``) — pas de ``str.format`` car PowerShell utilise
abondamment les accolades.
"""
from __future__ import annotations

_TEMPLATE = r'''# ============================================================================
# TrueSight - Installation en ligne de l'agent (généré par le serveur)
# ----------------------------------------------------------------------------
# À lancer dans un PowerShell ADMINISTRATEUR sur le poste à enrôler :
#   powershell -ExecutionPolicy Bypass -Command "iwr -useb __BASE__/install.ps1?t=__TOKEN__ | iex"
# ============================================================================
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Base        = "__BASE__"
$Token       = "__TOKEN__"
$ServiceName = "TrueSightAgent"
$DataDir     = "C:\ProgramData\TrueSight"
$AppDir      = "C:\Program Files\TrueSight"

function Info($m) { Write-Host "[TrueSight] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[TrueSight] $m" -ForegroundColor Green }
function Err($m)  { Write-Host "[TrueSight] $m" -ForegroundColor Red }

# --- 1. Droits administrateur -------------------------------------------------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Err "Ce script doit etre lance dans un PowerShell ADMINISTRATEUR."
    Err "Ouvrez 'Windows PowerShell' > clic droit > 'Executer en tant qu'administrateur', puis relancez la commande."
    return
}

Info "Installation de l'agent TrueSight depuis $Base"
$work = Join-Path $env:TEMP ("truesight-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $work | Out-Null

try {
    # --- 2. Telechargement du paquet (dossier onedir zippe) -------------------
    $zip = Join-Path $work "agent.zip"
    Info "Telechargement du paquet de l'agent..."
    Invoke-WebRequest -Uri "$Base/api/v1/install/$Token/package" -OutFile $zip -UseBasicParsing

    $extract = Join-Path $work "extract"
    New-Item -ItemType Directory -Force -Path $extract | Out-Null
    Info "Decompression..."
    Expand-Archive -Path $zip -DestinationPath $extract -Force

    # Localise le dossier onedir (celui qui contient truesight-agent.exe).
    $srcExe = Get-ChildItem -Path $extract -Recurse -Filter "truesight-agent.exe" -ErrorAction SilentlyContinue |
              Select-Object -First 1
    if (-not $srcExe) { throw "truesight-agent.exe introuvable dans le paquet telecharge." }
    $srcApp = Split-Path -Parent $srcExe.FullName

    # --- 3. config.ini (URL serveur + enrollment_token) -----------------------
    Info "Recuperation de la configuration..."
    $cfg = Join-Path $work "config.ini"
    Invoke-WebRequest -Uri "$Base/api/v1/install/$Token/config" -OutFile $cfg -UseBasicParsing

    # --- 4. Dossiers : donnees (restreint) + application (Program Files) -------
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    New-Item -ItemType Directory -Force -Path $AppDir  | Out-Null
    & icacls $DataDir /inheritance:r | Out-Null
    & icacls $DataDir /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

    $targetExe    = Join-Path $AppDir  "truesight-agent.exe"
    $targetConfig = Join-Path $DataDir "config.ini"

    # --- 5. Arret du service existant AVANT copie (sans suppression) -----------
    # On NE supprime PAS le service : un delete + recreate rapproche peut le
    # laisser « marque pour suppression » et empecher tout redemarrage. Un arret
    # suffit pour liberer les fichiers (le binPath sous Program Files est inchange).
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        # DESACTIVE le service avant de l'arreter : sinon la reprise sur echec
        # (sc failure ... restart) le relance pendant l'install et reverrouille
        # les fichiers. Le service est repasse en start= auto a l'etape 7.
        Info "Arret du service existant (desactivation temporaire)..."
        & sc.exe config $ServiceName start= disabled 2>&1 | Out-Null
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        for ($i = 0; $i -lt 40; $i++) {
            $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
            if (-not $s -or $s.Status -eq "Stopped") { break }
            Start-Sleep -Milliseconds 500
        }
    }
    # Arret du compagnon + de TOUS les helpers (ils verrouillent _internal\*.pyd
    # et survivent a l'arret du service) ; on attend leur disparition effective.
    try { Stop-ScheduledTask -TaskName "TrueSight Companion" -ErrorAction SilentlyContinue } catch {}
    Get-Process -Name "truesight-agent" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 40; $i++) {
        if (-not (Get-Process -Name "truesight-agent" -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Seconds 2

    # --- 6. Deploiement de l'application + configuration -----------------------
    Info "Deploiement de l'application dans $AppDir..."
    Get-ChildItem -Path $AppDir -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item -Path (Join-Path $srcApp '*') -Destination $AppDir -Recurse -Force
    if (-not (Test-Path $targetExe)) { throw "Echec de la copie : $targetExe introuvable." }

    if (-not (Test-Path $targetConfig)) {
        Copy-Item -Path $cfg -Destination $targetConfig -Force
        Info "config.ini deploye."
    } else {
        Info "config.ini deja present : conserve."
    }

    # --- 7. Service : conserver si present, sinon installer --------------------
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        Info "Installation du service $ServiceName..."
        & $targetExe --startup auto install
        if ($LASTEXITCODE -ne 0) { throw "Echec de l'installation du service (code $LASTEXITCODE)." }
    } else {
        Info "Service deja present : conserve (reconfiguration)."
    }
    & sc.exe config $ServiceName start= auto obj= "LocalSystem" | Out-Null
    & sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000 | Out-Null

    # --- 8. Tache compagnon (session utilisateur) ------------------------------
    $vbs = Join-Path $AppDir "companion.vbs"
    $vbsContent = 'CreateObject("WScript.Shell").Run """' + $targetExe + '"" companion", 0, False'
    Set-Content -Path $vbs -Value $vbsContent -Encoding ASCII
    try {
        $compAction   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"' + $vbs + '"')
        $compTrigger  = New-ScheduledTaskTrigger -AtLogOn
        $compSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -Hidden
        $compPrincipal = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited
        Register-ScheduledTask -TaskName "TrueSight Companion" -Action $compAction -Trigger $compTrigger `
            -Settings $compSettings -Principal $compPrincipal -Force | Out-Null
        Info "Tache compagnon installee (demarre au prochain logon)."
    } catch {
        Info "AVERTISSEMENT : tache compagnon non installee ($($_.Exception.Message))."
    }

    # --- 9. Demarrage (avec re-essais) -----------------------------------------
    # Juste apres sc delete + reinstall, le SCM peut refuser le 1er Start-Service.
    Info "Demarrage du service..."
    for ($i = 1; $i -le 6; $i++) {
        try { Start-Service -Name $ServiceName -ErrorAction Stop; break }
        catch { Info "Demarrage tentative $i echouee, nouvel essai dans 3 s."; Start-Sleep -Seconds 3 }
    }
    Start-Sleep -Seconds 2
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    # Lance aussi le compagnon pour la session courante (sans attendre un re-logon).
    try { Start-ScheduledTask -TaskName "TrueSight Companion" -ErrorAction SilentlyContinue } catch {}

    if ($svc -and $svc.Status -eq "Running") {
        Ok "Agent TrueSight installe et demarre. Le poste apparaitra en ligne sous ~30 s."
    } else {
        Err "Le service est installe mais non demarre (etat : $(if ($svc) { $svc.Status } else { 'absent' }))."
        Err "Consulter $DataDir\truesight-agent.log pour le diagnostic."
    }
}
finally {
    # Nettoyage du dossier temporaire (best effort).
    try { Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue } catch {}
}
'''

_INVALID = (
    "Write-Host 'TrueSight : lien d''installation invalide ou expire.' "
    "-ForegroundColor Red\n"
)


def render_install_script(base_url: str, token: str) -> str:
    """Rend le script d'installation pour une base + un jeton donnés."""
    base = (base_url or "").rstrip("/")
    return _TEMPLATE.replace("__BASE__", base).replace("__TOKEN__", token)


def render_invalid_script() -> str:
    """Script renvoyé pour un jeton invalide/expiré (message clair côté poste)."""
    return _INVALID
