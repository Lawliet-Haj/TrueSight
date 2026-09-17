"""Envoi d'e-mail transactionnel via Brevo.

Même principe que l'application SMS : une seule clé API Brevo, un expéditeur
d'identification visuelle, et les réponses redirigées vers ``MAIL_REPLY_TO``
(l'adresse d'expédition n'est pas une boîte relevée).

Le module est volontairement TOLÉRANT : si l'envoi n'est pas configuré, il le
dit au lieu d'échouer silencieusement. L'appelant (invitation) sait alors
afficher le lien à l'administrateur, qui le transmet autrement. Un e-mail qui ne
part pas ne doit jamais empêcher la création d'un compte.
"""
from __future__ import annotations

import logging

import requests
from flask import current_app

_logger = logging.getLogger("truesight.mailer")

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"
_TIMEOUT_SECONDS = 15


class MailError(Exception):
    """Envoi impossible : message lisible par un humain dans ``str(exc)``."""


def mail_enabled() -> bool:
    """L'envoi est-il configuré (clé API + expéditeur) ?"""
    cfg = current_app.config
    return bool(cfg.get("BREVO_API_KEY") and cfg.get("MAIL_SENDER_EMAIL"))


def mail_status() -> dict:
    """État de la configuration, pour l'afficher dans les réglages."""
    cfg = current_app.config
    return {
        "enabled": mail_enabled(),
        "sender": cfg.get("MAIL_SENDER_EMAIL") or None,
        "reply_to": cfg.get("MAIL_REPLY_TO") or None,
        "api_key_set": bool(cfg.get("BREVO_API_KEY")),
    }


def send_email(to: str, subject: str, html: str, text: str, to_name: str | None = None) -> None:
    """Envoie un e-mail. Lève ``MailError`` avec un motif lisible en cas d'échec."""
    cfg = current_app.config
    if not mail_enabled():
        raise MailError(
            "L'envoi d'e-mail n'est pas configuré sur ce serveur "
            "(BREVO_API_KEY et MAIL_SENDER_EMAIL)."
        )

    sender = {"email": cfg["MAIL_SENDER_EMAIL"]}
    if cfg.get("MAIL_SENDER_NAME"):
        sender["name"] = cfg["MAIL_SENDER_NAME"]

    destinataire = {"email": to}
    if to_name:
        destinataire["name"] = to_name

    body = {
        "sender": sender,
        "to": [destinataire],
        "subject": subject,
        "htmlContent": html,
        "textContent": text,
    }
    if cfg.get("MAIL_REPLY_TO"):
        body["replyTo"] = {"email": cfg["MAIL_REPLY_TO"]}
        if cfg.get("MAIL_REPLY_TO_NAME"):
            body["replyTo"]["name"] = cfg["MAIL_REPLY_TO_NAME"]

    try:
        resp = requests.post(
            _BREVO_URL,
            json=body,
            headers={"api-key": cfg["BREVO_API_KEY"], "accept": "application/json"},
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise MailError(f"Brevo injoignable : {exc}") from exc

    if resp.status_code >= 400:
        # Le message de Brevo est utile (expéditeur non validé, clé révoquée…) :
        # on le remonte tel quel plutôt qu'un « erreur 400 » opaque.
        motif = ""
        try:
            data = resp.json()
            motif = data.get("message") or data.get("code") or ""
        except ValueError:
            motif = (resp.text or "")[:200]
        raise MailError(f"Brevo a refusé l'envoi ({resp.status_code}) : {motif}")

    _logger.info("E-mail envoyé à %s (sujet : %s).", to, subject)
