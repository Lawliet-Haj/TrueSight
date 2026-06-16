"""Blueprint des pages HTML du dashboard (Jinja).

Gère l'authentification par session avec étape MFA TOTP optionnelle, et rend les
pages : accueil/agents, fiche poste, journal d'audit, login/logout.
"""
import uuid

import pyotp
from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .extensions import db
from .models import Agent, User
from .security import (
    admin_required,
    current_user,
    login_required,
    verify_password,
    write_audit,
)

bp = Blueprint("web", __name__)


# --------------------------------------------------------------------------
# Accueil
# --------------------------------------------------------------------------
@bp.get("/")
def index():
    """Redirige vers la liste des agents (ou la page de connexion)."""
    if current_user() is None:
        return redirect(url_for("web.login"))
    return redirect(url_for("web.agents_page"))


# --------------------------------------------------------------------------
# Connexion (mot de passe + étape MFA)
# --------------------------------------------------------------------------
@bp.get("/login")
def login():
    """Affiche le formulaire de connexion."""
    if current_user() is not None:
        return redirect(url_for("web.agents_page"))
    return render_template("login.html", next=request.args.get("next", ""))


@bp.post("/login")
def login_post():
    """Vérifie l'email + mot de passe ; lance l'étape MFA si activée."""
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    next_url = request.form.get("next", "")

    user = db.session.query(User).filter(
        db.func.lower(User.email) == email
    ).one_or_none()

    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        write_audit(
            action="login.fail",
            user_id=user.id if user else None,
            details={"email": email, "reason": "identifiants invalides"},
        )
        flash("Identifiants invalides.", "error")
        return render_template("login.html", next=next_url), 401

    if user.mfa_enabled and user.mfa_secret:
        # Étape MFA : on mémorise l'utilisateur en attente de validation TOTP.
        session.clear()
        session["pending_mfa_user_id"] = str(user.id)
        session["pending_next"] = next_url
        return redirect(url_for("web.mfa"))

    # Connexion directe (pas de MFA).
    _establish_session(user)
    write_audit(action="login.success", user_id=user.id, details={"mfa": False})
    return _redirect_after_login(next_url)


@bp.get("/mfa")
def mfa():
    """Affiche le formulaire de saisie du code TOTP."""
    if not session.get("pending_mfa_user_id"):
        return redirect(url_for("web.login"))
    return render_template("mfa.html")


@bp.post("/mfa")
def mfa_post():
    """Vérifie le code TOTP et finalise la connexion."""
    pending_id = session.get("pending_mfa_user_id")
    if not pending_id:
        return redirect(url_for("web.login"))

    try:
        uid = uuid.UUID(str(pending_id))
    except (ValueError, TypeError):
        session.clear()
        return redirect(url_for("web.login"))

    user = db.session.get(User, uid)
    code = (request.form.get("code") or "").strip().replace(" ", "")
    next_url = session.get("pending_next", "")

    if user is None or not user.is_active or not user.mfa_secret:
        session.clear()
        flash("Session MFA invalide.", "error")
        return redirect(url_for("web.login"))

    totp = pyotp.TOTP(user.mfa_secret)
    if not totp.verify(code, valid_window=1):
        write_audit(
            action="login.fail",
            user_id=user.id,
            details={"reason": "code MFA invalide"},
        )
        flash("Code MFA invalide.", "error")
        return render_template("mfa.html"), 401

    _establish_session(user)
    write_audit(action="login.success", user_id=user.id, details={"mfa": True})
    return _redirect_after_login(next_url)


@bp.get("/logout")
def logout():
    """Déconnecte l'utilisateur et vide la session."""
    user = current_user()
    if user is not None:
        write_audit(action="logout", user_id=user.id, details={})
    session.clear()
    flash("Vous êtes déconnecté.", "info")
    return redirect(url_for("web.login"))


def _establish_session(user: User):
    """Ouvre une session authentifiée pour l'utilisateur donné."""
    session.clear()
    session["user_id"] = str(user.id)
    session["role"] = user.role
    session["email"] = user.email
    session.permanent = True


def _redirect_after_login(next_url: str):
    """Redirige vers la cible demandée (si interne) ou vers la liste des agents."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("web.agents_page"))


# --------------------------------------------------------------------------
# Pages protégées
# --------------------------------------------------------------------------
@bp.get("/agents")
@login_required
def agents_page():
    """Tableau du parc (rafraîchi côté client via l'API JSON)."""
    return render_template("agents.html", user=g.user)


@bp.get("/agents/<agent_id>")
@login_required
def agent_detail_page(agent_id):
    """Fiche d'un poste : inventaire, graphiques, console de commande."""
    try:
        aid = uuid.UUID(str(agent_id))
    except (ValueError, TypeError):
        return render_template("agent_detail.html", user=g.user, agent_id=None, not_found=True), 404

    agent = db.session.get(Agent, aid)
    if agent is None:
        return render_template("agent_detail.html", user=g.user, agent_id=None, not_found=True), 404

    return render_template(
        "agent_detail.html",
        user=g.user,
        agent_id=str(agent.id),
        agent_hostname=agent.hostname or str(agent.id),
        is_admin=(g.user.role == "admin"),
        not_found=False,
    )


@bp.get("/audit")
@admin_required
def audit_page():
    """Journal d'audit (réservé aux administrateurs)."""
    return render_template("audit.html", user=g.user)


@bp.get("/alerts")
@login_required
def alerts_page():
    """Liste des alertes du parc (rafraîchie côté client via l'API JSON)."""
    return render_template("alerts.html", user=g.user)


@bp.get("/inventory")
@login_required
def inventory_page():
    """Inventaire logiciel agrégé du parc (rafraîchi via l'API JSON)."""
    return render_template("inventory.html", user=g.user)


@bp.get("/settings")
@login_required
def settings_page():
    """Réglages de l'utilisateur courant (mot de passe, MFA)."""
    return render_template("settings.html", user=g.user)
