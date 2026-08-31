# ============================================================================
# TrueSight - Construction de l'installeur .exe (Inno Setup)
# ----------------------------------------------------------------------------
# 1. s'assure que le build onedir existe (sinon lance build.ps1) ;
# 2. localise le compilateur Inno Setup (ISCC.exe) ;
# 3. compile installer\truesight.iss -> dist\TrueSightAgent-Setup-<version>.exe.
#
# Prérequis : Inno Setup 6 (https://jrsoftware.org/isdl.php) — installable via
#   winget install JRSoftware.InnoSetup
#
# Utilisation (depuis le dossier agent\) :
#   .\build-installer.ps1
#   .\build-installer.ps1 -Iscc "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
# ============================================================================
param(
    [string]$Iscc,
    # Jeton d'enrôlement à PRÉ-EMBARQUER dans le .exe (optionnel). Fourni ici, il
    # est injecté à la compilation (ISCC /DDefaultToken=) : le .exe s'installe alors
    # en double-clic sans rien saisir. Laisser vide = assistant demande le jeton.
    # Le jeton n'est jamais écrit dans le dépôt : il ne vit que le temps du build.
    [string]$Token,
    # URL du serveur pre-remplie dans l'installeur (double-clic ET mode
    # silencieux). Change a chaque migration de serveur.
    [string]$ServerUrl,
    # Signature Authenticode (optionnelle) : transmise au build de l'agent ET
    # appliquée au setup.exe produit. Cf. make-signing-cert.ps1 / SIGNING.md.
    [string]$CertThumbprint,
    [string]$PfxPath,
    [System.Security.SecureString]$PfxPassword
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $scriptDir

Write-Host "=== Construction de l'installeur TrueSight ===" -ForegroundColor Cyan

# 1. Version (source de vérité : truesight_agent\__init__.py).
$initFile = Join-Path $scriptDir "truesight_agent\__init__.py"
$verMatch = Select-String -Path $initFile -Pattern '__version__\s*=\s*"([^"]+)"'
$version = if ($verMatch) { $verMatch.Matches[0].Groups[1].Value } else { "0.0.0" }
Write-Host "Version : $version" -ForegroundColor Yellow

# 2. Build onedir présent ? (sinon on le produit).
$appExe = Join-Path $scriptDir "dist\truesight-agent\truesight-agent.exe"
$appVer = Join-Path $scriptDir "dist\truesight-agent\version.txt"

# On ne se contente PAS de vérifier la présence du dossier onedir : un dossier
# laissé par un build précédent produirait un installeur incohérent (nommé
# $version mais contenant une AUTRE version), et l'agent embarqué ne serait pas
# signé alors qu'on demande une signature. Dans ces deux cas : on rebuild.
$needBuild = $false
if (-not (Test-Path $appExe)) {
    $needBuild = $true
} else {
    $onDisk = if (Test-Path $appVer) { (Get-Content $appVer -Raw).Trim() } else { "" }
    if ($onDisk -ne $version) {
        Write-Host "Onedir present en version '$onDisk' au lieu de '$version' : rebuild." -ForegroundColor Yellow
        $needBuild = $true
    } elseif ($CertThumbprint -or $PfxPath) {
        $sigAgent = Get-AuthenticodeSignature -FilePath $appExe
        if (-not $sigAgent.SignerCertificate) {
            Write-Host "Onedir present mais agent NON signe : rebuild pour le signer." -ForegroundColor Yellow
            $needBuild = $true
        }
    }
}

if ($needBuild) {
    Write-Host "Lancement de build.ps1..." -ForegroundColor Yellow
    # On transmet la signature : l'agent embarqué doit être signé lui aussi.
    $buildParams = @{}
    if ($CertThumbprint) { $buildParams["CertThumbprint"] = $CertThumbprint }
    if ($PfxPath)        { $buildParams["PfxPath"] = $PfxPath }
    if ($PfxPassword)    { $buildParams["PfxPassword"] = $PfxPassword }
    & (Join-Path $scriptDir "build.ps1") @buildParams
    if (-not (Test-Path $appExe)) {
        Write-Host "Build onedir introuvable après build.ps1." -ForegroundColor Red
        exit 1
    }
}

# 3. Localisation d'ISCC.exe (Inno Setup).
if (-not $Iscc) {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) {
        $Iscc = $cmd.Source
    } else {
        foreach ($p in @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
        )) {
            if (Test-Path $p) { $Iscc = $p; break }
        }
    }
}
if (-not $Iscc -or -not (Test-Path $Iscc)) {
    Write-Host "Inno Setup (ISCC.exe) introuvable." -ForegroundColor Red
    Write-Host "Installez-le :  winget install JRSoftware.InnoSetup" -ForegroundColor Yellow
    Write-Host "ou téléchargez-le : https://jrsoftware.org/isdl.php" -ForegroundColor Yellow
    exit 1
}
Write-Host "ISCC : $Iscc" -ForegroundColor Yellow

