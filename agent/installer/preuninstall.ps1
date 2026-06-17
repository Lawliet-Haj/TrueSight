# ============================================================================
# TrueSight - Pré-désinstallation (appelé par l'uninstaller Inno Setup)
# ----------------------------------------------------------------------------
# Arrête et supprime le service + la tâche compagnon AVANT que l'uninstaller ne
# retire les fichiers (sinon l'exe en cours d'exécution les verrouille).
# ============================================================================
$ErrorActionPreference = "SilentlyContinue"
$ServiceName = "TrueSightAgent"

# $PSScriptRoot = dossier d'installation ({app}), où réside cet uninstaller.
$exe = Join-Path $PSScriptRoot "truesight-agent.exe"

# 1. Service.
Stop-Service -Name $ServiceName -Force
Start-Sleep -Seconds 2
if (Test-Path $exe) { & $exe remove | Out-Null }
& sc.exe delete $ServiceName 2>$null | Out-Null

# 2. Tâche compagnon (toutes sessions).
try { Stop-ScheduledTask -TaskName "TrueSight Companion" } catch {}
try { Unregister-ScheduledTask -TaskName "TrueSight Companion" -Confirm:$false } catch {}

# 3. Processus résiduels (compagnon / helper en session utilisateur).
Get-Process -Name "truesight-agent" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

# 4. Données : on retire le dossier ProgramData (config + state + logs).
#    Commentez la ligne suivante pour CONSERVER l'identité/token (réinstallation).
Remove-Item -Recurse -Force "C:\ProgramData\TrueSight" -ErrorAction SilentlyContinue
