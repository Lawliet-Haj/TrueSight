"""Gestion des comptes utilisateurs locaux Windows (lister / créer / supprimer).

Comme le déploiement logiciel, ces opérations passent par le pipeline de commandes
existant : ce module construit, de façon SÛRE, le PowerShell exécuté par l'agent.

Sécurité :
- les valeurs dynamiques sont injectées en littéraux PowerShell (quotes simples
  échappées) → pas d'injection possible ;
- PowerShell ASCII pur (transmis en ``-EncodedCommand`` par l'agent) ;
- le groupe « Administrateurs » est visé par son SID (``S-1-5-32-544``) → insensible
  à la langue de Windows ;
- découpage des chemins/membres via ``.Split([char]92)`` pour éviter toute
  gymnastique d'échappement de l'antislash.

ATTENTION (création) : créer un compte transmet un mot de passe à l'agent ; il
figure donc dans le ``command_text`` de la commande (nécessaire à l'exécution).
Le mot de passe n'est JAMAIS journalisé dans l'audit (cf. endpoint). À utiliser
avec un mot de passe temporaire.
"""
from __future__ import annotations

import re

# Nom d'utilisateur local : conservateur (lettres/chiffres + . _ -), 1 à 20 car.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$")

LIST_TIMEOUT = 30
MUTATE_TIMEOUT = 60
_ADMIN_SID = "S-1-5-32-544"  # groupe Administrateurs (constant, toutes langues)


def valid_username(name) -> bool:
    return isinstance(name, str) and bool(_USERNAME_RE.match(name))


def clean_text(value, limit: int = 120) -> str:
    """Nettoie un champ libre (nom complet) : retire les caractères de contrôle."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit]


def _ps_lit(value) -> str:
    """Littéral PowerShell sûr (quotes simples doublées)."""
    return "'" + str(value).replace("'", "''") + "'"


def build_list():
    """Liste les comptes locaux + état + appartenance au groupe Administrateurs."""
    cmd = (
        "$ProgressPreference='SilentlyContinue'; "
        "$admins=@(); try { $g=Get-LocalGroup -SID '" + _ADMIN_SID + "'; "
        "$admins=@(Get-LocalGroupMember -Group $g.Name | ForEach-Object { ($_.Name.Split([char]92))[-1] }) } catch {}; "
        "Get-LocalUser | ForEach-Object { [pscustomobject]@{ "
        "name=$_.Name; enabled=[bool]$_.Enabled; description=$_.Description; "
        "last_logon=$(if ($_.LastLogon) { $_.LastLogon.ToString('o') } else { $null }); "
        "admin=($admins -contains $_.Name) } } | ConvertTo-Json -Compress"
    )
    return ("powershell", cmd, LIST_TIMEOUT)


def build_create(username: str, password: str, full_name: str = "", administrator: bool = False):
    """Crée un compte local (New-LocalUser) ; l'ajoute aux Administrateurs si demandé."""
    parts = [
        "$ErrorActionPreference='Stop'; ",
        f"$u = {_ps_lit(username)}; ",
        f"$sec = ConvertTo-SecureString {_ps_lit(password)} -AsPlainText -Force; ",
    ]
    create = "New-LocalUser -Name $u -Password $sec -Description 'Compte cree via TrueSight'"
    if full_name:
        create += f" -FullName {_ps_lit(full_name)}"
    create += " | Out-Null; "
    parts.append(create)
    if administrator:
        parts.append("$g = Get-LocalGroup -SID '" + _ADMIN_SID + "'; "
                     "Add-LocalGroupMember -Group $g.Name -Member $u | Out-Null; ")
    parts.append("Write-Output ('Compte cree: ' + $u)")
    return ("powershell", "".join(parts), MUTATE_TIMEOUT)


def build_delete(username: str, remove_profile: bool = False):
    """Supprime un compte local (Remove-LocalUser) ; optionnellement son profil."""
    parts = [
        "$ErrorActionPreference='Stop'; ",
        f"$u = {_ps_lit(username)}; ",
        "Remove-LocalUser -Name $u; ",
    ]
    if remove_profile:
        parts.append(
            "Get-CimInstance Win32_UserProfile | "
            "Where-Object { $_.LocalPath -and ($_.LocalPath.Split([char]92))[-1] -ieq $u } | "
            "ForEach-Object { Remove-CimInstance -InputObject $_ }; "
        )
    parts.append("Write-Output ('Compte supprime: ' + $u)")
    return ("powershell", "".join(parts), MUTATE_TIMEOUT)
