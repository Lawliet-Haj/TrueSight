# ============================================================================
# TrueSight - Pre-desinstallation (appele par l'uninstaller Inno Setup)
# ----------------------------------------------------------------------------
# IMPORTANT : PUR ASCII (aucun accent) - lance par "powershell -File" sous
# Windows PowerShell 5.1 qui lit un .ps1 sans BOM en ANSI.
#
# Arrete et supprime le service + la tache compagnon AVANT que l'uninstaller ne
# retire les fichiers (sinon l'exe en cours d'execution les verrouille).
# ============================================================================
$ErrorActionPreference = "SilentlyContinue"
$ServiceName = "TrueSightAgent"

# $PSScriptRoot = dossier d'installation ({app}), ou reside cet uninstaller.
$exe = Join-Path $PSScriptRoot "truesight-agent.exe"

# 1. Service : desactiver (evite un redemarrage par la reprise sur echec), arreter, supprimer.
& sc.exe config $ServiceName start= disabled 2>&1 | Out-Null
Stop-Service -Name $ServiceName -Force
Start-Sleep -Seconds 2
if (Test-Path $exe) { & $exe remove | Out-Null }
& sc.exe delete $ServiceName 2>$null | Out-Null

# 2. Tache compagnon (toutes sessions).
try { Stop-ScheduledTask -TaskName "TrueSight Companion" } catch {}
try { Unregister-ScheduledTask -TaskName "TrueSight Companion" -Confirm:$false } catch {}

# 3. Process residuels (compagnon / helper en session utilisateur).
Get-Process -Name "truesight-agent" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

# 4. Donnees : on retire le dossier ProgramData (config + state + logs).
#    Commentez la ligne suivante pour CONSERVER l'identite/token (reinstallation).
Remove-Item -Recurse -Force "C:\ProgramData\TrueSight" -ErrorAction SilentlyContinue
