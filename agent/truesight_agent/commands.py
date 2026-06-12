"""Exécution des commandes à distance (PowerShell / cmd).

Modèle « one-shot avec timeout » (pas de shell interactif persistant en V1) :
- PowerShell : ``powershell -NoProfile -NonInteractive -Command <cmd>``
- cmd        : ``cmd /c <cmd>``

Capture stdout/stderr (décodage tolérant ``errors="replace"``), code de
sortie et durée. Gère ``TimeoutExpired`` (statut 'timeout'). Ne lève jamais.

Renvoie un dict conforme au SPEC 2.5 :
  {"exit_code": int|None, "stdout": str, "stderr": str, "duration_seconds": float}
auquel on ajoute "status" (done|error|timeout) pour piloter le serveur.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import time

_logger = logging.getLogger("truesight.commands")

# Timeout par défaut si le serveur n'en fournit pas (cf. SPEC : 120 s).
DEFAULT_TIMEOUT_SECONDS = 120

# Limite de troncature locale (le serveur tronque aussi à 1 Mo ; on protège
# la mémoire / le réseau de l'agent en cas de sortie gigantesque).
_MAX_OUTPUT_BYTES = 1_000_000  # 1 Mo

# Drapeau Windows pour ne pas faire apparaître de fenêtre console.
try:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover - hors Windows.
    _NO_WINDOW = 0


def execute(shell: str, command_text: str, timeout_seconds: int | None = None) -> dict:
    """Exécute une commande et renvoie le résultat (SPEC 2.5 + champ 'status').

    Paramètres :
      - ``shell`` : 'powershell' ou 'cmd' (toute autre valeur → 'cmd' par défaut).
      - ``command_text`` : la commande à exécuter.
      - ``timeout_seconds`` : délai max ; défaut 120 s.
    """
    timeout = int(timeout_seconds) if timeout_seconds and int(timeout_seconds) > 0 else DEFAULT_TIMEOUT_SECONDS
    shell_normalized = (shell or "").strip().lower()
    argv = _build_argv(shell_normalized, command_text or "")

    _logger.info("Exécution commande (%s, timeout=%ss) : %s",
                shell_normalized or "cmd", timeout, _summarize(command_text))

    start = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
            shell=False,  # liste d'arguments explicite (pas d'interprétation shell parente)
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - start, 2)
        stdout = _decode(exc.stdout)
        stderr = _decode(exc.stderr)
        if not stderr:
            stderr = f"La commande a dépassé le délai imparti de {timeout} secondes."
        _logger.warning("Commande expirée après %ss.", timeout)
        return {
            "status": "timeout",
            "exit_code": None,
            "stdout": _truncate(stdout),
            "stderr": _truncate(stderr),
            "duration_seconds": duration,
        }
    except FileNotFoundError as exc:
        # Interpréteur introuvable (PATH cassé) : erreur propre, pas de crash.
        duration = round(time.monotonic() - start, 2)
        _logger.error("Interpréteur introuvable : %s", exc)
        return {
            "status": "error",
            "exit_code": None,
            "stdout": "",
            "stderr": f"Interpréteur introuvable : {exc}",
            "duration_seconds": duration,
        }
    except Exception as exc:  # noqa: BLE001 - filet de sécurité global.
        duration = round(time.monotonic() - start, 2)
        _logger.error("Échec d'exécution de la commande : %s", exc)
        return {
            "status": "error",
            "exit_code": None,
            "stdout": "",
            "stderr": f"Erreur d'exécution : {exc}",
            "duration_seconds": duration,
        }

    duration = round(time.monotonic() - start, 2)
    exit_code = completed.returncode
    stdout = _decode(completed.stdout)
    stderr = _decode(completed.stderr)
    status = "done" if exit_code == 0 else "error"

    _logger.info("Commande terminée : exit_code=%s, durée=%.2fs, status=%s",
                exit_code, duration, status)

    return {
        "status": status,
        "exit_code": exit_code,
        "stdout": _truncate(stdout),
        "stderr": _truncate(stderr),
        "duration_seconds": duration,
    }


def _build_argv(shell_normalized: str, command_text: str) -> list[str]:
    """Construit la liste d'arguments selon l'interpréteur demandé."""
    if shell_normalized == "powershell":
        # -EncodedCommand : la commande est passée en base64 (UTF-16LE), ce qui
        # neutralise tout problème de guillemets / pipes / opérateurs dans les
        # commandes complexes émises depuis le dashboard. Bien plus robuste que
        # -Command "<texte>" soumis au requoting de Windows.
        # On force la sortie en UTF-8 pour que les accents reviennent propres
        # (sinon PowerShell écrit dans la codepage console et casse les non-ASCII).
        script = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + (command_text or "")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ]
    # 'cmd' par défaut (et pour toute valeur non reconnue). cmd.exe reçoit la
    # commande comme un seul argument et interprète lui-même &&, |, > etc.
    return ["cmd", "/c", command_text]


def _decode(data) -> str:
    """Décode des octets en texte avec tolérance (errors='replace')."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        # Repli ANSI Windows si l'UTF-8 échoue malgré errors='replace'.
        try:
            return data.decode("cp1252", errors="replace")
        except Exception:  # noqa: BLE001
            return ""


def _truncate(text: str) -> str:
    """Tronque la sortie à 1 Mo (sécurité réseau/mémoire)."""
    if not text:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_OUTPUT_BYTES:
        return text
    truncated = encoded[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return truncated + "\n[...sortie tronquée à 1 Mo...]"


def _summarize(command_text: str | None, limit: int = 120) -> str:
    """Résumé d'une commande pour les logs (sans inonder le fichier de log)."""
    text = (command_text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text
