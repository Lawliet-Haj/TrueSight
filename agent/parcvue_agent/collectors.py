"""Collecteurs de l'agent ParcVue : métriques, matériel, logiciels.

Chaque fonction renvoie une structure conforme au SPEC :
- ``collect_metrics()``  → dict du SPEC 2.2 (heartbeat),
- ``collect_hardware()`` → dict du SPEC 2.3 (inventaire matériel),
- ``collect_software()`` → liste du SPEC 2.3 (inventaire logiciel).

Robustesse : chaque collecteur est tolérant aux pannes (WMI absent, lecteur
inaccessible, clé de registre manquante) et ne lève jamais : il renvoie des
valeurs par défaut propres plutôt que de crasher l'agent.
"""

from __future__ import annotations

import datetime
import logging
import re
import time

import psutil

_logger = logging.getLogger("parcvue.collectors")

# ----------------------------------------------------------------------------
# Imports Windows tolérants.
# ----------------------------------------------------------------------------
try:
    import winreg  # type: ignore
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore

try:
    import wmi  # type: ignore
except ImportError:  # pragma: no cover
    wmi = None  # type: ignore

try:
    import pythoncom  # type: ignore - nécessaire pour WMI dans un thread.
except ImportError:  # pragma: no cover
    pythoncom = None  # type: ignore


def _round2(value: float) -> float:
    """Arrondit à 2 décimales (présentation homogène des Go/pourcentages)."""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


# ============================================================================
# 2.2 — Métriques (heartbeat)
# ============================================================================
def collect_metrics() -> dict:
    """Collecte les métriques live (SPEC 2.2).

    Renvoie EXACTEMENT les clés :
      cpu_pct, ram_used_pct, ram_total_mb, disk_free, uptime_seconds, logged_in_user
    """
    cpu_pct = _collect_cpu_pct()
    ram_used_pct, ram_total_mb = _collect_ram()
    disk_free = _collect_disk_free()
    uptime_seconds = _collect_uptime()
    logged_in_user = _collect_logged_in_user()

    return {
        "cpu_pct": cpu_pct,
        "ram_used_pct": ram_used_pct,
        "ram_total_mb": ram_total_mb,
        "disk_free": disk_free,
        "uptime_seconds": uptime_seconds,
        "logged_in_user": logged_in_user,
    }


def _collect_cpu_pct() -> float:
    """Pourcentage d'utilisation CPU global (moyenné sur un court intervalle)."""
    try:
        # interval=1 : mesure réelle sur 1 s (évite le 0.0 du premier appel).
        return _round2(psutil.cpu_percent(interval=1))
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Mesure CPU impossible : %s", exc)
        return 0.0


def _collect_ram() -> tuple[float, int]:
    """Retourne (ram_used_pct, ram_total_mb)."""
    try:
        vm = psutil.virtual_memory()
        used_pct = _round2(vm.percent)
        total_mb = int(round(vm.total / (1024 * 1024)))
        return used_pct, total_mb
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Mesure RAM impossible : %s", exc)
        return 0.0, 0


def _collect_disk_free() -> dict:
    """Go libres par lecteur fixe local : {"C:": 42.1, "D:": 870.3}."""
    free_by_drive: dict[str, float] = {}
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Énumération des partitions impossible : %s", exc)
        return free_by_drive

    for part in partitions:
        # On ne garde que les disques fixes locaux (pas CD-ROM, réseau, amovible).
        if not _is_fixed_drive(part):
            continue
        drive_label = _drive_label(part.mountpoint)
        try:
            usage = psutil.disk_usage(part.mountpoint)
            free_by_drive[drive_label] = _round2(usage.free / (1024 ** 3))
        except (PermissionError, OSError) as exc:
            # Lecteur monté mais non prêt (ex. lecteur de carte vide) : on ignore.
            _logger.debug("Disque %s inaccessible : %s", drive_label, exc)
            continue

    return free_by_drive


def _is_fixed_drive(part) -> bool:
    """Détermine si une partition est un disque fixe local."""
    opts = (part.opts or "").lower()
    fstype = (part.fstype or "").lower()
    # 'cdrom' ou absence de système de fichiers → lecteur amovible/optique.
    if "cdrom" in opts or not fstype:
        return False
    if "removable" in opts:
        return False
    return True


def _drive_label(mountpoint: str) -> str:
    """Normalise un point de montage Windows en libellé 'C:'."""
    mp = (mountpoint or "").strip()
    # 'C:\\' → 'C:' ; on conserve la lettre + ':' attendue par le serveur.
    match = re.match(r"^([A-Za-z]:)", mp)
    if match:
        return match.group(1).upper()
    return mp.rstrip("\\/")


def _collect_uptime() -> int:
    """Uptime en secondes (basé sur psutil.boot_time)."""
    try:
        boot = psutil.boot_time()
        return max(0, int(time.time() - boot))
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Calcul de l'uptime impossible : %s", exc)
        return 0


