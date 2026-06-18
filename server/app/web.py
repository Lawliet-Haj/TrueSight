"""Blueprint des pages HTML du dashboard (Jinja).

Gère l'authentification par session avec étape MFA TOTP optionnelle, et rend les
pages : accueil/agents, fiche poste, journal d'audit, login/logout.
"""
import time
import uuid
from collections import defaultdict
from threading import Lock

import pyotp
from flask import (
    Blueprint,
    current_app,
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
# Anti-bruteforce du dashboard (login + MFA) — limiteur mémoire par IP
# --------------------------------------------------------------------------
# Toute étape d'authentification ÉCHOUÉE (mot de passe OU code MFA) incrémente le
# compteur de l'IP sur une fenêtre glissante ; au-delà du quota, on renvoie 429
# le temps que la fenêtre se vide. Une connexion PLEINEMENT réussie remet à zéro.
# Même principe que le rate-limit de /enroll (api_agent). En mémoire de worker :
# suffisant pour freiner une attaque par dictionnaire ; un Redis serait nécessaire
# seulement pour un quota strict multi-worker (hors périmètre actuel).
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_FAILURES = 10
_login_failures: dict[str, list[float]] = defaultdict(list)
_login_lock = Lock()


def _client_ip() -> str:
    """IP cliente réelle (X-Forwarded-For via ProxyFix), ou '?' hors contexte."""
    return (request.remote_addr or "?") if request else "?"


def _login_blocked(ip: str) -> bool:
    """True si l'IP a dépassé le quota d'échecs d'authentification sur la fenêtre."""
    now = time.monotonic()
    with _login_lock:
        hits = _login_failures[ip]
        cutoff = now - _LOGIN_WINDOW_SECONDS
        hits[:] = [t for t in hits if t > cutoff]
        return len(hits) >= _LOGIN_MAX_FAILURES


def _record_login_failure(ip: str) -> None:
    """Enregistre un échec d'authentification pour l'IP."""
    with _login_lock:
        _login_failures[ip].append(time.monotonic())


def _clear_login_failures(ip: str) -> None:
    """Réinitialise le compteur d'échecs de l'IP (connexion réussie)."""
    with _login_lock:
        _login_failures.pop(ip, None)


# --------------------------------------------------------------------------
# Accueil
# --------------------------------------------------------------------------
@bp.get("/")
def index():
    """Redirige vers la vue d'ensemble (ou la page de connexion)."""
    if current_user() is None:
        return redirect(url_for("web.login"))
    return redirect(url_for("web.overview_page"))


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
    ip = _client_ip()

    # Anti-bruteforce : on bloque avant toute vérification coûteuse.
    if _login_blocked(ip):
        write_audit(action="login.blocked", details={"email": email})
        flash("Trop de tentatives échouées. Réessayez dans quelques minutes.", "error")
        return render_template("login.html", next=next_url), 429

    user = db.session.query(User).filter(
        db.func.lower(User.email) == email
    ).one_or_none()

    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        _record_login_failure(ip)
        write_audit(
            action="login.fail",
            user_id=user.id if user else None,
            details={"email": email, "reason": "identifiants invalides"},
        )
        flash("Identifiants invalides.", "error")
        return render_template("login.html", next=next_url), 401

    if user.mfa_enabled and user.mfa_secret:
        # Étape MFA : on mémorise l'utilisateur en attente de validation TOTP.
        # (Le compteur d'échecs n'est PAS réinitialisé tant que le MFA n'est pas
        # validé — un mot de passe correct seul ne lève pas la limitation.)
        session.clear()
        session["pending_mfa_user_id"] = str(user.id)
        session["pending_next"] = next_url
        return redirect(url_for("web.mfa"))

    # Connexion directe (pas de MFA) : succès complet → reset du compteur.
    _clear_login_failures(ip)
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

    ip = _client_ip()
    if _login_blocked(ip):
        write_audit(action="login.blocked", details={"step": "mfa"})
        flash("Trop de tentatives échouées. Réessayez dans quelques minutes.", "error")
        return render_template("mfa.html"), 429

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
        _record_login_failure(ip)
        write_audit(
            action="login.fail",
            user_id=user.id,
            details={"reason": "code MFA invalide"},
        )
        flash("Code MFA invalide.", "error")
        return render_template("mfa.html"), 401

    _clear_login_failures(ip)
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
@bp.get("/overview")
@login_required
def overview_page():
    """Vue d'ensemble : KPI de santé du parc + répartition par emplacement."""
    return render_template("overview.html", user=g.user)


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

    is_admin = g.user.role in ("admin", "superadmin")
    return render_template(
        "agent_detail.html",
        user=g.user,
        agent_id=str(agent.id),
        agent_hostname=agent.hostname or str(agent.id),
        is_admin=is_admin,
        # Copilote IA : visible seulement pour un admin ET si une clé est configurée.
        ai_enabled=is_admin and bool((current_app.config.get("OPENAI_API_KEY") or "").strip()),
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
    """Inventaire logiciel par poste (sélection d'un poste → ses logiciels)."""
    return render_template(
        "inventory.html",
        user=g.user,
        is_admin=(g.user.role in ("admin", "superadmin")),
    )


@bp.get("/settings")
@login_required
def settings_page():
    """Réglages de l'utilisateur courant (mot de passe, MFA)."""
    return render_template("settings.html", user=g.user)


# --------------------------------------------------------------------------
# Script d'installation en ligne (bootstrap PowerShell, gardé par jeton)
# --------------------------------------------------------------------------
@bp.get("/install.ps1")
def install_script():
    """Sert le script d'installation de l'agent pour un jeton d'installation.

    Public (pas de session) mais inopérant sans jeton valide : ``?t=<jeton>``
    issu du dashboard. Un jeton invalide/expiré renvoie un script qui affiche un
    message clair (plutôt qu'une erreur HTTP qui ferait échouer ``iwr | iex``).
    """
    from .api_deploy import _resolve_install_token
    from .install_script import render_install_script, render_invalid_script

    token = (request.args.get("t") or "").strip()
    it = _resolve_install_token(token) if token else None
    if it is None:
        body = render_invalid_script()
    else:
        body = render_install_script(request.host_url.rstrip("/"), token)
    return current_app.response_class(body, mimetype="text/plain; charset=utf-8")
