"""Client minimal pour une API compatible OpenAI (Chat Completions), via ``requests``.

On évite volontairement le SDK ``openai`` : il tirerait ``httpx``/``pydantic`` dans
l'arbre de dépendances, ce qui risquerait de bousculer ``cryptography`` (contrainte
projet : un bump casse pyOpenSSL → la base ne démarre plus). ``requests`` est déjà
présent dans ``requirements.txt``.

Ce module est le **seul point fournisseur** : il renvoie une forme NEUTRE
(``ChatResult``) pour que la boucle (``loop.py``) ne dépende pas du format brut.
Pour basculer vers un Ollama local (API compatible OpenAI), il suffit de changer
``OPENAI_BASE_URL`` / ``OPENAI_MODEL`` en configuration — aucun autre changement.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import requests
from flask import current_app

_logger = logging.getLogger("truesight.ai.client")

# Connexion courte, lecture longue (un tour de modèle peut prendre du temps), mais
# bornée pour ne jamais immobiliser un thread gunicorn jusqu'au --timeout 300.
_TIMEOUT = (10, 120)


class AIConfigError(RuntimeError):
    """L'IA n'est pas configurée (clé API absente)."""


class AIClientError(RuntimeError):
    """Erreur d'appel au fournisseur (réseau, HTTP, JSON illisible)."""


@dataclass
class ToolCall:
    """Un appel d'outil demandé par le modèle. ``arguments`` est du JSON brut (str)."""

    id: str
    name: str
    arguments: str


@dataclass
class ChatResult:
    """Réponse normalisée du fournisseur."""

    text: str
    tool_calls: list = field(default_factory=list)  # list[ToolCall]
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
    # Message brut de l'assistant, à ré-injecter tel quel dans l'historique
    # (format OpenAI : role=assistant + éventuel tool_calls).
    raw_message: dict = field(default_factory=dict)


def is_configured() -> bool:
    """True si une clé API est présente (donc Copilote activable)."""
    return bool((current_app.config.get("OPENAI_API_KEY") or "").strip())


def create_chat(messages, tools=None, *, max_tokens=None, tool_choice="auto") -> ChatResult:
    """Appelle ``POST {base}/chat/completions`` et renvoie un ``ChatResult``.

    Lève ``AIConfigError`` si la clé est absente, ``AIClientError`` sur erreur
    réseau / HTTP / JSON. Ne journalise jamais la clé.
    """
    cfg = current_app.config
    api_key = (cfg.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise AIConfigError("OPENAI_API_KEY non configurée")

    base = (cfg.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    # Les modèles récents OpenAI (GPT-5 / o-series) REFUSENT `max_tokens` (400) et
    # exigent `max_completion_tokens`. C'est donc le défaut. Pour un serveur
    # compatible OpenAI plus ancien / Ollama qui n'accepterait que `max_tokens`,
    # poser OPENAI_MAX_TOKENS_PARAM=max_tokens.
    token_param = (cfg.get("OPENAI_MAX_TOKENS_PARAM") or "max_completion_tokens").strip()
    body = {
        "model": cfg.get("OPENAI_MODEL") or "gpt-4o",
        "messages": messages,
        token_param: int(max_tokens or cfg.get("AI_MAX_TOKENS") or 3000),
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        resp = requests.post(base + "/chat/completions", json=body, headers=headers, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise AIClientError(f"appel IA impossible : {exc}") from exc

    if resp.status_code >= 400:
        snippet = (resp.text or "")[:300]
        _logger.warning("API IA a renvoyé %s : %s", resp.status_code, snippet)
        # On remonte un extrait du corps : utile au diagnostic (mauvais modèle,
        # paramètre non supporté…). Endpoint réservé aux admins.
        raise AIClientError(f"HTTP {resp.status_code} — {snippet[:200]}")

    try:
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = [
            ToolCall(
                id=tc.get("id") or "",
                name=(tc.get("function") or {}).get("name") or "",
                arguments=(tc.get("function") or {}).get("arguments") or "{}",
            )
            for tc in (msg.get("tool_calls") or [])
            if tc.get("type", "function") == "function"
        ]
        return ChatResult(
            text=msg.get("content") or "",
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason") or "stop",
            usage=data.get("usage") or {},
            raw_message=msg,
        )
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise AIClientError(f"réponse IA illisible : {exc}") from exc
