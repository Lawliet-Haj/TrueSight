# TrueSight Agent — persistance (rester en tâche de fond en continu)

L'agent doit tourner en permanence, sans fenêtre, redémarrer au boot et se relancer
en cas d'arrêt. Deux modes, selon le contexte.

L'agent embarque deux garde-fous pour ça :
- **journalisation sans console** (compatible `pythonw.exe` / service : pas de `sys.stderr`) ;
- **garde mono-instance** (mutex `TrueSightAgentSingleton`) : deux agents ne tournent jamais en parallèle dans la même session.

---

## Mode A — Tâche planifiée « au démarrage de session » (poste pilote / individuel)

**C'est ce qui est en place sur le poste pilote (DESKTOP-4KFP0H5).** L'agent tourne dans
la session de l'utilisateur → le **bureau à distance fonctionne directement** (pas de
bascule session 0). Idéal pour tester et pour un poste où quelqu'un est connecté.

Mise en place (PowerShell, dans la session de l'utilisateur) :
```powershell
$agentDir = "C:\Users\Haja\Documents\MCM_odoo\parc-monitoring\agent"
$pyw      = "$agentDir\.venv\Scripts\pythonw.exe"   # pythonw = sans fenêtre
$dataDir  = "C:\Users\Haja\Documents\TrueSightAgent"

setx TRUESIGHT_DATA_DIR "$dataDir" | Out-Null        # data dir persistant (config/state/log)

$action   = New-ScheduledTaskAction -Execute $pyw -Argument "-m truesight_agent" -WorkingDirectory $agentDir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "TrueSight Agent" -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force
Start-ScheduledTask -TaskName "TrueSight Agent"
```

Gestion :
```powershell
Get-ScheduledTask -TaskName "TrueSight Agent" | Select State
Stop-ScheduledTask  -TaskName "TrueSight Agent"     # arrêter
Start-ScheduledTask -TaskName "TrueSight Agent"     # relancer
Unregister-ScheduledTask -TaskName "TrueSight Agent" -Confirm:$false   # désinstaller
Get-Content "$env:TRUESIGHT_DATA_DIR\truesight-agent.log" -Tail 30     # journal
```

### Terminal & commandes en ADMINISTRATEUR (mode A)

Le terminal interactif (PowerShell/cmd) et les commandes héritent des **privilèges
du processus agent**. Pour un shell **administrateur** (élévation sans invite UAC),
ré-enregistrer la tâche avec `-RunLevel Highest` **depuis un PowerShell élevé**
(« Exécuter en tant qu'administrateur »), l'utilisateur devant être admin local :

```powershell
$agentDir = "C:\Users\Haja\Documents\MCM_odoo\parc-monitoring\agent"
$pyw      = "$agentDir\.venv\Scripts\pythonw.exe"
$action    = New-ScheduledTaskAction -Execute $pyw -Argument "-m truesight_agent" -WorkingDirectory $agentDir
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
             -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
             -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "TrueSight Agent" -Action $action -Trigger $trigger `
             -Settings $settings -Principal $principal -Force
Stop-ScheduledTask -TaskName "TrueSight Agent"; Start-ScheduledTask -TaskName "TrueSight Agent"
```

Le bureau à distance continue de fonctionner (session utilisateur). En **mode B**
(service SYSTEM), le terminal tourne directement en **SYSTEM** (privilèges encore
supérieurs à l'admin) — c'est le mode cible du parc.

Limites :
- Tourne **uniquement quand un utilisateur est connecté** (pas à l'écran de verrouillage sans session ouverte).
- `RunLevel Limited` (droits utilisateur standard) : verrouiller / message / déconnecter / redémarrer fonctionnent, mais le terminal n'est PAS admin → passer en `-RunLevel Highest` (ci-dessus) ou utiliser le mode B.

---

## Mode B — Service Windows SYSTEM (parc / production, déployé par GPO)

Pour le déploiement de masse : l'agent tourne en **service SYSTEM**, **toujours actif**
(même sans utilisateur connecté), démarrage automatique, reprise sur échec. C'est le
mode cible pour les 100+ postes.

1. **Construire l'exécutable** (sur un poste de build, dépendances installées) :
   ```powershell
   cd agent ; pip install -r requirements.txt ; .\build.ps1   # -> dist\truesight-agent.exe
   ```
2. **Installer le service** (en ADMINISTRATEUR) :
   ```powershell
   .\install-service.ps1   # copie vers C:\ProgramData\TrueSight, service "TrueSightAgent" (LocalSystem), démarrage auto + reprise
   ```
3. **Déploiement de masse** : `deploy\gpo-install.ps1` (copie exe+config.ini depuis un partage, installe et démarre le service) poussé par GPO/Intune. Le `config.ini` (avec `enrollment_token`) est poussé dans `C:\ProgramData\TrueSight`.

Spécificités du mode SYSTEM :
- Le **bureau à distance** nécessite la bascule session 0 → session interactive
  (`CreateProcessAsUser`, voir `remote/launcher.py`) — **à valider sur un poste pilote**
  avant déploiement de masse.
- Le **terminal** tourne en SYSTEM (shell **administrateur**) — puissant, à encadrer.
- La garde mono-instance empêche un doublon si une tâche planifiée Mode A coexiste :
  **ne pas cumuler** Mode A et Mode B sur le même poste.

---

## Recommandation
- **Pilote / tests** : Mode A (en place). Simple, bureau à distance direct.
- **Parc** : Mode B via GPO, après validation de la capture session 0 sur 1-2 postes pilotes.
