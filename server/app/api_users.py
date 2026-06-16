"""Blueprint API JSON — gestion des accès au dashboard (réservé au superadmin).

Routes (toutes ``superadmin_required``) sous ``/api/v1/users`` :
- ``GET    /users``                    : liste des comptes
- ``POST   /users``                    : création d'un compte {email, password, role}
- ``POST   /users/<id>/role``          : changement de rôle {role}
- ``POST   /users/<id>/active``        : activation/désactivation {active}
- ``POST   /users/<id>/reset-password``: réinitialisation {new_password}
- ``DELETE /users/<id>``               : suppression

Garde-fous anti-verrouillage : on ne peut jamais rétrograder, désactiver ni
supprimer le DERNIER super-administrateur actif, ni mener une action destructrice
sur son propre compte. Toute action est tracée dans le journal d'audit (jamais le
mot de passe en clair).
"""
import re
import uuid

from flask import Blueprint, g, jsonify, request

from .extensions import db
from .models import Command, RemoteSession, User
from .models import utcnow
from .security import hash_password, superadmin_required, write_audit

bp = Blueprint("api_users", __name__, url_prefix="/api/v1")

_ROLES = ("viewer", "admin", "superadmin")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _iso_utc(dt):
    """Formate un datetime en ISO 8601 UTC (suffixe Z), ou None."""
    if dt is None:
        return None
    from datetime import timezone

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _serialize(user: User, current: User) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "mfa_enabled": bool(user.mfa_enabled),
        "created_at": _iso_utc(user.created_at),
        "is_self": user.id == current.id,
    }


def _active_superadmin_count() -> int:
    """Nombre de super-administrateurs actifs (pour le garde-fou anti-verrouillage)."""
    return (
        db.session.query(User)
        .filter(User.role == "superadmin", User.is_active.is_(True))
        .count()
    )


def _is_last_active_superadmin(user: User) -> bool:
    """True si ``user`` est le dernier super-administrateur actif du système."""
    return (
        user.role == "superadmin"
        and user.is_active
        and _active_superadmin_count() <= 1
    )


# --------------------------------------------------------------------------
# GET /users — liste
# --------------------------------------------------------------------------
@bp.get("/users")
@superadmin_required
def list_users():
    """Liste tous les comptes du dashboard (les plus récents d'abord)."""
    rows = db.session.query(User).order_by(User.created_at.desc()).all()
    return jsonify([_serialize(u, g.user) for u in rows]), 200


# --------------------------------------------------------------------------
# POST /users — création
# --------------------------------------------------------------------------
@bp.post("/users")
@superadmin_required
def create_user():
    """Crée un compte {email, password, role}. Email unique, mot de passe ≥ 8."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = (data.get("role") or "viewer").strip().lower()

    if not _EMAIL_RE.match(email):
        return jsonify({"error": "adresse e-mail invalide"}), 400
    if len(password) < 8:
        return jsonify({"error": "le mot de passe doit faire au moins 8 caractères"}), 400
    if role not in _ROLES:
        return jsonify({"error": "rôle invalide (viewer, admin, superadmin)"}), 400

    exists = db.session.query(User).filter(
        db.func.lower(User.email) == email
    ).first()
    if exists is not None:
        return jsonify({"error": "un compte avec cet e-mail existe déjà"}), 409

    user = User(
        email=email,
        password_hash=hash_password(password),
        role=role,
        mfa_enabled=False,
        is_active=True,
        created_at=utcnow(),
    )
    db.session.add(user)
    db.session.flush()
    write_audit(
        action="user.create",
        user_id=g.user.id,
        details={"target_user": str(user.id), "email": email, "role": role},
        commit=False,
    )
    db.session.commit()
    return jsonify(_serialize(user, g.user)), 201


# --------------------------------------------------------------------------
# Helpers de résolution de la cible
# --------------------------------------------------------------------------
def _get_target(user_id):
    """Résout l'utilisateur cible ; renvoie (user, None) ou (None, (payload, code))."""
    uid = _parse_uuid(user_id)
    if uid is None:
        return None, ({"error": "user_id invalide"}, 400)
    user = db.session.get(User, uid)
    if user is None:
        return None, ({"error": "compte introuvable"}, 404)
    return user, None


