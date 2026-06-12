"""Lecture de la configuration (config.ini) et de l'état (state.json) de l'agent.

- En production : tout se trouve sous ``C:\\ProgramData\\ParcVue\\``
  (config.ini poussé par GPO, state.json généré après enrôlement, logs).
- En développement : on travaille dans le dossier courant.

Le ``agent_token`` stocké dans state.json est protégé via DPAPI (win32crypt)
en production, avec un repli en clair si DPAPI est indisponible.

Identité machine : ``get_machine_id()`` renvoie le MachineGuid lu dans
``HKLM\\SOFTWARE\\Microsoft\\Cryptography`` (empreinte stable du poste).
"""

from __future__ import annotations

import base64
import configparser
import json
import logging
import os
import platform
import socket
import sys

_logger = logging.getLogger("parcvue.config")

# ----------------------------------------------------------------------------
# Imports Windows tolérants : l'agent ne doit jamais crasher si un module
# Windows manque (ex. exécution partielle hors d'un poste Windows complet).
# ----------------------------------------------------------------------------
try:  # Registre Windows (lecture du MachineGuid).
    import winreg  # type: ignore
except ImportError:  # pragma: no cover - environnement non Windows.
    winreg = None  # type: ignore

try:  # DPAPI pour chiffrer/déchiffrer le token au repos.
    import win32crypt  # type: ignore
except ImportError:  # pragma: no cover - pywin32 absent.
    win32crypt = None  # type: ignore


# Répertoire de données en production.
PROD_DATA_DIR = r"C:\ProgramData\ParcVue"

# Marqueur de chiffrement DPAPI dans state.json (préfixe de la valeur token).
_DPAPI_PREFIX = "dpapi:"


def is_frozen() -> bool:
    """Retourne True si l'agent tourne en exécutable PyInstaller (.exe)."""
    return getattr(sys, "frozen", False)


def get_data_dir() -> str:
    """Détermine le répertoire de données (config/state/logs).

    Priorité :
      1. variable d'environnement ``PARCVUE_DATA_DIR`` (utile pour les tests),
      2. ``C:\\ProgramData\\ParcVue`` si présent / créable (production),
      3. dossier courant (développement).
    """
    override = os.environ.get("PARCVUE_DATA_DIR")
    if override:
        try:
            os.makedirs(override, exist_ok=True)
        except OSError as exc:  # On loggue mais on continue.
            _logger.warning("Impossible de créer PARCVUE_DATA_DIR=%s : %s", override, exc)
        return override

    # En production (Windows), on privilégie ProgramData.
    if os.name == "nt":
        try:
            os.makedirs(PROD_DATA_DIR, exist_ok=True)
            if os.path.isdir(PROD_DATA_DIR):
                return PROD_DATA_DIR
        except OSError as exc:
            # Pas les droits / dossier indisponible → repli dossier courant.
            _logger.warning("Répertoire %s indisponible (%s), repli dossier courant.",
                            PROD_DATA_DIR, exc)

    return os.getcwd()


def get_config_path() -> str:
    """Chemin absolu du fichier config.ini."""
    return os.path.join(get_data_dir(), "config.ini")


def get_state_path() -> str:
    """Chemin absolu du fichier state.json."""
    return os.path.join(get_data_dir(), "state.json")


def get_log_path() -> str:
    """Chemin absolu du fichier de log de l'agent."""
    return os.path.join(get_data_dir(), "parcvue-agent.log")


