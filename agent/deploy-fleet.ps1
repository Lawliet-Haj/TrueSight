# ============================================================================
# TrueSight - Deploiement de masse via l'INSTALLEUR SIGNE (GPO / Intune)
# ----------------------------------------------------------------------------
# Script de DEMARRAGE machine (execute en SYSTEM au boot par une GPO
# "Scripts de demarrage", ou par Intune). IDEMPOTENT : il peut tourner a chaque
# boot sans rien casser - il n'agit que si l'agent est absent ou perime.
#
# Pourquoi ce script plutot que gpo-install.ps1 :
#   - un SEUL fichier a publier (l'installeur .exe), pas d'arborescence onedir
#     ni de config.ini a maintenir sur le partage ;
#   - l'installeur est SIGNE et porte deja l'URL du serveur et le jeton
#     d'enrolement (build-installer.ps1 -Token ...), donc aucun secret en clair
#     sur le partage ;
#   - il embarque les correctifs d'installation (arret des processus, re-essais).
#
# Preparer le partage (lecture seule pour "Ordinateurs du domaine") :
#   \SERVEUR\Partage\TrueSight\TrueSightAgent-Setup-1.4.5.exe
#
# GPO : Configuration ordinateur > Strategies > Parametres Windows > Scripts
#       (demarrage/arret) > Demarrage > Ajouter > deploy-fleet.ps1
#       Parametres :  -SetupPath \SERVEUR\Partage\TrueSight\TrueSightAgent-Setup-1.4.5.exe -Version 1.4.5
#
# NB : fichier en ASCII PUR - lance via "powershell -File", un .ps1 UTF-8 sans
# BOM est lu en cp1252 par PowerShell 5.1 et ne parse plus.
# ============================================================================
param(
    # Chemin UNC de l'installeur signe (accessible en lecture par le compte
    # ORDINATEUR, pas seulement par l'utilisateur).
    [Parameter(Mandatory = $true)][string]$SetupPath,
    # Version attendue. Si l'agent installe est deja >= a celle-ci, on ne fait
    # RIEN. Omise, on installe seulement si l'agent est totalement absent.
    [string]$Version,
    # Emplacement a affecter au poste dans le dashboard (optionnel).
    [string]$Site,
    [string]$LogFile = "C:\Windows\Temp\truesight-deploy.log"
)

$ErrorActionPreference = "Stop"
$ServiceName = "TrueSightAgent"
$AppDir      = "C:\Program Files\TrueSight"

function Log($m) {
    try { Add-Content -Path $LogFile -Value ((Get-Date).ToString("s") + "  " + $m) } catch {}
}

function Get-InstalledVersion {
    $p = Join-Path $AppDir "version.txt"
    if (Test-Path $p) { return (Get-Content $p -Raw -ErrorAction SilentlyContinue).Trim() }
    return ""
}

# Comparaison NUMERIQUE (et non alphabetique : "1.4.10" doit etre > "1.4.9").
function Compare-Version($a, $b) {
    $pa = @(); $pb = @()
    foreach ($x in ($a -split '\.')) { $pa += [int]($x -replace '\D', '0') }
    foreach ($x in ($b -split '\.')) { $pb += [int]($x -replace '\D', '0') }
    for ($i = 0; $i -lt 3; $i++) {
        $va = if ($i -lt $pa.Count) { $pa[$i] } else { 0 }
        $vb = if ($i -lt $pb.Count) { $pb[$i] } else { 0 }
        if ($va -gt $vb) { return 1 }
        if ($va -lt $vb) { return -1 }
    }
    return 0
}

$installed = Get-InstalledVersion
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

# --- Faut-il agir ? -----------------------------------------------------------
$action = $null
if (-not $svc -or -not (Test-Path (Join-Path $AppDir "truesight-agent.exe"))) {
    $action = "installation (agent absent)"
} elseif ($Version -and (Compare-Version $installed $Version) -lt 0) {
    $action = "mise a jour ($installed -> $Version)"
} elseif ($svc.Status -ne "Running") {
    # Agent a jour mais service arrete : on tente un simple demarrage, sans
    # reinstaller (bien moins intrusif).
    Log "Agent $installed present mais service $($svc.Status) : tentative de demarrage."
    & sc.exe config $ServiceName start= auto | Out-Null
    for ($i = 1; $i -le 4; $i++) {
        Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        if ((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status -eq "Running") { break }
    }
    $st = (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status
    Log "Service : $st"
    exit $(if ($st -eq "Running") { 0 } else { 1 })
}

if (-not $action) {
    # Cas le plus frequent apres deploiement : rien a faire, on sort vite.
    exit 0
}

Log "=== $action"

if (-not (Test-Path $SetupPath)) {
    Log "ECHEC : installeur introuvable ou partage inaccessible : $SetupPath"
    exit 1
}

# --- Copie locale avant execution --------------------------------------------
# On n'execute PAS depuis le partage : si le reseau tombe en pleine
# installation, l'installeur se retrouve ampute et le poste reste casse.
$local = Join-Path $env:TEMP ("TrueSightSetup-" + [Guid]::NewGuid().ToString("N") + ".exe")
try {
    Copy-Item -Path $SetupPath -Destination $local -Force
} catch {
    Log "ECHEC de la copie locale : $($_.Exception.Message)"
    exit 1
}

# --- Installation silencieuse -------------------------------------------------
$setupArgs = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
if ($Site) { $setupArgs += "/SITE=$Site" }
Log ("Lancement : " + (Split-Path $SetupPath -Leaf) + " " + ($setupArgs -join " "))

try {
    $p = Start-Process -FilePath $local -ArgumentList $setupArgs -Wait -PassThru
    $code = $p.ExitCode
} catch {
    Log "ECHEC du lancement : $($_.Exception.Message)"
    Remove-Item $local -Force -ErrorAction SilentlyContinue
    exit 1
}
Remove-Item $local -Force -ErrorAction SilentlyContinue

# --- Verification du resultat -------------------------------------------------
Start-Sleep -Seconds 5
$after = Get-InstalledVersion
$svc2 = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$state = if ($svc2) { $svc2.Status } else { "absent" }
Log "Code de sortie=$code  version=$after  service=$state"

if ($svc2 -and $svc2.Status -eq "Running") {
    Log "=== Deploiement REUSSI (agent $after)"
    exit 0
}

# Le service peut mettre un instant a demarrer : on laisse une chance.
for ($i = 1; $i -le 5; $i++) {
    Start-Sleep -Seconds 3
    Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status -eq "Running") {
        Log "=== Deploiement REUSSI apres $i tentative(s) de demarrage (agent $after)"
        exit 0
    }
}

Log "=== ECHEC : service non demarre. Voir C:\ProgramData\TrueSight\postinstall.log"
exit 1
