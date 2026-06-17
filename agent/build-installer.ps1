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
    [string]$Iscc
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
if (-not (Test-Path $appExe)) {
    Write-Host "Build onedir absent : lancement de build.ps1..." -ForegroundColor Yellow
    & (Join-Path $scriptDir "build.ps1")
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
& $Iscc "/DAppVersion=$version" $iss
if ($LASTEXITCODE -ne 0) {
    Write-Host "Échec de la compilation Inno Setup (code $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}

$out = Join-Path $scriptDir "dist\TrueSightAgent-Setup-$version.exe"
if (Test-Path $out) {
    $sizeMb = [math]::Round((Get-Item $out).Length / 1MB, 1)
    Write-Host "=== Installeur prêt ===" -ForegroundColor Green
    Write-Host "Fichier : $out ($sizeMb Mo)" -ForegroundColor Green
    Write-Host "Manuel  : double-clic (assistant URL + jeton)." -ForegroundColor Cyan
    Write-Host "Parc    : TrueSightAgent-Setup-$version.exe /VERYSILENT /SUPPRESSMSGBOXES /SERVERURL=https://srv778935.hstgr.cloud /TOKEN=<jeton>" -ForegroundColor Cyan
} else {
    Write-Host "=== Échec : installeur introuvable ===" -ForegroundColor Red
    exit 1
}
