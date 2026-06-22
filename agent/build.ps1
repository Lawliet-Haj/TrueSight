# ============================================================================
# TrueSight - Build de l'agent Windows en exécutable .exe (PyInstaller)
# ----------------------------------------------------------------------------
# Produit le dossier onedir dist\truesight-agent\ (exe + _internal\) PUIS un
# paquet de déploiement dist\truesight-agent-<version>.zip (avec version.txt +
# empreinte SHA-256) à téléverser dans le dashboard (auto-update + lien d'install).
# L'exécutable embarque le service Windows, le compagnon ET le mode console.
#
# Prérequis :
#   - Python 3.12 (Windows)
#   - pip install -r requirements.txt   (inclut pyinstaller)
#
# Utilisation (depuis le dossier agent\) :
#   .\build.ps1
# ============================================================================

# Arrête le script à la première erreur.
$ErrorActionPreference = "Stop"

# Se place dans le dossier du script (= dossier agent\).
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $scriptDir

Write-Host "=== Build de l'agent TrueSight ===" -ForegroundColor Cyan

# 1. Vérifie la présence de PyInstaller.
Write-Host "Vérification de PyInstaller..." -ForegroundColor Yellow
$pyinstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyinstaller) {
    Write-Host "PyInstaller introuvable. Installation des dépendances..." -ForegroundColor Yellow
    python -m pip install -r requirements.txt
}

# 2. Nettoie les artefacts d'un build précédent.
Write-Host "Nettoyage des builds précédents..." -ForegroundColor Yellow
foreach ($dir in @("build", "dist", "__pycache__")) {
    if (Test-Path $dir) {
        Remove-Item -Recurse -Force $dir -Confirm:$false
    }
}
if (Test-Path "truesight-agent.spec") {
    Remove-Item -Force "truesight-agent.spec" -Confirm:$false
}

# 3. Imports cachés nécessaires (modules chargés dynamiquement par pywin32/WMI).
#    win32timezone est requis par certains chemins de code pywin32.
$hiddenImports = @(
    "win32timezone",
    "win32serviceutil",
    "win32service",
    "win32event",
    "servicemanager",
    "win32crypt",
    "wmi",
    "win32com",
    "win32com.client",
    "pythoncom",
    "pywintypes",
    # --- Bureau à distance : capture, encodage, transport ---
    # Sans ces imports, l'exe ne pourrait pas capturer l'écran ni se connecter au relais.
    "mss",
    "mss.windows",
    "PIL",
    "PIL.Image",
    "websocket",
    # --- Terminal interactif : PTY ConPTY (pywinpty) ---
    # Sans cet import, l'exe ne pourrait pas lancer le shell PTY relayé.
    "winpty",
    # pywin32 pour le helper en session active (CreateProcessAsUser).
    "win32ts",
    "win32profile",
    "win32process",
    "win32security",
    "win32con",
    "win32api",
    # --- Compagnon de session utilisateur : IPC named pipe (service <-> compagnon) ---
    "win32pipe",
    "win32file",
    "winerror",
    # --- Encodage JPEG accéléré (optionnel) : PyTurboJPEG + numpy ---
    "turbojpeg",
    "numpy"
)

$hiddenArgs = @()
foreach ($imp in $hiddenImports) {
    $hiddenArgs += "--hidden-import"
    $hiddenArgs += $imp
}

# 3bis. DLL native libjpeg-turbo (turbojpeg.dll) — OPTIONNELLE.
#   Si présente sur le poste de build, on l'embarque (--add-binary) : l'agent
#   l'utilisera en priorité (encodage ~5-10x plus rapide que Pillow). Sinon le
#   build reste valide et l'agent se rabat automatiquement sur Pillow.
#   Localisation : variable TURBOJPEG_DLL, sinon installation standard.
$turbojpegArgs = @()
$turbojpegDll = $env:TURBOJPEG_DLL
if (-not $turbojpegDll -or -not (Test-Path $turbojpegDll)) {
    foreach ($cand in @("C:\libjpeg-turbo64\bin\turbojpeg.dll", "C:\libjpeg-turbo\bin\turbojpeg.dll")) {
        if (Test-Path $cand) { $turbojpegDll = $cand; break }
    }
}
if ($turbojpegDll -and (Test-Path $turbojpegDll)) {
    Write-Host "libjpeg-turbo detecte : $turbojpegDll (embarque)" -ForegroundColor Green
    $turbojpegArgs += "--add-binary"
    $turbojpegArgs += "$turbojpegDll;."
} else {
    Write-Host "turbojpeg.dll introuvable : encodage Pillow (repli). Definir TURBOJPEG_DLL pour l'accelerer." -ForegroundColor Yellow
}

