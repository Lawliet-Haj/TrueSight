# ============================================================================
# ParcVue - Build de l'agent Windows en exécutable .exe (PyInstaller)
# ----------------------------------------------------------------------------
# Produit "parcvue-agent.exe" (mode --onefile) à partir du paquet parcvue_agent.
# L'exécutable embarque le service Windows ET le mode console.
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

Write-Host "=== Build de l'agent ParcVue ===" -ForegroundColor Cyan

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
if (Test-Path "parcvue-agent.spec") {
    Remove-Item -Force "parcvue-agent.spec" -Confirm:$false
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
    # pywin32 pour le helper en session active (CreateProcessAsUser).
    "win32ts",
    "win32profile",
    "win32process",
    "win32security",
    "win32con",
    "win32api"
)

$hiddenArgs = @()
foreach ($imp in $hiddenImports) {
    $hiddenArgs += "--hidden-import"
    $hiddenArgs += $imp
}

# 4. Lance PyInstaller en mode --onefile.
#    Le point d'entrée est __main__.py du paquet ; le service est inclus via
#    les hidden-imports + l'import de parcvue_agent.service par le runner.
Write-Host "Compilation de l'exécutable (cela peut prendre un moment)..." -ForegroundColor Yellow

# Fichier d'amorçage : importe le paquet et délègue selon le contexte
# (lancé par le SCM => service ; sinon => console).
$entryPoint = Join-Path $scriptDir "parcvue_agent\__main__.py"

pyinstaller `
    --onefile `
    --name "parcvue-agent" `
    --console `
    --paths "$scriptDir" `
    @hiddenArgs `
    --collect-submodules "win32com" `
    --collect-submodules "mss" `
    --collect-submodules "parcvue_agent" `
    "$entryPoint"

# 5. Vérifie le résultat.
$exePath = Join-Path $scriptDir "dist\parcvue-agent.exe"
if (Test-Path $exePath) {
    Write-Host "=== Build réussi ===" -ForegroundColor Green
    Write-Host "Exécutable : $exePath" -ForegroundColor Green
} else {
    Write-Host "=== Build échoué : exécutable introuvable ===" -ForegroundColor Red
    exit 1
}
