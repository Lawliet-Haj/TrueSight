"""Catalogue et constructeurs de commandes pour le déploiement logiciel.

Le déploiement (installation / désinstallation silencieuse) ne nécessite AUCUN
code agent dédié : on réutilise le pipeline de commandes existant (table
``commands`` → l'agent exécute le PowerShell → résultat via ``command_results``).
Ce module se contente de produire, de façon SÛRE, le ``command_text`` PowerShell.

Sécurité (contexte médical — priorité) :
- les identifiants winget sont validés contre un jeu de caractères strict ;
- les URL d'installeur sont restreintes au HTTPS et à un jeu de caractères sûr ;
- toute valeur dynamique est injectée comme littéral PowerShell entre quotes
  simples avec échappement (``''``), ce qui neutralise toute évasion de chaîne ;
- le PowerShell produit reste en ASCII pur (cohérent avec ``scripts_catalog``),
  l'agent le transmet de toute façon en ``-EncodedCommand`` (UTF-16LE base64).

L'agent exécute la commande dans la boucle commandes (thread distinct du
heartbeat) : une installation longue ne fait donc jamais passer le poste
« hors ligne ». Le code de sortie pilote le statut (0 → done, sinon error).
"""
from __future__ import annotations

import re

# --- Validation -----------------------------------------------------------
# Identifiants winget : « Google.Chrome », « 7zip.7zip », « Notepad++.Notepad++ »…
_WINGET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")
# URL d'installeur : HTTPS uniquement, jeu de caractères d'URL sûr (ni quote, ni
# espace, ni caractère de shell). La valeur est de toute façon ré-encodée en
# littéral PS, mais on refuse en amont tout ce qui n'a rien à faire dans une URL.
_URL_RE = re.compile(r"^https://[A-Za-z0-9._~:/?#@!$&()*+,;=%-]{4,2048}$")

# Délais (s). Installation = téléchargement + install possiblement longs ;
# désinstallation plus courte. Bornés côté endpoint (<= 3600).
INSTALL_TIMEOUT = 900
UNINSTALL_TIMEOUT = 300


def valid_winget_id(value) -> bool:
    return isinstance(value, str) and bool(_WINGET_ID_RE.match(value))


def valid_url(value) -> bool:
    return isinstance(value, str) and bool(_URL_RE.match(value))


def clean_name(value, limit: int = 256) -> str:
    """Nettoie un nom de logiciel / des arguments : retire les caractères de
    contrôle (dont \\r\\n\\t) et tronque. Le résultat sera encodé en littéral PS."""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    return cleaned[:limit]


def _ps_lit(value) -> str:
    """Encode une chaîne en littéral PowerShell sûr (quotes simples échappées).

    En PowerShell, une chaîne entre quotes simples n'interprète RIEN ; le seul
    caractère spécial est la quote simple, qui se double pour être littérale.
    Cela rend l'injection impossible quel que soit le contenu.
    """
    return "'" + str(value).replace("'", "''") + "'"


# Localisation de winget sous le service SYSTEM : l'alias per-user
# (%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe) n'est pas dans le PATH de
# SYSTEM. On résout le binaire réel dans WindowsApps (dernière version installée).
_WINGET_LOCATE = (
    "$ProgressPreference='SilentlyContinue'; "
    "$wg = Get-ChildItem "
    "\"$env:ProgramFiles\\WindowsApps\\Microsoft.DesktopAppInstaller_*__8wekyb3d8bbwe\\winget.exe\" "
    "-ErrorAction SilentlyContinue | Sort-Object FullName | Select-Object -Last 1; "
    "if (-not $wg) { Write-Output 'ERREUR: App Installer (winget) introuvable sur ce poste. "
    "Utilisez une URL MSI/EXE.'; exit 2 }; $wg = $wg.FullName; "
)


# --- Catalogue curé -------------------------------------------------------
# Applications métier courantes. Chaque entrée s'installe via winget (scope
# machine quand l'installeur le permet, repli automatique sinon).
CATALOG = [
    {"key": "chrome", "label": "Google Chrome", "category": "Navigateurs", "winget_id": "Google.Chrome"},
    {"key": "firefox", "label": "Mozilla Firefox", "category": "Navigateurs", "winget_id": "Mozilla.Firefox"},
    {"key": "reader", "label": "Adobe Acrobat Reader", "category": "Bureautique", "winget_id": "Adobe.Acrobat.Reader.64-bit"},
    {"key": "libreoffice", "label": "LibreOffice", "category": "Bureautique", "winget_id": "TheDocumentFoundation.LibreOffice"},
    {"key": "7zip", "label": "7-Zip", "category": "Outils", "winget_id": "7zip.7zip"},
    {"key": "vlc", "label": "VLC media player", "category": "Outils", "winget_id": "VideoLAN.VLC"},
    {"key": "notepadpp", "label": "Notepad++", "category": "Outils", "winget_id": "Notepad++.Notepad++"},
    {"key": "greenshot", "label": "Greenshot (captures d'écran)", "category": "Outils", "winget_id": "Greenshot.Greenshot"},
    {"key": "powertoys", "label": "Microsoft PowerToys", "category": "Outils", "winget_id": "Microsoft.PowerToys"},
    {"key": "zoom", "label": "Zoom", "category": "Communication", "winget_id": "Zoom.Zoom"},
    {"key": "teams", "label": "Microsoft Teams", "category": "Communication", "winget_id": "Microsoft.Teams"},
    {"key": "anydesk", "label": "AnyDesk", "category": "Accès distant", "winget_id": "AnyDeskSoftwareGmbH.AnyDesk"},
]