# 4. Lance PyInstaller en mode --onefile.
#    Le point d'entrée est __main__.py du paquet ; le service est inclus via
#    les hidden-imports + l'import de truesight_agent.service par le runner.
Write-Host "Compilation de l'exécutable (cela peut prendre un moment)..." -ForegroundColor Yellow

# Fichier d'amorçage : importe le paquet (import ABSOLU) et délègue selon le
# contexte (SCM => service ; sinon => console). On NE pointe PAS directement sur
# truesight_agent\__main__.py : exécuté en top-level, ses imports relatifs
# casseraient (« relative import with no known parent package »).
$entryPoint = Join-Path $scriptDir "run_agent.py"

# --onedir (et NON --onefile) : produit un dossier dist\truesight-agent\ (exe +
# _internal\). Pas d'extraction temporaire au lancement → fiable quand le service
# SYSTEM relance le helper dans la session utilisateur via CreateProcessAsUser
# (l'extraction onefile échouait dans ce contexte → bureau à distance écran noir).
pyinstaller `
    --onedir `
    --name "truesight-agent" `
    --console `
    --noconfirm `
    --paths "$scriptDir" `
    @hiddenArgs `
    @turbojpegArgs `
    --collect-submodules "win32com" `
    --collect-submodules "mss" `
    --collect-all "winpty" `
    --collect-all "dxcam" `
    --collect-all "comtypes" `
    --collect-all "pyaudiowpatch" `
    --hidden-import "_portaudiowpatch" `
    --collect-submodules "truesight_agent" `
    "$entryPoint"

# 5. Vérifie le résultat (onedir : dossier dist\truesight-agent\ + exe à l'intérieur).
$appDir  = Join-Path $scriptDir "dist\truesight-agent"
$exePath = Join-Path $appDir "truesight-agent.exe"
if (-not (Test-Path $exePath)) {
    Write-Host "=== Build échoué : exécutable introuvable ===" -ForegroundColor Red
    exit 1
}
Write-Host "=== Build réussi (onedir) ===" -ForegroundColor Green
Write-Host "Dossier applicatif : $appDir" -ForegroundColor Green
Write-Host "Exécutable : $exePath" -ForegroundColor Green

# 6. Empaquetage : version.txt + zip versionné + empreinte SHA-256.
#    Le zip (contenant le dossier truesight-agent\) se téléverse tel quel dans le
#    dashboard (Réglages > Déploiement) → sert l'auto-update et le lien d'install.
Write-Host "Empaquetage du paquet de déploiement..." -ForegroundColor Yellow
$initFile = Join-Path $scriptDir "truesight_agent\__init__.py"
$verMatch = Select-String -Path $initFile -Pattern '__version__\s*=\s*"([^"]+)"'
$version = if ($verMatch) { $verMatch.Matches[0].Groups[1].Value } else { "0.0.0" }
# version.txt à la racine du dossier onedir : lu par le serveur au téléversement.
Set-Content -Path (Join-Path $appDir "version.txt") -Value $version -Encoding ASCII -NoNewline

$zipPath = Join-Path $scriptDir ("dist\truesight-agent-$version.zip")
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path $appDir -DestinationPath $zipPath -CompressionLevel Optimal
$sha = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLower()
$sizeMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)

Write-Host "=== Paquet prêt ===" -ForegroundColor Green
Write-Host "Version  : $version" -ForegroundColor Green
Write-Host "Paquet   : $zipPath ($sizeMb Mo)" -ForegroundColor Green
Write-Host "SHA-256  : $sha" -ForegroundColor Green
Write-Host "→ Téléverser ce .zip dans le dashboard : Réglages > Déploiement & mises à jour." -ForegroundColor Cyan