# ----------------------------------------------------------------------------
# Configuration (config.ini)
# ----------------------------------------------------------------------------
class AgentConfig:
    """Configuration immuable lue depuis config.ini.

    Les intervalles agent peuvent être pilotés à chaud par le serveur (champ
    ``config`` du heartbeat) ; ils restent modifiables via ``apply_server_config``.
    """

    def __init__(
        self,
        server_url: str,
        enrollment_token: str,
        verify_tls: bool,
        heartbeat_interval: int,
        command_poll_interval: int,
        inventory_interval_hours: float,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.enrollment_token = enrollment_token
        self.verify_tls = verify_tls
        self.heartbeat_interval = int(heartbeat_interval)
        self.command_poll_interval = int(command_poll_interval)
        self.inventory_interval_hours = float(inventory_interval_hours)

    def apply_server_config(self, server_config: dict) -> bool:
        """Applique les intervalles renvoyés par le serveur dans le heartbeat.

        Retourne True si au moins une valeur a changé.
        """
        changed = False
        if not isinstance(server_config, dict):
            return False

        hb = server_config.get("heartbeat_interval")
        if isinstance(hb, (int, float)) and hb > 0 and int(hb) != self.heartbeat_interval:
            self.heartbeat_interval = int(hb)
            changed = True

        cp = server_config.get("command_poll_interval")
        if isinstance(cp, (int, float)) and cp > 0 and int(cp) != self.command_poll_interval:
            self.command_poll_interval = int(cp)
            changed = True

        if changed:
            _logger.info(
                "Config serveur appliquée : heartbeat=%ss, poll commandes=%ss",
                self.heartbeat_interval, self.command_poll_interval,
            )
        return changed


def _str_to_bool(value: str, default: bool = True) -> bool:
    """Convertit une chaîne ini en booléen de façon tolérante."""
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "oui", "vrai")


def load_config(path: str | None = None) -> AgentConfig:
    """Charge config.ini ; lève une exception claire si le fichier est absent.

    Le format est défini par le SPEC (section 4.2).
    """
    config_path = path or get_config_path()
    if not os.path.isfile(config_path):
        raise FileNotFoundError(
            f"Fichier de configuration introuvable : {config_path}. "
            "Copier config.example.ini et le renseigner."
        )

    parser = configparser.ConfigParser()
    # Lecture tolérante à l'encodage (UTF-8, avec ou sans BOM).
    with open(config_path, "r", encoding="utf-8-sig") as fh:
        parser.read_file(fh)

    if not parser.has_section("server"):
        raise ValueError("Section [server] manquante dans config.ini.")

    server_url = parser.get("server", "url", fallback="").strip()
    enrollment_token = parser.get("server", "enrollment_token", fallback="").strip()
    verify_tls = _str_to_bool(parser.get("server", "verify_tls", fallback="true"))

    if not server_url:
        raise ValueError("Clé [server] url manquante ou vide dans config.ini.")
    if not enrollment_token:
        raise ValueError("Clé [server] enrollment_token manquante ou vide dans config.ini.")

    heartbeat_interval = parser.getint("agent", "heartbeat_interval", fallback=45)
    command_poll_interval = parser.getint("agent", "command_poll_interval", fallback=8)
    inventory_interval_hours = parser.getfloat("agent", "inventory_interval_hours", fallback=12.0)

    return AgentConfig(
        server_url=server_url,
        enrollment_token=enrollment_token,
        verify_tls=verify_tls,
        heartbeat_interval=heartbeat_interval,
        command_poll_interval=command_poll_interval,
        inventory_interval_hours=inventory_interval_hours,
    )


# ----------------------------------------------------------------------------
# État (state.json) : agent_id + agent_token, token protégé DPAPI
# ----------------------------------------------------------------------------
def _dpapi_encrypt(plaintext: str) -> str | None:
    """Chiffre une chaîne via DPAPI (machine) et renvoie une valeur préfixée.

    Retourne None si DPAPI est indisponible (l'appelant fera un repli en clair).
    """
    if win32crypt is None:
        return None
    try:
        blob = win32crypt.CryptProtectData(
            plaintext.encode("utf-8"),
            "ParcVue agent token",  # description
            None, None, None,
            # 0x4 = CRYPTPROTECT_LOCAL_MACHINE : lié à la machine, pas à l'utilisateur
            # (l'agent tourne en SYSTEM, le contexte utilisateur n'est pas garanti).
            0x4,
        )
        return _DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception as exc:  # noqa: BLE001 - on ne crashe jamais sur le crypto.
        _logger.warning("Chiffrement DPAPI impossible (repli en clair) : %s", exc)
        return None