def public_catalog():
    """Catalogue normalisé pour l'UI."""
    return [dict(item) for item in CATALOG]


def catalog_winget_id(key) -> str | None:
    """Renvoie l'ID winget d'une entrée de catalogue, ou None si inconnue."""
    for item in CATALOG:
        if item["key"] == key:
            return item["winget_id"]
    return None


# --- Constructeurs de commandes ------------------------------------------
# Chaque builder renvoie un tuple (shell, command_text, timeout_seconds).
def build_winget_install(winget_id: str):
    """Installation silencieuse via winget (scope machine + repli sans scope)."""
    cmd = (
        _WINGET_LOCATE
        + f"$id = {_ps_lit(winget_id)}; "
        "$wargs = @('install','-e','--id',$id,'--silent','--accept-package-agreements',"
        "'--accept-source-agreements','--disable-interactivity'); "
        "Write-Output ('Installation winget: ' + $id); "
        "& $wg @wargs --scope machine; $code = $LASTEXITCODE; "
        "if ($code -ne 0) { Write-Output ('Repli sans --scope machine (code ' + $code + ')'); "
        "& $wg @wargs; $code = $LASTEXITCODE }; "
        "Write-Output ('winget exit=' + $code); exit $code"
    )
    return ("powershell", cmd, INSTALL_TIMEOUT)


def build_winget_uninstall(winget_id: str | None = None, name: str | None = None):
    """Désinstallation silencieuse via winget (par ID ``-e`` ou par nom)."""
    if winget_id:
        sel = f"'-e','--id',{_ps_lit(winget_id)}"
    else:
        sel = f"'--name',{_ps_lit(name)}"
    cmd = (
        _WINGET_LOCATE
        + f"$wargs = @('uninstall',{sel},'--silent','--accept-source-agreements','--disable-interactivity'); "
        "& $wg @wargs; $code = $LASTEXITCODE; "
        "Write-Output ('winget exit=' + $code); exit $code"
    )
    return ("powershell", cmd, UNINSTALL_TIMEOUT)


def build_registry_uninstall(name: str):
    """Désinstallation silencieuse à partir du nom affiché (base de registre).

    Cherche l'entrée dans les clés Uninstall (64 + 32 bits), utilise
    ``QuietUninstallString`` si présent, sinon reconstruit un ``msiexec /x {GUID}
    /qn`` pour les produits MSI. Échoue proprement (exit != 0) sinon.
    """
    cmd = (
        f"$name = {_ps_lit(name)}; "
        "$paths = @('HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
        "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'); "
        "$app = Get-ItemProperty $paths -ErrorAction SilentlyContinue | "
        "Where-Object { $_.DisplayName -eq $name } | Select-Object -First 1; "
        "if (-not $app) { Write-Output ('Introuvable dans la base de desinstallation: ' + $name); exit 3 }; "
        "$u = $app.QuietUninstallString; "
        "if (-not $u -and $app.UninstallString -match 'msiexec') { "
        "$g = [regex]::Match($app.UninstallString, '\\{[0-9A-Fa-f-]+\\}').Value; "
        "if ($g) { $u = 'msiexec.exe /x ' + $g + ' /qn /norestart' } }; "
        "if (-not $u) { Write-Output ('Pas de desinstallation silencieuse connue pour: ' + $name); exit 4 }; "
        "Write-Output ('Desinstallation: ' + $u); "
        "& cmd.exe /c $u; $code = $LASTEXITCODE; "
        "Write-Output ('exit=' + $code); exit $code"
    )
    return ("powershell", cmd, UNINSTALL_TIMEOUT)


def build_url_install(url: str, exe_args: str | None = None):
    """Installation depuis une URL MSI/EXE : téléchargement TEMP → exécution
    silencieuse → nettoyage. MSI → ``msiexec /i /qn`` ; EXE → arguments fournis
    (défaut ``/S``)."""
    is_msi = url.split("?", 1)[0].lower().endswith(".msi")
    ext = ".msi" if is_msi else ".exe"
    if is_msi:
        run = ("$p = Start-Process 'msiexec.exe' -ArgumentList @('/i', $dst, '/qn', '/norestart') "
               "-Wait -PassThru; $code = $p.ExitCode")
    else:
        args = clean_name(exe_args, 200) or "/S"
        run = f"$p = Start-Process $dst -ArgumentList {_ps_lit(args)} -Wait -PassThru; $code = $p.ExitCode"
    cmd = (
        "$ProgressPreference='SilentlyContinue'; "
        f"$url = {_ps_lit(url)}; "
        f"$dst = Join-Path $env:TEMP ('truesight_inst_' + [guid]::NewGuid().ToString('N') + '{ext}'); "
        "try { Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing } "
        "catch { Write-Output ('Telechargement echoue: ' + $_.Exception.Message); exit 5 }; "
        "Write-Output ('Telecharge: ' + $dst); "
        f"{run}; "
        "Remove-Item $dst -Force -ErrorAction SilentlyContinue; "
        "Write-Output ('installeur exit=' + $code); exit $code"
    )
    return ("powershell", cmd, INSTALL_TIMEOUT)