def _collect_logged_in_user() -> str:
    """Utilisateur interactif connecté (ex. 'MEDICOFI\\jdupont') ou '' si aucun."""
    try:
        users = psutil.users()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Énumération des sessions impossible : %s", exc)
        return ""

    for user in users:
        name = (user.name or "").strip()
        if name:
            # psutil renvoie déjà 'DOMAINE\\user' sous Windows dans la plupart des cas.
            return name
    return ""


# ============================================================================
# 2.3 — Inventaire matériel
# ============================================================================
def collect_hardware() -> dict:
    """Collecte l'inventaire matériel (SPEC 2.3).

    Clés : manufacturer, model, serial_number, cpu_model, cpu_cores,
           ram_total_mb, disks, mac_addresses.

    Utilise WMI quand disponible (Win32_ComputerSystem / BIOS / Processor),
    avec un repli propre via psutil/platform si WMI est absent.
    """
    hardware = {
        "manufacturer": "",
        "model": "",
        "serial_number": "",
        "cpu_model": "",
        "cpu_cores": _cpu_cores_fallback(),
        "ram_total_mb": _ram_total_mb_fallback(),
        "disks": _collect_disks_detail(),
        "mac_addresses": _collect_mac_addresses(),
    }

    _enrich_hardware_with_wmi(hardware)
    return hardware


