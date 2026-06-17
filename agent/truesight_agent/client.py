"""Client HTTP de l'agent TrueSight (couche réseau).

Toutes les requêtes sortent en HTTPS vers le serveur. Le client :
- réutilise une session ``requests`` (keep-alive),
- ajoute l'en-tête ``Authorization: Bearer <agent_token>`` une fois enrôlé,
- applique des timeouts stricts,
- effectue un retry/backoff exponentiel sur les erreurs réseau et 5xx,
- ne lève jamais d'exception « surprise » : il renvoie des objets ``ApiResult``.

Conforme aux payloads du SPEC section 2.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any

import requests

from . import __version__

_logger = logging.getLogger("truesight.client")

# Base de tous les endpoints (SPEC section 0).
API_BASE = "/api/v1"

# Statuts HTTP que l'on retente (en plus des erreurs réseau).
_RETRYABLE_STATUS = {500, 502, 503, 504}

# Statuts HTTP qui indiquent une révocation / un token invalide.
_AUTH_ERROR_STATUS = {401, 403}


class AuthError(Exception):
    """Levée quand le serveur renvoie 401/403 (token révoqué ou invalide)."""


class ApiResult:
    """Résultat normalisé d'un appel API.

    Attributs :
      - ``ok`` : True si la requête a abouti avec un statut < 400,
      - ``status_code`` : code HTTP (None si jamais de réponse reçue),
      - ``data`` : corps JSON décodé (dict/list) ou None,
      - ``error`` : message d'erreur lisible (None si succès).
    """

    def __init__(
        self,
        ok: bool,
        status_code: int | None = None,
        data: Any = None,
        error: str | None = None,
    ) -> None:
        self.ok = ok
        self.status_code = status_code
        self.data = data
        self.error = error

    def __repr__(self) -> str:  # pragma: no cover - aide au débogage.
        return f"<ApiResult ok={self.ok} status={self.status_code} error={self.error!r}>"


class ApiClient:
    """Client HTTP réutilisable vers le serveur TrueSight."""

    # Timeouts (connexion, lecture) en secondes.
    DEFAULT_TIMEOUT = (10, 30)
    # Paramètres du backoff exponentiel.
    MAX_RETRIES = 5
    BACKOFF_BASE = 1.5
    BACKOFF_CAP = 60.0

    def __init__(
        self,
        base_url: str,
        verify_tls: bool = True,
        timeout: tuple[int, int] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify_tls = verify_tls
        self.timeout = timeout or self.DEFAULT_TIMEOUT

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": f"TrueSight-Agent/{__version__}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        # Identité de l'agent (renseignée après enrôlement).
        self._agent_id: str | None = None
        self._agent_token: str | None = None
        self._lock = threading.Lock()

        if not self.verify_tls:
            # Mode développement : on évite le bruit des avertissements urllib3.
            _logger.warning("Vérification TLS désactivée (verify_tls=false) - DEV uniquement.")
            try:
                from urllib3.exceptions import InsecureRequestWarning
                requests.packages.urllib3.disable_warnings(InsecureRequestWarning)  # type: ignore
            except Exception:  # noqa: BLE001
                pass

    # -- Gestion de l'identité ------------------------------------------------
    def set_credentials(self, agent_id: str, agent_token: str) -> None:
        """Enregistre l'identité de l'agent pour les appels authentifiés."""
        with self._lock:
            self._agent_id = agent_id
            self._agent_token = agent_token
            self._session.headers["Authorization"] = f"Bearer {agent_token}"

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    @property
    def is_authenticated(self) -> bool:
        return bool(self._agent_id) and bool(self._agent_token)

    # -- Requête générique avec retry/backoff ---------------------------------
    def _request(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        params: dict | None = None,
        authed: bool = True,
        max_retries: int | None = None,
    ) -> ApiResult:
        """Exécute une requête HTTP avec retry/backoff.

        - ``authed`` : True si l'appel doit porter le Bearer (tous sauf /enroll).
        - Retente sur erreurs réseau et statuts 5xx (jusqu'à ``max_retries``).
        - Ne retente PAS sur 4xx (sauf 429 : on respecte un backoff).
        """
        url = self.base_url + API_BASE + path
        retries = self.MAX_RETRIES if max_retries is None else max_retries

        if authed and not self.is_authenticated:
            return ApiResult(False, error="Appel authentifié sans identité agent.")

        last_error = "échec inconnu"
        attempt = 0
        while attempt <= retries:
            try:
                response = self._session.request(
                    method=method,
                    url=url,
                    json=json_body,
                    params=params,
                    timeout=self.timeout,
                    verify=self.verify_tls,
                )
            except requests.exceptions.RequestException as exc:
                # Erreur réseau (DNS, connexion refusée, timeout, TLS...) → retry.
                last_error = f"erreur réseau : {exc}"
                _logger.warning(
                    "%s %s : %s (tentative %d/%d)",
                    method, path, last_error, attempt + 1, retries + 1,
                )
                if attempt >= retries:
                    return ApiResult(False, error=last_error)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            status = response.status_code

            # Authentification : remontée immédiate (le runner gérera le ré-enrôlement).
            if status in _AUTH_ERROR_STATUS:
                body = self._safe_json(response)
                msg = f"authentification refusée (HTTP {status})"
                _logger.error("%s %s : %s", method, path, msg)
                raise AuthError(msg if not isinstance(body, dict) else body.get("error", msg))

            # 429 : rate-limit → on respecte un backoff et on retente.
            if status == 429:
                last_error = "rate-limit (HTTP 429)"
                if attempt >= retries:
                    return ApiResult(False, status_code=status, error=last_error)
                retry_after = self._parse_retry_after(response)
                _logger.warning("%s %s : %s, pause %.1fs", method, path, last_error, retry_after)
                time.sleep(retry_after)
                attempt += 1
                continue

            # 5xx : erreur serveur transitoire → retry.
            if status in _RETRYABLE_STATUS:
                last_error = f"erreur serveur (HTTP {status})"
                _logger.warning(
                    "%s %s : %s (tentative %d/%d)",
                    method, path, last_error, attempt + 1, retries + 1,
                )
                if attempt >= retries:
                    return ApiResult(False, status_code=status, error=last_error)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            # Autres 4xx : erreur définitive, pas de retry.
            if status >= 400:
                body = self._safe_json(response)
                err = f"HTTP {status}"
                if isinstance(body, dict) and body.get("error"):
                    err = f"{err} : {body['error']}"
                _logger.error("%s %s a échoué : %s", method, path, err)
                return ApiResult(False, status_code=status, data=body, error=err)

            # Succès (2xx).
            return ApiResult(True, status_code=status, data=self._safe_json(response))

        return ApiResult(False, error=last_error)

    def _sleep_backoff(self, attempt: int) -> None:
        """Pause exponentielle avec jitter pour lisser les reconnexions."""
        delay = min(self.BACKOFF_CAP, self.BACKOFF_BASE ** attempt)
        delay += random.uniform(0, delay * 0.25)  # jitter +/- 25 %
        time.sleep(delay)

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> float:
        """Lit l'en-tête Retry-After (secondes) ; valeur par défaut raisonnable."""
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return max(1.0, float(raw))
            except ValueError:
                pass
        return 5.0

    @staticmethod
    def _safe_json(response: requests.Response) -> Any:
        """Décode le JSON sans jamais lever d'exception."""
        try:
            return response.json()
        except ValueError:
            return None

    # -- Endpoints agent (SPEC section 2) -------------------------------------
    def enroll(
        self,
        enrollment_token: str,
        machine_id: str,
        hostname: str,
        os_version: str,
        agent_version: str,
    ) -> ApiResult:
        """POST /api/v1/enroll (aucune auth) → renvoie agent_id + agent_token."""
        body = {
            "enrollment_token": enrollment_token,
            "machine_id": machine_id,
            "hostname": hostname,
            "os_version": os_version,
            "agent_version": agent_version,
        }
        # /enroll : pas de Bearer ; 401 = token d'enrôlement invalide, on remonte
        # un ApiResult plutôt qu'une AuthError (le flux enroll a sa propre logique).
        url = self.base_url + API_BASE + "/enroll"
        retries = self.MAX_RETRIES
        attempt = 0
        last_error = "échec inconnu"
        while attempt <= retries:
            try:
                response = self._session.post(
                    url,
                    json=body,
                    timeout=self.timeout,
                    verify=self.verify_tls,
                    # Pas d'en-tête Authorization pour l'enrôlement.
                    headers={"Authorization": ""} if "Authorization" in self._session.headers else None,
                )
            except requests.exceptions.RequestException as exc:
                last_error = f"erreur réseau : {exc}"
                _logger.warning("enroll : %s (tentative %d/%d)", last_error, attempt + 1, retries + 1)
                if attempt >= retries:
                    return ApiResult(False, error=last_error)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            status = response.status_code
            if status == 429:
                if attempt >= retries:
                    return ApiResult(False, status_code=status, error="rate-limit (HTTP 429)")
                time.sleep(self._parse_retry_after(response))
                attempt += 1
                continue
            if status in _RETRYABLE_STATUS:
                if attempt >= retries:
                    return ApiResult(False, status_code=status, error=f"erreur serveur (HTTP {status})")
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            if status >= 400:
                data = self._safe_json(response)
                err = f"HTTP {status}"
                if isinstance(data, dict) and data.get("error"):
                    err = f"{err} : {data['error']}"
                return ApiResult(False, status_code=status, data=data, error=err)
            return ApiResult(True, status_code=status, data=self._safe_json(response))

        return ApiResult(False, error=last_error)

    def heartbeat(self, metrics: dict, meta: dict | None = None) -> ApiResult:
        """POST /api/v1/agents/{agent_id}/heartbeat → {ok, pending_commands, config}.

        ``meta`` (optionnel) transporte les métadonnées du poste (os_version,
        agent_version, hostname) pour que le serveur les rafraîchisse sans
        ré-enrôlement (ex. après une mise à niveau Windows 10 → 11).
        """
        if not self._agent_id:
            return ApiResult(False, error="heartbeat sans agent_id.")
        body = {"metrics": metrics}
        if meta:
            body.update(meta)
        return self._request(
            "POST",
            f"/agents/{self._agent_id}/heartbeat",
            json_body=body,
        )

    def send_inventory(self, hardware: dict, software: list) -> ApiResult:
        """POST /api/v1/agents/{agent_id}/inventory → {ok}."""
        if not self._agent_id:
            return ApiResult(False, error="inventaire sans agent_id.")
        return self._request(
            "POST",
            f"/agents/{self._agent_id}/inventory",
            json_body={"hardware": hardware, "software": software},
        )

    def get_commands(self) -> ApiResult:
        """GET /api/v1/agents/{agent_id}/commands → {commands:[...]}."""
        if not self._agent_id:
            return ApiResult(False, error="poll commandes sans agent_id.")
        return self._request(
            "GET",
            f"/agents/{self._agent_id}/commands",
        )

    def post_result(self, command_id: str, result: dict) -> ApiResult:
        """POST /api/v1/commands/{command_id}/result → {ok}."""
        return self._request(
            "POST",
            f"/commands/{command_id}/result",
            json_body=result,
        )

    # -- Bureau à distance (WebSocket) ----------------------------------------
    @staticmethod
    def derive_ws_base(server_url: str) -> str:
        """Déduit la base WebSocket depuis l'URL HTTP du serveur.

        ``https://host`` → ``wss://host`` ; ``http://host`` → ``ws://host``.
        Le ``ws_url`` complet de la session vient normalement du serveur (champ
        ``remote_session.ws_url``) ; ce helper fournit un **repli** pour
        reconstruire l'URL côté agent si besoin.
        """
        url = (server_url or "").strip().rstrip("/")
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        # Pas de schéma explicite : on suppose wss (prod TLS par défaut).
        return "wss://" + url

    def ws_base(self) -> str:
        """Base WebSocket de ce client (déduite de ``base_url``)."""
        return self.derive_ws_base(self.base_url)

    def remote_agent_ws_url(self, token: str) -> str:
        """Repli : construit l'URL wss agent du relais à partir du token.

        Utilisé uniquement si le serveur n'a pas fourni de ``ws_url`` complet.
        """
        return f"{self.ws_base()}/ws/remote/agent?token={token}"

    # -- Téléchargement de fichier (auto-update) ------------------------------
    def download_file(self, url: str, dest_path: str, chunk_size: int = 256 * 1024) -> ApiResult:
        """Télécharge un fichier (streaming) vers ``dest_path``. URL ABSOLUE.

        Réutilise la session authentifiée (Bearer + verify_tls). Écrit dans un
        fichier ``.part`` puis le renomme atomiquement. Ne retente pas (le runner
        relancera au prochain cycle si besoin) ; lève ``AuthError`` sur 401/403.
        """
        tmp_path = dest_path + ".part"
        try:
            with self._session.get(
                url, stream=True, timeout=(10, 300), verify=self.verify_tls
            ) as resp:
                if resp.status_code in _AUTH_ERROR_STATUS:
                    raise AuthError(f"téléchargement refusé (HTTP {resp.status_code})")
                if resp.status_code >= 400:
                    return ApiResult(False, status_code=resp.status_code,
                                     error=f"HTTP {resp.status_code}")
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                with open(tmp_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if chunk:
                            fh.write(chunk)
                os.replace(tmp_path, dest_path)
                return ApiResult(True, status_code=resp.status_code)
        except requests.exceptions.RequestException as exc:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return ApiResult(False, error=f"erreur réseau : {exc}")

    def close(self) -> None:
        """Ferme la session HTTP (libère les connexions)."""
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass
