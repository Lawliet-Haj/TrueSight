"""Sécurité : hachage de token, authentification agent et dashboard, audit.

- Token agent : généré via ``secrets.token_urlsafe`` (32 octets), stocké hashé SHA-256.
- Authentification agent : en-tête ``Authorization: Bearer <token>`` + agent_id du path.
- Authentification dashboard : session Flask (``user_id`` + rôle).
- Helper ``write_audit`` pour tracer les actions sensibles.
"""
import functools
import hashlib
import secrets
import threading
import time
import uuid

from flask import g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .models import Agent, AuditLog, User


# --------------------------------------------------------------------------
# Tokens agent
# --------------------------------------------------------------------------
def generate_agent_token() -> str:
    """Génère un token agent aléatoire (>= 32 octets, base64url)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Retourne le hash SHA-256 hexadécimal d'un token (agent ou session)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_session_token() -> str:
    """Génère un jeton de session de bureau à distance (>= 32 octets, base64url url-safe).

    Réutilise la même primitive que les tokens agent (``secrets.token_urlsafe``).
    Le jeton est transmis une seule fois (au navigateur et à l'agent via la
    signalisation) et stocké hashé SHA-256 côté serveur (cf. REMOTE.md §7).
    """
    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------
# Cache mémoire des jetons de session de bureau à distance
# --------------------------------------------------------------------------
# La table ``remote_sessions`` ne stocke que le hash du jeton (jamais le clair).
# Or l'agent doit recevoir le jeton EN CLAIR via la signalisation (réponse au
# heartbeat / GET commands). On conserve donc le jeton en clair en mémoire, le
# temps du TTL d'appariement (~60 s), indexé par ``session_id``. C'est éphémère
# et non persistant (un redémarrage du worker invalide les sessions en cours
# d'appariement, ce qui est acceptable : l'admin relance la session).
# TTL du cache : au-delà, l'entrée est purgée d'office (borne la mémoire même si
# une session est appariée — son jeton n'a plus à être re-signalé une fois active).
_REMOTE_TOKEN_TTL_SECONDS = 120
_remote_token_lock = threading.Lock()
_remote_session_tokens: dict[str, tuple[str, float]] = {}


def _purge_expired_tokens_locked() -> None:
    """Retire les jetons plus vieux que le TTL (appelé sous verrou)."""
    now = time.monotonic()
    stale = [
        sid for sid, (_tok, ts) in _remote_session_tokens.items()
        if now - ts > _REMOTE_TOKEN_TTL_SECONDS
    ]
    for sid in stale:
        _remote_session_tokens.pop(sid, None)


def store_session_token(session_id: str, token: str) -> None:
    """Mémorise le jeton en clair d'une session (indexé par session_id)."""
    with _remote_token_lock:
        _purge_expired_tokens_locked()
        _remote_session_tokens[str(session_id)] = (token, time.monotonic())


def pop_session_token(session_id: str) -> str | None:
    """Récupère (sans retirer) le jeton en clair d'une session, ou None si absent/expiré.

    Lecture non destructive : heartbeat ET poll commandes peuvent lire le jeton
    tant que la session est « requested ». La purge TTL borne la mémoire ;
    ``forget_session_token`` est appelé explicitement dès l'expiration côté
    signalisation (cf. api_agent._remote_session_for_agent).
    """
    with _remote_token_lock:
        _purge_expired_tokens_locked()
        entry = _remote_session_tokens.get(str(session_id))
        return entry[0] if entry else None


def forget_session_token(session_id: str) -> None:
    """Oublie le jeton en clair d'une session (libération mémoire)."""
    with _remote_token_lock:
        _remote_session_tokens.pop(str(session_id), None)


# --------------------------------------------------------------------------
# Mots de passe (dashboard)
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Hache un mot de passe via werkzeug (pbkdf2)."""
    return generate_password_hash(password, method="pbkdf2:sha256")


def verify_password(password_hash: str, password: str) -> bool:
    """Vérifie un mot de passe en clair contre son hash werkzeug."""
    if not password_hash:
        return False
    return check_password_hash(password_hash, password)


# --------------------------------------------------------------------------
# Décorateur d'authentification agent
# --------------------------------------------------------------------------
def _parse_bearer_token() -> str | None:
    """Extrait le token de l'en-tête ``Authorization: Bearer <token>``."""
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def _authenticate_agent(agent: Agent | None, token: str):
    """Vérifie un agent candidat contre le token Bearer fourni.

    Retourne (agent, None) en cas de succès, ou (None, (payload, code)) en cas
    d'échec. Place l'agent dans ``g.agent`` si l'authentification réussit.
    """
    if agent is None:
        # Comparaison factice pour limiter les fuites de timing.
        secrets.compare_digest(hash_token(token), "0" * 64)
        return None, ({"error": "agent introuvable"}, 401)

    if not agent.is_active:
        return None, ({"error": "agent révoqué"}, 401)

    if not secrets.compare_digest(agent.token_hash or "", hash_token(token)):
        return None, ({"error": "token invalide"}, 401)

    g.agent = agent
    return agent, None


