"""Constructeurs de commandes pour la gestion des correctifs Windows.

Comme pour le déploiement logiciel (cf. ``software_catalog``), l'installation des
correctifs Windows ne nécessite **aucun code agent dédié** : on réutilise le
pipeline de commandes existant (table ``commands`` → l'agent exécute le PowerShell
→ résultat via ``command_results``). Ce module se contente de produire, de façon
SÛRE, le ``command_text`` PowerShell.

Méthode d'installation : il n'existe pas de cmdlet PowerShell *inbox* pour
installer un KB précis (``winget`` n'a pas de mode correctif ; ``PSWindowsUpdate``
est un module non-inbox = dépendance externe à déployer, qu'on évite). On s'appuie
donc sur l'**API COM ``Microsoft.Update.Session``** — la même que celle déjà
utilisée pour la détection (``collectors._collect_windows_update``) : aucune
dépendance nouvelle.

Le redémarrage requis est signalé par le **code de sortie 3010**
(``IUpdateInstallationResult.RebootRequired``) ; le serveur l'interprète comme
« redémarrage en attente » et ne redémarre JAMAIS le poste de lui-même (un
utilisateur peut être en session).

Sécurité (priorité — RMM exposé, contrôle à distance, commandes SYSTEM) :
- chaque numéro KB est validé contre ``^KB\\d{6,8}$`` ;
- toute valeur dynamique est injectée comme littéral PowerShell entre quotes
  simples avec échappement (``''``), ce qui neutralise toute évasion de chaîne ;
- le PowerShell produit reste en ASCII pur, l'agent le transmet de toute façon en
  ``-EncodedCommand`` (UTF-16LE base64).
"""
from __future__ import annotations

import re

# Numéro de correctif : « KB5034441 » (6 à 8 chiffres). Insensible à la casse.
_KB_RE = re.compile(r"^KB\d{6,8}$", re.IGNORECASE)

# Délais (s). Une installation de correctifs peut être longue (téléchargement +
# install + configuration). Bornée côté endpoint (<= 3600).
PATCH_TIMEOUT = 1800
RESCAN_TIMEOUT = 180
REBOOT_STATUS_TIMEOUT = 30

# Modes d'installation acceptés.
INSTALL_MODES = ("all", "critical", "selected")


def valid_kb(value) -> bool:
    """Vrai si ``value`` ressemble à un numéro KB valide."""
    return isinstance(value, str) and bool(_KB_RE.match(value.strip()))


def normalize_kbs(kb_list) -> list[str]:
    """Normalise/valide une liste de KB en majuscules. Lève ``ValueError`` si l'un
    d'eux est mal formé."""
    out: list[str] = []
    for kb in (kb_list or []):
        kb_s = str(kb).strip().upper()
        if not _KB_RE.match(kb_s):
            raise ValueError("KB invalide : %r" % (kb,))
        out.append(kb_s)
    return out


def _ps_lit(value) -> str:
    """Encode une chaîne en littéral PowerShell sûr (quotes simples échappées).

    En PowerShell, une chaîne entre quotes simples n'interprète RIEN ; le seul
    caractère spécial est la quote simple, qui se double pour être littérale.
    Cela rend l'injection impossible quel que soit le contenu.
    """
    return "'" + str(value).replace("'", "''") + "'"


