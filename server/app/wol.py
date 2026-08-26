"""Réveil à distance (Wake-on-LAN) d'un poste éteint.

Un serveur sur Internet ne peut pas réveiller une machine : le paquet magique
doit être émis **sur le réseau local du poste** (les routeurs ne relaient pas la
diffusion). On s'appuie donc sur un poste RELAIS : un autre agent, allumé, situé
au même emplacement — il reçoit une commande PowerShell ordinaire qui émet le
paquet. Aucun code agent supplémentaire n'est nécessaire (même approche que
l'onglet Processus).

Prérequis côté données :
  - l'adresse MAC de la cible, déjà collectée par l'inventaire
    (``HardwareInventory.mac_addresses``) ;
  - un **emplacement** renseigné sur les deux postes : c'est ce qui garantit
    qu'ils partagent un réseau local. Sans emplacement, on refuse plutôt que
    d'envoyer un paquet qui n'atteindra jamais sa cible.

Prérequis côté postes (à faire une fois, hors TrueSight) : Wake-on-LAN activé
dans l'UEFI et sur la carte réseau (« Autoriser ce périphérique à sortir
l'ordinateur du mode veille »).
"""
from __future__ import annotations

import re

# Format normalisé par le collecteur : 6 octets hexadécimaux séparés par « : ».
_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")

# Le paquet magique est court et l'émission est locale : quelques secondes suffisent.
WOL_TIMEOUT = 60


def normalize_macs(raw) -> list[str]:
    """Adresses MAC valides, normalisées et dédoublonnées (ordre conservé).

    Tout ce qui ne correspond pas exactement au format attendu est écarté : les
    valeurs sont ensuite RÉÉCRITES à partir de cette liste validée, jamais
    reprises telles quelles — aucune chaîne d'origine n'atteint PowerShell.
    """
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        mac = str(item or "").strip().upper().replace("-", ":")
        if not _MAC_RE.match(mac):
            continue
        # Une MAC tout à zéro n'identifie aucune carte réelle.
        if set(mac.replace(":", "")) <= {"0"}:
            continue
        if mac not in out:
            out.append(mac)
    return out


def build_wake_command(macs: list[str]) -> tuple[str, str, int]:
    """Construit la commande d'émission du paquet magique.

    Renvoie ``(shell, command_text, timeout)``. Le paquet est envoyé sur les
    ports 7 ET 9 (les deux conventions rencontrées selon les cartes réseau) en
    diffusion locale.
    """
    safe = normalize_macs(macs)
    if not safe:
        raise ValueError("aucune adresse MAC exploitable")

    # Littéral PowerShell : chaque MAC est déjà validée par _MAC_RE, donc ne peut
    # contenir ni quote ni métacaractère.
    array = ", ".join("'%s'" % m for m in safe)

    text = (
        "$macs = @(" + array + ")\n"
        "$sent = 0\n"
        "foreach ($m in $macs) {\n"
        "  try {\n"
        "    $b = $m.Split(':') | ForEach-Object { [Convert]::ToByte($_, 16) }\n"
        "    if ($b.Count -ne 6) { continue }\n"
        "    $packet = New-Object byte[] 102\n"
        "    for ($i = 0; $i -lt 6; $i++) { $packet[$i] = 0xFF }\n"
        "    for ($r = 0; $r -lt 16; $r++) {\n"
        "      for ($i = 0; $i -lt 6; $i++) { $packet[6 + $r * 6 + $i] = $b[$i] }\n"
        "    }\n"
        "    foreach ($port in 7, 9) {\n"
        "      $u = New-Object System.Net.Sockets.UdpClient\n"
        "      $u.EnableBroadcast = $true\n"
        "      $u.Connect([System.Net.IPAddress]::Broadcast, $port)\n"
        "      [void]$u.Send($packet, $packet.Length)\n"
        "      $u.Close()\n"
        "    }\n"
        "    $sent++\n"
        "    Write-Output ('Paquet magique envoye a ' + $m)\n"
        "  } catch {\n"
        "    Write-Output ('Echec pour ' + $m + ' : ' + $_.Exception.Message)\n"
        "  }\n"
        "}\n"
        "if ($sent -eq 0) { Write-Error 'Aucun paquet envoye'; exit 1 }\n"
        "Write-Output (\"Total : $sent paquet(s)\")\n"
    )
    return "powershell", text, WOL_TIMEOUT