# 4. Compilation.
$iss = Join-Path $scriptDir "installer\truesight.iss"
$isccArgs = @("/DAppVersion=$version")
if ($ServerUrl) {
    $isccArgs += "/DDefaultServerUrl=$ServerUrl"
    Write-Host "URL serveur embarquee : $ServerUrl" -ForegroundColor Yellow
}
if ($Token) {
    $isccArgs += "/DDefaultToken=$Token"
    Write-Host "Jeton d'enrôlement PRÉ-EMBARQUÉ : installation en double-clic sans saisie." -ForegroundColor Yellow
}
$isccArgs += $iss
& $Iscc @isccArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "Échec de la compilation Inno Setup (code $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}

$out = Join-Path $scriptDir "dist\TrueSightAgent-Setup-$version.exe"

# 4bis. Signature du setup.exe (le fichier que l'utilisateur double-clique : c'est
#       LUI qui déclenche l'avertissement « éditeur inconnu »).
if (Test-Path $out) {
    if ($CertThumbprint -or $PfxPath) {
        $signScript = Join-Path $scriptDir "sign.ps1"
        $signParams = @{ Path = $out }
        if ($CertThumbprint) { $signParams["CertThumbprint"] = $CertThumbprint }
        if ($PfxPath)        { $signParams["PfxPath"] = $PfxPath }
        if ($PfxPassword)    { $signParams["PfxPassword"] = $PfxPassword }
        & $signScript @signParams
        if ($LASTEXITCODE -ne 0) {
            Write-Host "=== Installeur produit mais NON signé (échec de signature) ===" -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host "Installeur NON signé (« éditeur inconnu »)." -ForegroundColor Yellow
        Write-Host "Pour signer : -CertThumbprint <empreinte>  (cf. SIGNING.md)" -ForegroundColor DarkGray
    }
}

if (Test-Path $out) {
    $sizeMb = [math]::Round((Get-Item $out).Length / 1MB, 1)
    Write-Host "=== Installeur prêt ===" -ForegroundColor Green
    Write-Host "Fichier : $out ($sizeMb Mo)" -ForegroundColor Green
    if ($Token) {
        Write-Host "Jeton embarqué : double-clic = installation automatique (aucune saisie)." -ForegroundColor Cyan
    } else {
        Write-Host "Manuel  : double-clic (assistant URL + jeton)." -ForegroundColor Cyan
        Write-Host "Embarquer le jeton : .\build-installer.ps1 -Token <jeton>" -ForegroundColor Cyan
    }
    $hintUrl = if ($ServerUrl) { $ServerUrl } else { "https://VOTRE-SERVEUR" }
    Write-Host "Parc    : TrueSightAgent-Setup-$version.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SERVERURL=$hintUrl /TOKEN=<jeton>" -ForegroundColor Cyan
} else {
    Write-Host "=== Échec : installeur introuvable ===" -ForegroundColor Red
    exit 1
}