# --- Constructeurs de commandes ------------------------------------------
# Chaque builder renvoie un tuple (shell, command_text, timeout_seconds).
def build_install(mode: str, kb_list=None):
    """Installe les correctifs Windows en attente selon ``mode``.

    - ``'all'``      : tous les correctifs (IsInstalled=0 and IsHidden=0) ;
    - ``'critical'`` : sévérité Microsoft Critical ou Important ;
    - ``'selected'`` : uniquement les KB de ``kb_list`` (validés).

    Le script COM télécharge puis installe la sélection et **sort en 3010 si un
    redémarrage est requis** (le serveur en déduit l'état « reboot_pending »).
    Lève ``ValueError`` si ``mode`` est invalide ou si la liste de KB l'est.
    """
    mode = (mode or "").strip().lower()
    if mode not in INSTALL_MODES:
        raise ValueError("mode invalide (all | critical | selected)")

    kb_items: list[str] = []
    if mode == "selected":
        kb_items = normalize_kbs(kb_list)
        if not kb_items:
            raise ValueError("kb_list requis (non vide) pour le mode 'selected'")

    kb_array = "@(" + ",".join(_ps_lit(k) for k in kb_items) + ")"
    cmd = (
        "$ErrorActionPreference='Stop'; "
        f"$mode = {_ps_lit(mode)}; "
        f"$kbs = {kb_array}; "
        "try { "
        "$session = New-Object -ComObject Microsoft.Update.Session; "
        "$searcher = $session.CreateUpdateSearcher(); "
        "$res = $searcher.Search('IsInstalled=0 and IsHidden=0'); "
        "$sel = New-Object -ComObject Microsoft.Update.UpdateColl; "
        "foreach ($u in $res.Updates) { $take=$false; "
        "if ($mode -eq 'all') { $take=$true } "
        "elseif ($mode -eq 'critical') { if (@('Critical','Important') -contains $u.MsrcSeverity) { $take=$true } } "
        "else { foreach ($k in $u.KBArticleIDs) { if ($kbs -contains ('KB'+$k)) { $take=$true } } } "
        "if ($take) { if (-not $u.EulaAccepted) { try { $u.AcceptEula() } catch {} } $null=$sel.Add($u) } } "
        "if ($sel.Count -eq 0) { Write-Output 'Aucun correctif correspondant a installer.'; exit 0 } "
        "Write-Output ('Correctifs a installer: ' + $sel.Count); "
        "$dl = $session.CreateUpdateDownloader(); $dl.Updates = $sel; $dr = $dl.Download(); "
        "Write-Output ('Telechargement: code ' + $dr.ResultCode); "
        "$inst = $session.CreateUpdateInstaller(); $inst.Updates = $sel; $ir = $inst.Install(); "
        "Write-Output ('Installation: code ' + $ir.ResultCode + ', reboot=' + $ir.RebootRequired); "
        "for ($i=0; $i -lt $sel.Count; $i++) { $ur = $ir.GetUpdateResult($i); "
        "Write-Output (' - ' + $sel.Item($i).Title + ' => code ' + $ur.ResultCode) } "
        "if ($ir.RebootRequired) { Write-Output 'REDEMARRAGE REQUIS'; exit 3010 } "
        "if (@(2,3) -contains $ir.ResultCode) { exit 0 } else { exit 1 } "
        "} catch { Write-Output ('ERREUR: ' + $_.Exception.Message); exit 1 }"
    )
    return ("powershell", cmd, PATCH_TIMEOUT)


# Marqueur qui précède le bloc JSON dans la sortie du rescan : l'UI le repère
# pour parser la liste des correctifs et la rendre cochable dans le tableau.
RESCAN_JSON_MARKER = "===PATCHES_JSON==="


def build_rescan():
    """Recherche read-only des correctifs en attente (rafraîchissement à la
    demande). N'installe rien. Émet un récap lisible puis, après le marqueur
    ``RESCAN_JSON_MARKER``, un tableau JSON ``[{kb,title,severity,size_mb,
    reboot_required}]`` que le dashboard parse pour peupler la liste cochable —
    fonctionne quel que soit l'âge de l'agent (commande construite côté serveur)."""
    cmd = (
        "$ErrorActionPreference='Stop'; "
        "try { "
        "$session = New-Object -ComObject Microsoft.Update.Session; "
        "$searcher = $session.CreateUpdateSearcher(); "
        "$res = $searcher.Search('IsInstalled=0 and IsHidden=0'); "
        "Write-Output ('Correctifs en attente: ' + $res.Updates.Count); "
        "$items = foreach ($u in $res.Updates) { "
        "$kb=''; foreach ($k in $u.KBArticleIDs) { $kb='KB'+$k; break }; "
        "$mb=0; try { $mb=[math]::Round($u.MaxDownloadSize/1MB,1) } catch {}; "
        "$sev=$u.MsrcSeverity; if (-not $sev) { $sev='Unknown' }; "
        "$rb=$false; try { $rb=([int]$u.InstallationBehavior.RebootBehavior -ne 0) } catch {}; "
        "[pscustomobject]@{ kb=$kb; title=[string]$u.Title; severity=$sev; size_mb=$mb; reboot_required=$rb } }; "
        "$json='[' + (($items | ForEach-Object { $_ | ConvertTo-Json -Compress }) -join ',') + ']'; "
        f"Write-Output {_ps_lit(RESCAN_JSON_MARKER)}; "
        "Write-Output $json; "
        "exit 0 "
        "} catch { Write-Output ('ERREUR: ' + $_.Exception.Message); exit 1 }"
    )
    return ("powershell", cmd, RESCAN_TIMEOUT)


def build_reboot_status():
    """Indique si un redémarrage est en attente (clé registre WindowsUpdate).

    Même requête que le script ``pending-reboot`` de ``scripts_catalog``.
    """
    cmd = (
        "if (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\"
        "WindowsUpdate\\Auto Update\\RebootRequired') "
        "{ 'Redemarrage REQUIS' } else { 'Aucun redemarrage en attente' }"
    )
    return ("powershell", cmd, REBOOT_STATUS_TIMEOUT)
