"""Invitation d'un compte : la personne choisit elle-même son mot de passe.

Reprend le fonctionnement de l'application SMS. Un administrateur crée un
compte avec un nom et une adresse ; aucun mot de passe n'est saisi ni transmis.
La personne reçoit un lien à USAGE UNIQUE et définit son mot de passe.

Deux garde-fous :

- seule l'EMPREINTE SHA-256 du jeton est stockée. Une copie de la base ne
  permet donc pas de reconstituer les liens encore valides ;
- le jeton a une durée de vie (``INVITE_VALIDITY_DAYS``) et il est consommé dès
  qu'il a servi.

L'échec de l'envoi n'est JAMAIS fatal : le compte existe, et l'administrateur
récupère le lien à l'écran pour le transmettre par le canal de son choix.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from flask import current_app, request

from .extensions import db
from .mailer import MailError, mail_enabled, send_email
from .models import User, utcnow

_logger = logging.getLogger("truesight.invitations")

# Longueur du jeton : 32 octets = 256 bits, illisible par force brute.
_TOKEN_BYTES = 32


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_base_url() -> str:
    """Base publique du dashboard (configurée, sinon déduite de la requête)."""
    configured = (current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    try:
        return request.host_url.rstrip("/")
    except RuntimeError:  # hors contexte de requête (tests, tâche de fond)
        return ""


def invitation_url(token: str) -> str:
    return f"{public_base_url()}/invitation/{token}"


def create_invitation(user: User) -> tuple[str, object]:
    """Pose un jeton neuf sur ce compte ; renvoie (jeton en clair, expiration).

    Le jeton en clair n'existe QUE dans la réponse : il n'est ni journalisé, ni
    stocké. Poser une nouvelle invitation invalide la précédente.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    jours = int(current_app.config.get("INVITE_VALIDITY_DAYS", 7))
    expire = utcnow() + timedelta(days=jours)

    user.invite_token_hash = _hash(token)
    user.invite_expires_at = expire
    user.invited_at = utcnow()
    return token, expire


def find_by_invitation(token: str) -> User | None:
    """Compte correspondant à un jeton ENCORE VALIDE, sinon None."""
    if not token:
        return None
    user = (
        db.session.query(User)
        .filter(User.invite_token_hash == _hash(token), User.is_active.is_(True))
        .first()
    )
    if user is None or user.invite_expires_at is None:
        return None
    expire = user.invite_expires_at
    if expire.tzinfo is None:  # colonne lue sans fuseau (SQLite des tests)
        from datetime import timezone

        expire = expire.replace(tzinfo=timezone.utc)
    if expire <= utcnow():
        return None
    return user


def consume_invitation(user: User) -> None:
    """Le jeton ne doit servir qu'une fois."""
    user.invite_token_hash = None
    user.invite_expires_at = None


def _template(nom: str, lien: str, invite_par: str, expire_le: str) -> tuple[str, str]:
    """Corps de l'e-mail (HTML + texte). Volontairement sobre et court."""
    salutation = f"Bonjour {nom}," if nom else "Bonjour,"
    par = f" par {invite_par}" if invite_par else ""
    html = f"""<!doctype html>
<html lang="fr"><body style="margin:0;padding:24px;background:#f5f6f8;
  font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d2126">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:10px;
    padding:28px;border:1px solid #e3e6ea">
    <p style="margin:0 0 16px;font-size:15px">{salutation}</p>
    <p style="margin:0 0 16px;font-size:15px;line-height:1.5">
      Un accès à <strong>TrueSight</strong>, la supervision du parc informatique,
      vient d'être ouvert pour vous{par}.
    </p>
    <p style="margin:0 0 24px;font-size:15px;line-height:1.5">
      Cliquez sur le bouton ci-dessous pour <strong>choisir votre mot de passe</strong>.
      Ce lien est personnel, ne sert qu'une fois, et expire le {expire_le}.
    </p>
    <p style="margin:0 0 24px;text-align:center">
      <a href="{lien}" style="display:inline-block;background:#1f6feb;color:#fff;
        text-decoration:none;padding:12px 22px;border-radius:8px;font-size:15px">
        Choisir mon mot de passe</a>
    </p>
    <p style="margin:0;font-size:13px;color:#6b7280;line-height:1.5">
      Si le bouton ne fonctionne pas, copiez cette adresse dans votre navigateur :<br>
      <span style="word-break:break-all">{lien}</span>
    </p>
    <p style="margin:20px 0 0;font-size:13px;color:#6b7280;line-height:1.5">
      Vous n'attendiez pas ce message ? Ignorez-le : sans votre mot de passe,
      le compte reste inutilisable.
    </p>
  </div>
</body></html>"""
    text = (
        f"{salutation}\n\n"
        f"Un accès à TrueSight vient d'être ouvert pour vous{par}.\n\n"
        f"Choisissez votre mot de passe ici (lien personnel, à usage unique, "
        f"valable jusqu'au {expire_le}) :\n{lien}\n\n"
        "Vous n'attendiez pas ce message ? Ignorez-le : sans votre mot de passe, "
        "le compte reste inutilisable.\n"
    )
    return html, text


def send_invitation(user: User, invite_par: str = "") -> dict:
    """Pose un jeton et envoie l'e-mail.

    Renvoie ``{"ok": bool, "lien": str, "erreur": str|None, "expire_le": str}``.
    L'appelant DOIT committer : le jeton est posé sur l'objet ``user``.
    """
    token, expire = create_invitation(user)
    lien = invitation_url(token)
    expire_le = expire.strftime("%d/%m/%Y")

    if not mail_enabled():
        return {
            "ok": False,
            "lien": lien,
            "expire_le": expire_le,
            "erreur": "L'envoi d'e-mail n'est pas configuré sur ce serveur.",
        }

    html, text = _template(user.name or "", lien, invite_par, expire_le)
    try:
        send_email(
            to=user.email,
            to_name=user.name or None,
            subject="Votre accès à TrueSight",
            html=html,
            text=text,
        )
    except MailError as exc:
        _logger.warning("Invitation non envoyée à %s : %s", user.email, exc)
        return {"ok": False, "lien": lien, "expire_le": expire_le, "erreur": str(exc)}

    return {"ok": True, "lien": lien, "expire_le": expire_le, "erreur": None}
