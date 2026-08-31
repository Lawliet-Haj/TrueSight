# ============================================================================
# TrueSight - Deploiement de masse VIA NINJAONE (script d'automatisation)
# ----------------------------------------------------------------------------
# A coller dans NinjaOne : Administration > Library > Scripting > New Script
#   Language        : PowerShell
#   Operating System: Windows
#   Architecture    : All
#   Run As          : System
# Puis executer sur un groupe de postes (Run > Script).
#
# Ninja est deja installe sur tous les postes : on l'utilise comme vehicule de
# deploiement de TrueSight AVANT son expiration. Le script est IDEMPOTENT : on
# peut le lancer et le relancer sur tout le parc sans risque.
#
# Il s'appuie sur le LIEN D'INSTALLATION du serveur, qui telecharge le paquet et
# ecrit la configuration (URL + jeton d'enrolement) : rien a heberger sur un
# partage, aucun secret en clair sur le reseau.
#
# NB ENCODAGE (important) : on execute le bootstrap via "iwr | iex" et NON en
# l'ecrivant dans un fichier. Le script servi contient des accents et n'a pas de
# BOM : lance par "powershell -File", il serait lu en cp1252 par PowerShell 5.1
# et l'analyse casserait. "iex" recoit une chaine deja decodee -> aucun risque.
#
# Ce fichier est en ASCII PUR pour la meme raison.
# ============================================================================
param(
    [string]$ServerUrl = "https://srv778935.hstgr.cloud",
    # Jeton d'installation (Reglages > Deploiement). A renseigner dans Ninja,
    # PAS dans le depot.
    [string]$Token = "",
    # Version attendue : si l'agent installe est plus ancien, on (re)installe.
    [string]$TargetVersion = "1.4.5",
    # Etalement des telechargements. 100 postes x ~37 Mo simultanes saturent le
    # lien du serveur : on repartit les demarrages sur cette duree.
    [int]$JitterSeconds = 300
)

$ServiceName = "TrueSightAgent"
$AppDir      = "C:\Program Files\TrueSight"
$LogFile     = "C:\Windows\Temp\truesight-ninja-deploy.log"

function Log($m) {
    $line = "[" + (Get-Date).ToString("s") + "] " + $m
    Write-Output $line
    try { Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue } catch {}
}

function Get-InstalledVersion {
    $p = Join-Path $AppDir "version.txt"
    if (Test-Path $p) {
        try { return (Get-Content $p -Raw -ErrorAction Stop).Trim() } catch { return "" }
    }
    return ""
}

# Comparaison NUMERIQUE : "1.4.10" doit etre superieur a "1.4.9" (une comparaison
# de texte donnerait l'inverse).
function Compare-Version($a, $b) {
    $pa = @(); $pb = @()
    foreach ($x in ($a -split '\.')) { $pa += [int]($x -replace '\D', '0') }
    foreach ($x in ($b -split '\.')) { $pb += [int]($x -replace '\D', '0') }
    for ($i = 0; $i -lt 3; $i++) {
        $va = 0; $vb = 0
        if ($i -lt $pa.Count) { $va = $pa[$i] }
        if ($i -lt $pb.Count) { $vb = $pb[$i] }
        if ($va -gt $vb) { return 1 }
        if ($va -lt $vb) { return -1 }
    }
    return 0
}

function Start-AgentService {
    & sc.exe config $ServiceName start= auto | Out-Null
    for ($i = 1; $i -le 5; $i++) {
        Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        $s = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
        if ($s -and $s.Status -eq "Running") { return $true }
    }
    return $false
}

# --- 0. Controles prealables --------------------------------------------------
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $Token) {
    Log "ECHEC : parametre -Token vide. Renseignez le jeton d'installation dans Ninja."
    exit 2
}

$installed = Get-InstalledVersion
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
Log ("Etat initial : version=" + $(if ($installed) { $installed } else { "absente" }) + " service=" + $(if ($svc) { $svc.Status } else { "absent" }))

# --- 1. Deja a jour ? ---------------------------------------------------------
$upToDate = $false
if ($installed -and (Test-Path (Join-Path $AppDir "truesight-agent.exe"))) {
    if (-not $TargetVersion) { $upToDate = $true }
    elseif ((Compare-Version $installed $TargetVersion) -ge 0) { $upToDate = $true }
}

if ($upToDate) {
    if ($svc -and $svc.Status -eq "Running") {
        Log ("Rien a faire : agent " + $installed + " deja en service.")
        exit 0
    }
    Log "Agent a jour mais service non demarre : demarrage."
    if (Start-AgentService) { Log "Service demarre."; exit 0 }
    Log "ECHEC : le service ne demarre pas. Voir C:\ProgramData\TrueSight\truesight-agent.log"
    exit 1
}

# --- 2. Etalement ------------------------------------------------------------
# Evite que tout le parc telecharge le paquet a la seconde ou Ninja lance le job.
if ($JitterSeconds -gt 0) {
    $wait = Get-Random -Minimum 0 -Maximum $JitterSeconds
    Log ("Attente de " + $wait + " s (etalement des telechargements).")
    Start-Sleep -Seconds $wait
}

# --- 3. Installation ----------------------------------------------------------
Log ("Installation depuis " + $ServerUrl + " (cible " + $TargetVersion + ").")
$url = $ServerUrl.TrimEnd('/') + "/install.ps1?t=" + $Token

try {
    # iwr renvoie une CHAINE deja decodee : pas de fichier, donc pas de probleme
    # d'encodage (cf. note en tete).
    $bootstrap = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 120
    $script = $bootstrap.Content
    if (-not $script -or $script.Length -lt 500) {
        Log "ECHEC : le serveur n'a pas renvoye de script d'installation (jeton invalide ou revoque ?)."
        exit 1
    }
    if ($script -match "invalide|expire") {
        Log "ECHEC : jeton d'installation invalide ou expire. Regenerez-le dans Reglages > Deploiement."
        exit 1
    }
    Log ("Bootstrap recu (" + $script.Length + " caracteres) : execution.")
    $out = Invoke-Expression $script 2>&1 | Out-String
    foreach ($l in ($out -split "`r?`n")) { if ($l.Trim()) { Log ("  | " + $l.Trim()) } }
} catch {
    Log ("ECHEC du telechargement/execution : " + $_.Exception.Message)
    exit 1
}

# --- 4. Verification ----------------------------------------------------------
Start-Sleep -Seconds 5
$after = Get-InstalledVersion
$svc2 = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if (-not $svc2) {
    Log "ECHEC : le service n'existe pas apres installation."
    exit 1
}
if ($svc2.Status -ne "Running") {
    Log ("Service en etat " + $svc2.Status + " : tentative de demarrage.")
    if (-not (Start-AgentService)) {
        Log "ECHEC : service non demarre. Voir C:\ProgramData\TrueSight\postinstall.log"
        exit 1
    }
}

if ($TargetVersion -and (Compare-Version $after $TargetVersion) -lt 0) {
    Log ("AVERTISSEMENT : version installee " + $after + " inferieure a la cible " + $TargetVersion + ".")
    exit 1
}

Log ("=== SUCCES : agent " + $after + " installe et en service.")
exit 0
