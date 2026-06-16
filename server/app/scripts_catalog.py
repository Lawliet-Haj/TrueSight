"""Bibliothèque de scripts prêts à l'emploi (exécutables en 1 clic).

Liste curée et sûre, orientée support quotidien. Chaque entrée est matérialisée
comme une commande normale via le pipeline existant (POST /agents/<id>/commands)
→ audit, résultat et timeout identiques aux commandes saisies à la main.

Champs :
- ``key``          : identifiant stable (pour l'UI) ;
- ``label``        : libellé affiché ;
- ``category``     : regroupement dans l'UI ;
- ``shell``        : 'powershell' ou 'cmd' ;
- ``command_text`` : la commande exécutée ;
- ``danger``       : True → l'UI demande une confirmation appuyée (action modifiante) ;
- ``timeout``      : délai max d'exécution (s).
"""

SCRIPTS = [
    # --- Réseau ---
    {"key": "flush-dns", "label": "Vider le cache DNS", "category": "Réseau",
     "shell": "cmd", "command_text": "ipconfig /flushdns", "timeout": 30},
    {"key": "ipconfig", "label": "Configuration IP (ipconfig /all)", "category": "Réseau",
     "shell": "cmd", "command_text": "ipconfig /all", "timeout": 30},
    {"key": "renew-ip", "label": "Renouveler l'adresse IP", "category": "Réseau",
     "shell": "cmd", "command_text": "ipconfig /release & ipconfig /renew", "timeout": 60, "danger": True},
    {"key": "flush-arp", "label": "Vider le cache ARP", "category": "Réseau",
     "shell": "cmd", "command_text": "arp -d *", "timeout": 30},
    {"key": "test-internet", "label": "Tester la connexion Internet", "category": "Réseau",
     "shell": "powershell",
     "command_text": "Test-NetConnection 8.8.8.8 | Format-List ComputerName,RemoteAddress,PingSucceeded",
     "timeout": 60},

    # --- Système ---
    {"key": "gpupdate", "label": "Forcer les stratégies (gpupdate)", "category": "Système",
     "shell": "cmd", "command_text": "gpupdate /force", "timeout": 180},
    {"key": "restart-explorer", "label": "Redémarrer l'explorateur", "category": "Système",
     "shell": "powershell", "command_text": "Stop-Process -Name explorer -Force; 'Explorateur redemarre'",
     "timeout": 30, "danger": True},
    {"key": "sessions", "label": "Sessions utilisateurs ouvertes", "category": "Système",
     "shell": "cmd", "command_text": "query user", "timeout": 30},
    {"key": "last-boot", "label": "Dernier démarrage", "category": "Système",
     "shell": "powershell", "command_text": "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime",
     "timeout": 30},
    {"key": "pending-reboot", "label": "Redémarrage en attente ?", "category": "Système",
     "shell": "powershell",
     "command_text": "if (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired') { 'Redemarrage REQUIS' } else { 'Aucun redemarrage en attente' }",
     "timeout": 30},

    # --- Impression ---
    {"key": "restart-spooler", "label": "Redémarrer le spouleur d'impression", "category": "Impression",
     "shell": "powershell", "command_text": "Restart-Service -Name Spooler -Force; 'Spouleur redemarre'",
     "timeout": 60},
    {"key": "list-printers", "label": "Lister les imprimantes", "category": "Impression",
     "shell": "powershell",
     "command_text": "Get-Printer | Select-Object Name,DriverName,PortName,PrinterStatus | Format-Table -AutoSize",
     "timeout": 60},

    # --- Maintenance / Disque ---
    {"key": "clear-temp", "label": "Vider les fichiers temporaires", "category": "Maintenance",
     "shell": "powershell",
     "command_text": "Remove-Item -Path \"$env:TEMP\\*\" -Recurse -Force -ErrorAction SilentlyContinue; 'Dossier TEMP vide'",
     "timeout": 120, "danger": True},
    {"key": "empty-recyclebin", "label": "Vider la corbeille", "category": "Maintenance",
     "shell": "powershell", "command_text": "Clear-RecycleBin -Force -ErrorAction SilentlyContinue; 'Corbeille videe'",
     "timeout": 60, "danger": True},
    {"key": "disk-usage", "label": "Espace disque", "category": "Maintenance",
     "shell": "powershell",
     "command_text": "Get-PSDrive -PSProvider FileSystem | Select-Object Name,@{n='Libre_Go';e={[math]::Round($_.Free/1GB,1)}},@{n='Total_Go';e={[math]::Round(($_.Used+$_.Free)/1GB,1)}} | Format-Table -AutoSize",
     "timeout": 30},

    # --- Sécurité / diagnostic ---
    {"key": "defender-status", "label": "État Windows Defender", "category": "Sécurité",
     "shell": "powershell",
     "command_text": "Get-MpComputerStatus | Select-Object AntivirusEnabled,RealTimeProtectionEnabled,AntivirusSignatureLastUpdated | Format-List",
     "timeout": 60},
    {"key": "system-errors", "label": "Erreurs système (24 h)", "category": "Sécurité",
     "shell": "powershell",
     "command_text": "Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2;StartTime=(Get-Date).AddDays(-1)} -MaxEvents 15 -ErrorAction SilentlyContinue | Select-Object TimeCreated,Id,ProviderName | Format-Table -AutoSize",
     "timeout": 60},
]


def public_catalog():
    """Catalogue normalisé pour l'UI (valeurs par défaut appliquées)."""
    out = []
    for s in SCRIPTS:
        out.append({
            "key": s["key"],
            "label": s["label"],
            "category": s["category"],
            "shell": s["shell"],
            "command_text": s["command_text"],
            "danger": bool(s.get("danger", False)),
            "timeout": int(s.get("timeout", 120)),
        })
    return out