def _cpu_cores_fallback() -> int:
    """Nombre de cœurs logiques (repli si WMI indisponible)."""
    try:
        return int(psutil.cpu_count(logical=True) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _ram_total_mb_fallback() -> int:
    """RAM totale en Mo (repli si WMI indisponible)."""
    try:
        return int(round(psutil.virtual_memory().total / (1024 * 1024)))
    except Exception:  # noqa: BLE001
        return 0


def _enrich_hardware_with_wmi(hardware: dict) -> None:
    """Complète le dict matériel via WMI ; silencieux si WMI indisponible."""
    if wmi is None:
        _logger.info("Module WMI absent : inventaire matériel partiel (repli psutil).")
        return

    # WMI/COM doit être initialisé dans le thread courant.
    com_initialized = False
    try:
        if pythoncom is not None:
            pythoncom.CoInitialize()
            com_initialized = True

        conn = wmi.WMI()

        # Win32_ComputerSystem : fabricant, modèle, RAM totale.
        try:
            for cs in conn.Win32_ComputerSystem():
                hardware["manufacturer"] = _clean(cs.Manufacturer)
                hardware["model"] = _clean(cs.Model)
                if cs.TotalPhysicalMemory:
                    try:
                        hardware["ram_total_mb"] = int(int(cs.TotalPhysicalMemory) / (1024 * 1024))
                    except (TypeError, ValueError):
                        pass
                break
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Win32_ComputerSystem indisponible : %s", exc)

        # Win32_BIOS : numéro de série.
        try:
            for bios in conn.Win32_BIOS():
                hardware["serial_number"] = _clean(bios.SerialNumber)
                break
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Win32_BIOS indisponible : %s", exc)

        # Win32_Processor : modèle CPU + nombre de cœurs.
        try:
            cpu_models = []
            total_cores = 0
            for proc in conn.Win32_Processor():
                if proc.Name:
                    cpu_models.append(_clean(proc.Name))
                # NumberOfLogicalProcessors reflète les threads logiques.
                cores = proc.NumberOfLogicalProcessors or proc.NumberOfCores or 0
                try:
                    total_cores += int(cores)
                except (TypeError, ValueError):
                    pass
            if cpu_models:
                hardware["cpu_model"] = cpu_models[0]
            if total_cores > 0:
                hardware["cpu_cores"] = total_cores
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Win32_Processor indisponible : %s", exc)

    except Exception as exc:  # noqa: BLE001 - WMI peut échouer globalement.
        _logger.warning("Connexion WMI impossible (inventaire partiel) : %s", exc)
    finally:
        if com_initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass


def _collect_disks_detail() -> list:
    """Liste des disques fixes : [{"drive":"C:","total_gb":237.5,"free_gb":42.1}]."""
    disks = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Énumération des partitions impossible : %s", exc)
        return disks

    for part in partitions:
        if not _is_fixed_drive(part):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append({
            "drive": _drive_label(part.mountpoint),
            "total_gb": _round2(usage.total / (1024 ** 3)),
            "free_gb": _round2(usage.free / (1024 ** 3)),
        })
    return disks


def _collect_mac_addresses() -> list:
    """Adresses MAC des cartes réseau physiques (sans doublon, ordre stable)."""
    macs: list[str] = []
    seen: set[str] = set()
    try:
        nics = psutil.net_if_addrs()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Énumération des cartes réseau impossible : %s", exc)
        return macs

    for _iface, addresses in nics.items():
        for addr in addresses:
            # AF_LINK (psutil.AF_LINK) porte l'adresse MAC.
            if getattr(addr, "family", None) != getattr(psutil, "AF_LINK", -1):
                continue
            mac = (addr.address or "").strip().upper().replace("-", ":")
            # On ignore les MAC vides ou nulles (00:00:...).
            if not mac or set(mac.replace(":", "")) <= {"0"}:
                continue
            # Format MAC plausible (6 octets hex).
            if not re.match(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", mac):
                continue
            if mac not in seen:
                seen.add(mac)
                macs.append(mac)
    return macs


# ============================================================================
# 2.3 — Inventaire logiciel
# ============================================================================
# Clés de registre où Windows liste les logiciels installés.
_UNINSTALL_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def collect_software() -> list:
    """Collecte les logiciels installés (SPEC 2.3).

    Parcourt les clés ``Uninstall`` :
      - HKLM 64 bits (KEY_WOW64_64KEY),
      - HKLM 32 bits (KEY_WOW64_32KEY),
      - HKCU (logiciels par utilisateur).
    Déduplique sur (nom, version) et renvoie une liste de dicts :
      {"name", "version", "publisher", "install_date"} (date YYYY-MM-DD ou null).
    """
    if winreg is None:
        _logger.info("winreg indisponible : inventaire logiciel vide.")
        return []

    collected: dict[tuple[str, str], dict] = {}

    hives = [
        (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_PATH, winreg.KEY_WOW64_64KEY, "HKLM/64"),
        (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_PATH, winreg.KEY_WOW64_32KEY, "HKLM/32"),
        (winreg.HKEY_CURRENT_USER, _UNINSTALL_PATH, 0, "HKCU"),
    ]

    for root, subpath, wow_flag, label in hives:
        try:
            _read_uninstall_hive(root, subpath, wow_flag, collected)
        except OSError as exc:
            # La clé peut ne pas exister (ex. HKCU vide) : non bloquant.
            _logger.debug("Clé Uninstall %s absente/inaccessible : %s", label, exc)

    software = list(collected.values())
    # Tri par nom pour une sortie stable et lisible.
    software.sort(key=lambda item: (item.get("name") or "").lower())
    _logger.info("Inventaire logiciel : %d entrées collectées.", len(software))
    return software


def _read_uninstall_hive(root, subpath: str, wow_flag: int, collected: dict) -> None:
    """Lit une clé Uninstall et alimente ``collected`` (dédup sur nom+version)."""
    access = winreg.KEY_READ | wow_flag
    with winreg.OpenKey(root, subpath, 0, access) as base_key:
        index = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(base_key, index)
            except OSError:
                break  # plus de sous-clés
            index += 1
            try:
                with winreg.OpenKey(base_key, subkey_name, 0, access) as subkey:
                    entry = _parse_uninstall_entry(subkey)
            except OSError:
                continue
            if entry is None:
                continue
            key = ((entry["name"] or "").lower(), (entry["version"] or "").lower())
            if key not in collected:
                collected[key] = entry


def _parse_uninstall_entry(subkey) -> dict | None:
    """Construit une entrée logicielle depuis une sous-clé Uninstall.

    Filtre les mises à jour système, composants masqués et entrées sans nom.
    Retourne None si l'entrée doit être ignorée.
    """
    name = _reg_value(subkey, "DisplayName")
    if not name:
        return None

    # On ignore les entrées marquées comme système / masquées / mises à jour.
    system_component = _reg_value(subkey, "SystemComponent")
    if system_component in ("1", 1):
        return None
    release_type = (_reg_value(subkey, "ReleaseType") or "").lower()
    if release_type in ("security update", "update rollup", "hotfix"):
        return None
    # WindowsInstaller + ParentKeyName : composants liés à un parent (sous-éléments).
    if _reg_value(subkey, "ParentKeyName"):
        return None

    version = _reg_value(subkey, "DisplayVersion") or ""
    publisher = _reg_value(subkey, "Publisher") or ""
    install_date = _normalize_install_date(_reg_value(subkey, "InstallDate"))

    return {
        "name": name.strip(),
        "version": version.strip(),
        "publisher": publisher.strip(),
        "install_date": install_date,
    }


def _reg_value(subkey, value_name: str):
    """Lit une valeur de registre ; renvoie None si absente."""
    try:
        value, _type = winreg.QueryValueEx(subkey, value_name)
        return value
    except OSError:
        return None


def _normalize_install_date(raw) -> str | None:
    """Normalise une date d'installation en 'YYYY-MM-DD' ou None.

    Le registre stocke généralement 'YYYYMMDD' (ex. '20260112').
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None

    # Format compact 'YYYYMMDD'.
    if re.match(r"^\d{8}$", text):
        try:
            dt = datetime.datetime.strptime(text, "%Y%m%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    # Format déjà ISO 'YYYY-MM-DD'.
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        try:
            datetime.datetime.strptime(text, "%Y-%m-%d")
            return text
        except ValueError:
            return None

    # Format inconnu : on préfère null à une donnée erronée.
    return None


def _clean(value) -> str:
    """Nettoie une valeur WMI (None → '', trim)."""
    if value is None:
        return ""
    return str(value).strip()