def agent_required(view):
    """Décorateur : authentifie l'agent à partir du Bearer token et de l'agent_id du path.

    Place l'agent authentifié dans ``g.agent``. Renvoie 401 si :
    - en-tête Authorization absent / mal formé,
    - agent_id introuvable,
    - hash du token ne correspond pas,
    - agent révoqué (``is_active = false``).
    """

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        token = _parse_bearer_token()
        if not token:
            return jsonify({"error": "authentification requise"}), 401

        agent = _resolve_agent(kwargs.get("agent_id"))
        authed, error = _authenticate_agent(agent, token)
        if error is not None:
            payload, code = error
            return jsonify(payload), code
        return view(*args, **kwargs)

    return wrapper


def agent_required_by_command(view):
    """Décorateur pour ``/commands/<command_id>/result`` (pas d'agent_id dans le path).

    L'agent est résolu via le ``agent_id`` de la commande visée, puis authentifié
    avec le Bearer token. Renvoie 404 si la commande est introuvable, 401 si
    l'authentification échoue.
    """
    # Import local pour éviter un import circulaire au chargement du module.
    from .models import Command

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        token = _parse_bearer_token()
        if not token:
            return jsonify({"error": "authentification requise"}), 401

        command_id = kwargs.get("command_id")
        try:
            cmd_uuid = uuid.UUID(str(command_id))
        except (ValueError, TypeError):
            return jsonify({"error": "command_id invalide"}), 400

        command = db.session.get(Command, cmd_uuid)
        if command is None:
            # On exige une authentification valide avant de divulguer l'existence.
            secrets.compare_digest(hash_token(token), "0" * 64)
            return jsonify({"error": "commande introuvable"}), 404

        agent = db.session.get(Agent, command.agent_id)
        authed, error = _authenticate_agent(agent, token)
        if error is not None:
            payload, code = error
            return jsonify(payload), code

        # Expose la commande déjà résolue pour éviter une seconde requête.
        g.command = command
        return view(*args, **kwargs)

    return wrapper


def _resolve_agent(agent_id) -> Agent | None:
    """Retrouve un agent par son UUID (string ou UUID), tolérant aux formats invalides."""
    if agent_id is None:
        return None
    try:
        if not isinstance(agent_id, uuid.UUID):
            agent_id = uuid.UUID(str(agent_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return db.session.get(Agent, agent_id)


# --------------------------------------------------------------------------
# Authentification dashboard (session)
# --------------------------------------------------------------------------
def current_user() -> User | None:
    """Retourne l'utilisateur connecté (ou None) à partir de la session."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return None
    user = db.session.get(User, uid)
    if user is None or not user.is_active:
        return None
    return user


def _wants_json() -> bool:
    """Détermine si la requête courante attend une réponse JSON (API)."""
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def login_required(view):
    """Décorateur : exige une session authentifiée (admin ou viewer)."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if _wants_json():
                return jsonify({"error": "authentification requise"}), 401
            return redirect(url_for("web.login", next=request.path))
        g.user = user
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    """Décorateur : exige une session authentifiée avec un rôle administrateur.

    ``superadmin`` est un sur-ensemble d'``admin`` : il dispose de tous les
    pouvoirs admin (commandes, bureau à distance, actions rapides) EN PLUS de la
    gestion des accès.
    """

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if _wants_json():
                return jsonify({"error": "authentification requise"}), 401
            return redirect(url_for("web.login", next=request.path))
        if user.role not in ("admin", "superadmin"):
            if _wants_json():
                return jsonify({"error": "accès réservé aux administrateurs"}), 403
            return jsonify({"error": "accès réservé aux administrateurs"}), 403
        g.user = user
        return view(*args, **kwargs)

    return wrapper


def superadmin_required(view):
    """Décorateur : exige une session authentifiée avec le rôle ``superadmin``.

    Réservé à la gestion des accès (création / rôle / activation / suppression
    de comptes du dashboard).
    """

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if _wants_json():
                return jsonify({"error": "authentification requise"}), 401
            return redirect(url_for("web.login", next=request.path))
        if user.role != "superadmin":
            return jsonify({"error": "accès réservé au super-administrateur"}), 403
        g.user = user
        return view(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------
def write_audit(action: str, user_id=None, target_agent=None, details=None, commit: bool = True):
    """Enregistre une entrée dans le journal d'audit (append-only).

    ``action`` : ex. ``command.create``, ``agent.revoke``, ``login.success``.
    ``user_id`` / ``target_agent`` : UUID ou None.
    ``details`` : dict sérialisable JSON.
    L'IP est récupérée automatiquement depuis la requête courante.
    """
    ip = None
    try:
        # request.remote_addr peut être None hors contexte requête.
        ip = request.remote_addr
    except RuntimeError:
        ip = None

    entry = AuditLog(
        action=action,
        user_id=_coerce_uuid(user_id),
        target_agent=_coerce_uuid(target_agent),
        ip=ip,
        details=details or {},
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry


def _coerce_uuid(value):
    """Convertit une valeur en UUID si possible, sinon None."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None