def _dpapi_decrypt(stored: str) -> str:
    """Déchiffre une valeur stockée. Gère les valeurs en clair (sans préfixe)."""
    if not stored:
        return ""
    if not stored.startswith(_DPAPI_PREFIX):
        # Valeur en clair (repli historique ou DPAPI indisponible à l'écriture).
        return stored
    if win32crypt is None:
        _logger.error("Token chiffré DPAPI mais win32crypt indisponible pour le lire.")
        return ""
    try:
        blob = base64.b64decode(stored[len(_DPAPI_PREFIX):])
        # CryptUnprotectData renvoie un tuple (description, data).
        _desc, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0x4)
        return data.decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        _logger.error("Déchiffrement DPAPI du token impossible : %s", exc)
        return ""


class AgentState:
    """État persistant de l'agent : agent_id + agent_token."""

    def __init__(self, agent_id: str | None = None, agent_token: str | None = None) -> None:
        self.agent_id = agent_id
        self.agent_token = agent_token

    @property
    def is_enrolled(self) -> bool:
        """True si l'agent dispose d'un identifiant et d'un token valides."""
        return bool(self.agent_id) and bool(self.agent_token)


def load_state(path: str | None = None) -> AgentState:
    """Charge state.json ; renvoie un état vide si le fichier est absent/invalide."""
    state_path = path or get_state_path()
    if not os.path.isfile(state_path):
        return AgentState()

    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        _logger.error("Lecture de state.json impossible (%s), état réinitialisé.", exc)
        return AgentState()

    agent_id = raw.get("agent_id")
    stored_token = raw.get("agent_token") or ""
    agent_token = _dpapi_decrypt(stored_token)
    return AgentState(agent_id=agent_id, agent_token=agent_token or None)


def save_state(state: AgentState, path: str | None = None) -> None:
    """Écrit state.json (token protégé DPAPI si possible, repli en clair sinon).

    L'écriture est atomique (fichier temporaire + remplacement).
    """
    state_path = path or get_state_path()
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)

    token_value = state.agent_token or ""
    if token_value:
        encrypted = _dpapi_encrypt(token_value)
        if encrypted is not None:
            token_value = encrypted  # protégé DPAPI
        # sinon : on garde le token en clair (repli explicite)

    payload = {
        "agent_id": state.agent_id,
        "agent_token": token_value,
    }

    tmp_path = state_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, state_path)
    except OSError as exc:
        _logger.error("Écriture de state.json impossible : %s", exc)
        # Nettoyage du fichier temporaire en cas d'échec.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------------
# Identité machine
# ----------------------------------------------------------------------------
def get_machine_id() -> str:
    """Empreinte stable du poste = MachineGuid du registre Windows.

    Source : ``HKLM\\SOFTWARE\\Microsoft\\Cryptography`` valeur ``MachineGuid``.
    Repli : nom d'hôte si le registre est inaccessible (ne crashe jamais).
    """
    if winreg is not None:
        try:
            # KEY_WOW64_64KEY pour lire la vue 64 bits même depuis un process 32 bits.
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                value, _type = winreg.QueryValueEx(key, "MachineGuid")
                if value:
                    return str(value).strip()
        except OSError as exc:
            _logger.warning("Lecture du MachineGuid impossible (%s), repli hostname.", exc)

    # Repli : nom de machine (moins stable, mais évite un identifiant vide).
    return f"hostname:{get_hostname()}"


def get_hostname() -> str:
    """Nom d'hôte du poste."""
    try:
        return socket.gethostname()
    except OSError:
        return platform.node() or "inconnu"


def get_os_version() -> str:
    """Version lisible du système d'exploitation (ex. 'Windows 11 Pro 26100')."""
    try:
        system = platform.system() or "Windows"
        release = platform.release() or ""
        version = platform.version() or ""
        # version Windows ressemble à '10.0.26100' : on garde le numéro de build.
        build = version.split(".")[-1] if version else ""
        edition = ""
        if os.name == "nt":
            # platform.win32_edition() existe depuis Python 3.8 sous Windows.
            try:
                edition = platform.win32_edition() or ""
            except (AttributeError, OSError):
                edition = ""
        parts = [p for p in (system, release, edition, build) if p]
        return " ".join(parts).strip() or "Windows"
    except Exception:  # noqa: BLE001 - jamais bloquant.
        return "Windows"