# --------------------------------------------------------------------------
# POST /users/<id>/role — changement de rôle
# --------------------------------------------------------------------------
@bp.post("/users/<user_id>/role")
@superadmin_required
def set_role(user_id):
    """Change le rôle d'un compte. Interdit de rétrograder le dernier superadmin."""
    user, err = _get_target(user_id)
    if err:
        return jsonify(err[0]), err[1]

    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "").strip().lower()
    if role not in _ROLES:
        return jsonify({"error": "rôle invalide (viewer, admin, superadmin)"}), 400

    if role != "superadmin" and _is_last_active_superadmin(user):
        return jsonify({"error": "impossible de rétrograder le dernier super-administrateur"}), 409

    old = user.role
    user.role = role
    write_audit(
        action="user.role",
        user_id=g.user.id,
        details={"target_user": str(user.id), "from": old, "to": role},
        commit=False,
    )
    db.session.commit()
    return jsonify(_serialize(user, g.user)), 200


# --------------------------------------------------------------------------
# POST /users/<id>/active — activation / désactivation
# --------------------------------------------------------------------------
@bp.post("/users/<user_id>/active")
@superadmin_required
def set_active(user_id):
    """Active/désactive un compte. Interdit sur soi-même et sur le dernier superadmin."""
    user, err = _get_target(user_id)
    if err:
        return jsonify(err[0]), err[1]

    data = request.get_json(silent=True) or {}
    active = bool(data.get("active"))

    if not active:
        if user.id == g.user.id:
            return jsonify({"error": "vous ne pouvez pas désactiver votre propre compte"}), 409
        if _is_last_active_superadmin(user):
            return jsonify({"error": "impossible de désactiver le dernier super-administrateur"}), 409

    user.is_active = active
    write_audit(
        action="user.active",
        user_id=g.user.id,
        details={"target_user": str(user.id), "active": active},
        commit=False,
    )
    db.session.commit()
    return jsonify(_serialize(user, g.user)), 200


# --------------------------------------------------------------------------
# POST /users/<id>/reset-password — réinitialisation du mot de passe
# --------------------------------------------------------------------------
@bp.post("/users/<user_id>/reset-password")
@superadmin_required
def reset_password(user_id):
    """Définit un nouveau mot de passe (≥ 8) pour un compte (jamais journalisé en clair)."""
    user, err = _get_target(user_id)
    if err:
        return jsonify(err[0]), err[1]

    data = request.get_json(silent=True) or {}
    new_password = data.get("new_password") or ""
    if len(new_password) < 8:
        return jsonify({"error": "le mot de passe doit faire au moins 8 caractères"}), 400

    user.password_hash = hash_password(new_password)
    write_audit(
        action="user.password_reset",
        user_id=g.user.id,
        details={"target_user": str(user.id)},
        commit=False,
    )
    db.session.commit()
    return jsonify({"ok": True}), 200


# --------------------------------------------------------------------------
# DELETE /users/<id> — suppression
# --------------------------------------------------------------------------
@bp.delete("/users/<user_id>")
@superadmin_required
def delete_user(user_id):
    """Supprime un compte. Interdit sur soi-même et sur le dernier superadmin.

    Les références (commandes créées, sessions de bureau à distance) sont
    dissociées (FK mises à NULL) pour ne pas violer l'intégrité ; le journal
    d'audit conserve l'UUID de l'auteur.
    """
    user, err = _get_target(user_id)
    if err:
        return jsonify(err[0]), err[1]

    if user.id == g.user.id:
        return jsonify({"error": "vous ne pouvez pas supprimer votre propre compte"}), 409
    if _is_last_active_superadmin(user):
        return jsonify({"error": "impossible de supprimer le dernier super-administrateur"}), 409

    # Dissocie les références FK vers ce compte avant suppression.
    db.session.query(Command).filter_by(created_by=user.id).update(
        {"created_by": None}, synchronize_session=False
    )
    db.session.query(RemoteSession).filter_by(admin_user_id=user.id).update(
        {"admin_user_id": None}, synchronize_session=False
    )

    email = user.email
    db.session.delete(user)
    write_audit(
        action="user.delete",
        user_id=g.user.id,
        details={"target_user": str(user_id), "email": email},
        commit=False,
    )
    db.session.commit()
    return jsonify({"ok": True}), 200
