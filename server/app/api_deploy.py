"""Blueprint déploiement & mises à jour de l'agent.

Trois familles d'endpoints (toutes sous ``/api/v1``) :

1. **Agent** (Bearer agent) :
   - ``GET /agents/<id>/package`` : télécharge le paquet courant (auto-update).

2. **Dashboard** (session) :
   - ``GET/POST /agent-releases``            : liste / publication d'un paquet (superadmin) ;
   - ``POST /agent-releases/<id>/current``   : définit la release courante (superadmin) ;
   - ``DELETE /agent-releases/<id>``         : supprime une release (superadmin) ;
   - ``GET/POST /install-tokens``            : liste / génération d'un lien d'installation (admin) ;
   - ``DELETE /install-tokens/<id>``         : révocation d'un lien (admin).

3. **Installation** (jeton d'installation, sans session) :
   - ``GET /install/<token>/package`` : paquet de l'agent ;
   - ``GET /install/<token>/config``  : ``config.ini`` (URL serveur + enrollment_token).
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import timedelta, timezone

from flask import Blueprint, current_app, g, jsonify, request, send_file

from .extensions import db
from .models import AgentRelease, InstallToken, Site, User, utcnow
from .releases import (
    current_release,
    read_zip_version,
    release_dir,
    release_path,
    sha256_file,
)
from .security import (
    admin_required,
    agent_required,
    generate_session_token,
    hash_token,
    login_required,
    superadmin_required,
    write_audit,
)

bp = Blueprint("api_deploy", __name__, url_prefix="/api/v1")

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _aware(dt):
    """Rend un datetime « aware » (UTC) pour comparaison sûre."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# --------------------------------------------------------------------------
# 1. Agent : téléchargement du paquet courant (auto-update)
# --------------------------------------------------------------------------
@bp.get("/agents/<agent_id>/package")
@agent_required
def agent_package(agent_id):
    """Streame le paquet de l'agent courant (auth Bearer agent)."""
    rel = current_release()
    if rel is None:
        return jsonify({"error": "aucune release publiée"}), 404
    path = release_path(rel)
    if not os.path.isfile(path):
        return jsonify({"error": "paquet introuvable sur le serveur"}), 404
    return send_file(
        path, mimetype="application/zip", as_attachment=True,
        download_name=rel.filename, conditional=True,
    )


# --------------------------------------------------------------------------
# 2. Dashboard : gestion des releases (superadmin)
# --------------------------------------------------------------------------
def _release_payload(rel: AgentRelease, emails: dict) -> dict:
    return {
        "id": str(rel.id),
        "version": rel.version,
        "filename": rel.filename,
        "sha256": rel.sha256,
        "size": rel.size,
        "notes": rel.notes,
        "is_current": rel.is_current,
        "published_by": emails.get(rel.published_by),
        "published_at": _iso(rel.published_at),
    }


@bp.get("/agent-releases")
@admin_required
def list_releases():
    """Liste les paquets publiés (récent d'abord). Lecture : admin + superadmin."""
    rows = (
        db.session.query(AgentRelease)
        .order_by(AgentRelease.published_at.desc())
        .all()
    )
    user_ids = {r.published_by for r in rows if r.published_by}
    emails = {}
    if user_ids:
        for u in db.session.query(User).filter(User.id.in_(user_ids)).all():
            emails[u.id] = u.email
    return jsonify([_release_payload(r, emails) for r in rows]), 200


@bp.post("/agent-releases")
@superadmin_required
def publish_release():
    """Publie un paquet d'agent (zip onedir). Réservé au super-administrateur.

    Form-data multipart : ``file`` (le zip), ``notes`` (optionnel),
    ``make_current`` (optionnel, défaut true), ``version`` (optionnel : utilisé
    si le zip ne contient pas de ``version.txt``).
    """
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "fichier manquant (champ 'file')"}), 400

    notes = (request.form.get("notes") or "").strip() or None
    make_current = (request.form.get("make_current", "true").strip().lower()
                    not in ("0", "false", "no", "non"))
    forced_version = (request.form.get("version") or "").strip()

    rdir = release_dir()
    tmp_path = os.path.join(rdir, f".upload-{uuid.uuid4().hex}.tmp")
    try:
        upload.save(tmp_path)
    except OSError as exc:
        return jsonify({"error": f"écriture impossible : {exc}"}), 500

    try:
        version = read_zip_version(tmp_path) or forced_version
        if not version:
            return jsonify({
                "error": "version introuvable : le zip ne contient pas de "
                         "version.txt et aucune 'version' n'a été fournie"
            }), 400
        if not _VERSION_RE.match(version):
            return jsonify({"error": f"version invalide : {version} (attendu MAJOR.MINOR.PATCH)"}), 400

        filename = f"truesight-agent-{version}.zip"
        final_path = os.path.join(rdir, filename)
        # Remplace une éventuelle release de même version (réécriture).
        os.replace(tmp_path, final_path)
        tmp_path = None  # déplacé

        sha = sha256_file(final_path)
        size = os.path.getsize(final_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # Upsert sur la version (une seule ligne par version).
    rel = db.session.query(AgentRelease).filter_by(version=version).one_or_none()
    if rel is None:
        rel = AgentRelease(version=version)
        db.session.add(rel)
    rel.filename = filename
    rel.sha256 = sha
    rel.size = size
    rel.notes = notes
    rel.published_by = g.user.id
    rel.published_at = utcnow()

    if make_current:
        db.session.query(AgentRelease).filter(AgentRelease.version != version).update(
            {"is_current": False}, synchronize_session=False
        )
        rel.is_current = True

    db.session.flush()
    write_audit(
        action="agent.release.publish", user_id=g.user.id,
        details={"version": version, "sha256": sha, "size": size, "current": rel.is_current},
        commit=False,
    )
    db.session.commit()
    return jsonify({"id": str(rel.id), "version": version, "is_current": rel.is_current}), 201


@bp.post("/agent-releases/<release_id>/current")
@superadmin_required
def set_current_release(release_id):
    """Définit la release courante (celle servie à l'auto-update et à l'installation)."""
    try:
        rid = uuid.UUID(str(release_id))
    except (ValueError, TypeError):
        return jsonify({"error": "id invalide"}), 400
    rel = db.session.get(AgentRelease, rid)
    if rel is None:
        return jsonify({"error": "release introuvable"}), 404

    db.session.query(AgentRelease).update({"is_current": False}, synchronize_session=False)
    rel.is_current = True
    write_audit(
        action="agent.release.current", user_id=g.user.id,
        details={"version": rel.version}, commit=False,
    )
    db.session.commit()
    return jsonify({"id": str(rel.id), "version": rel.version, "is_current": True}), 200


@bp.delete("/agent-releases/<release_id>")
@superadmin_required
def delete_release(release_id):
    """Supprime une release (métadonnées + fichier). Interdit sur la release courante."""
    try:
        rid = uuid.UUID(str(release_id))
    except (ValueError, TypeError):
        return jsonify({"error": "id invalide"}), 400
    rel = db.session.get(AgentRelease, rid)
    if rel is None:
        return jsonify({"error": "release introuvable"}), 404
    if rel.is_current:
        return jsonify({"error": "impossible de supprimer la release courante"}), 400

    path = release_path(rel)
    version = rel.version
    db.session.delete(rel)
    write_audit(
        action="agent.release.delete", user_id=g.user.id,
        details={"version": version}, commit=False,
    )
    db.session.commit()
    # Supprime le fichier après le commit (best effort).
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
    return jsonify({"ok": True}), 200


# --------------------------------------------------------------------------
# 3. Dashboard : liens d'installation (admin + superadmin)
# --------------------------------------------------------------------------
def _token_active(it: InstallToken) -> bool:
    if it.revoked:
        return False
    exp = _aware(it.expires_at)
    if exp is not None and utcnow() > exp:
        return False
    return True


def _token_payload(it: InstallToken, emails: dict, sites: dict) -> dict:
    return {
        "id": str(it.id),
        "label": it.label,
        "site_id": str(it.site_id) if it.site_id else None,
        "site_name": sites.get(it.site_id),
        "created_by": emails.get(it.created_by),
        "created_at": _iso(it.created_at),
        "expires_at": _iso(it.expires_at),
        "revoked": it.revoked,
        "active": _token_active(it),
        "use_count": it.use_count,
        "last_used_at": _iso(it.last_used_at),
    }


@bp.get("/install-tokens")
@admin_required
def list_install_tokens():
    """Liste les liens d'installation (récent d'abord)."""
    rows = (
        db.session.query(InstallToken)
        .order_by(InstallToken.created_at.desc())
        .limit(100)
        .all()
    )
    user_ids = {r.created_by for r in rows if r.created_by}
    emails = {}
    if user_ids:
        for u in db.session.query(User).filter(User.id.in_(user_ids)).all():
            emails[u.id] = u.email
    sites = {s.id: s.name for s in db.session.query(Site).all()}
    return jsonify([_token_payload(r, emails, sites) for r in rows]), 200


@bp.get("/enrollment-token")
@superadmin_required
def get_enrollment_token():
    """Renvoie le jeton d'enrôlement partagé (superadmin) — saisie dans l'installeur .exe.

    Réservé au super-administrateur (secret maître). L'accès est audité
    (``enrollment.token.view``) car c'est une lecture sensible.
    """
    write_audit(action="enrollment.token.view", user_id=g.user.id, details={})
    return jsonify({"token": current_app.config.get("ENROLLMENT_TOKEN", "")}), 200


@bp.post("/install-tokens")
@admin_required
def create_install_token():
    """Génère un lien d'installation. Renvoie le jeton EN CLAIR une seule fois.

    Body JSON optionnel : ``{label, ttl_days}`` (ttl_days <= 0 → sans expiration).
    """
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()[:80] or None
    default_ttl = current_app.config.get("INSTALL_TOKEN_TTL_DAYS", 7)
    try:
        ttl_days = int(data.get("ttl_days", default_ttl))
    except (TypeError, ValueError):
        ttl_days = default_ttl

    # Emplacement pré-affecté (optionnel) : les postes installés via ce lien le
    # rejoignent automatiquement à l'enrôlement.
    site_id = None
    site_name = None
    raw_site = data.get("site_id")
    if raw_site:
        try:
            site_uuid = uuid.UUID(str(raw_site))
        except (ValueError, TypeError):
            return jsonify({"error": "site_id invalide"}), 400
        site = db.session.get(Site, site_uuid)
        if site is None:
            return jsonify({"error": "emplacement introuvable"}), 404
        site_id = site.id
        site_name = site.name

    expires_at = None
    if ttl_days > 0:
        expires_at = utcnow() + timedelta(days=ttl_days)

    token = generate_session_token()
    it = InstallToken(
        token_hash=hash_token(token),
        label=label,
        site_id=site_id,
        created_by=g.user.id,
        created_at=utcnow(),
        expires_at=expires_at,
    )
    db.session.add(it)
    db.session.flush()
    write_audit(
        action="install.token.create", user_id=g.user.id,
        details={"token_id": str(it.id), "label": label, "ttl_days": ttl_days},
        commit=False,
    )
    db.session.commit()

    base = request.host_url.rstrip("/")
    install_url = f"{base}/install.ps1?t={token}"
    one_liner = (
        'powershell -ExecutionPolicy Bypass -Command '
        f'"iwr -useb {install_url} | iex"'
    )
    return (
        jsonify({
            "id": str(it.id),
            "token": token,
            "site_name": site_name,
            "install_url": install_url,
            "one_liner": one_liner,
            # Installeur double-cliquable (.cmd) servi par ce lien.
            "installer_cmd_url": f"{base}/install/{token}/installer.cmd",
            "expires_at": _iso(expires_at),
        }),
        201,
    )


@bp.delete("/install-tokens/<token_id>")
@admin_required
def revoke_install_token(token_id):
    """Révoque un lien d'installation (irréversible)."""
    try:
        tid = uuid.UUID(str(token_id))
    except (ValueError, TypeError):
        return jsonify({"error": "id invalide"}), 400
    it = db.session.get(InstallToken, tid)
    if it is None:
        return jsonify({"error": "lien introuvable"}), 404
    it.revoked = True
    write_audit(
        action="install.token.revoke", user_id=g.user.id,
        details={"token_id": str(it.id)}, commit=False,
    )
    db.session.commit()
    return jsonify({"ok": True}), 200


# --------------------------------------------------------------------------
# 4. Installation : endpoints gardés par le jeton d'installation (sans session)
# --------------------------------------------------------------------------
def _resolve_install_token(token: str) -> InstallToken | None:
    """Retrouve un jeton d'installation actif depuis sa valeur en clair, ou None."""
    if not token:
        return None
    it = (
        db.session.query(InstallToken)
        .filter_by(token_hash=hash_token(token))
        .one_or_none()
    )
    if it is None or not _token_active(it):
        return None
    return it


@bp.get("/install/<token>/package")
def install_package(token):
    """Streame le paquet de l'agent courant (gardé par le jeton d'installation)."""
    it = _resolve_install_token(token)
    if it is None:
        return jsonify({"error": "lien invalide ou expiré"}), 403
    rel = current_release()
    if rel is None:
        return jsonify({"error": "aucune release publiée"}), 404
    path = release_path(rel)
    if not os.path.isfile(path):
        return jsonify({"error": "paquet introuvable sur le serveur"}), 404
    return send_file(
        path, mimetype="application/zip", as_attachment=True,
        download_name=rel.filename, conditional=True,
    )


@bp.get("/install/<token>/config")
def install_config(token):
    """Renvoie le ``config.ini`` (URL serveur + enrollment_token) pour ce lien.

    Compte un « usage » (use_count + last_used_at) et audite ``install.config`` :
    c'est l'étape qui matérialise une installation effective sur un poste.
    """
    it = _resolve_install_token(token)
    if it is None:
        return jsonify({"error": "lien invalide ou expiré"}), 403

    base = request.host_url.rstrip("/")
    enrollment_token = current_app.config.get("ENROLLMENT_TOKEN", "")
    # Emplacement pré-affecté : écrit dans config.ini ; l'agent l'envoie à
    # l'enrôlement et le serveur l'affecte au poste (find-or-create).
    site_line = ""
    if it.site_id:
        site = db.session.get(Site, it.site_id)
        if site is not None:
            site_line = f"site = {site.name}\n"
    config_text = (
        "[server]\n"
        f"url = {base}\n"
        f"enrollment_token = {enrollment_token}\n"
        "verify_tls = true\n"
        "\n"
        "[agent]\n"
        "heartbeat_interval = 30\n"
        "command_poll_interval = 8\n"
        "inventory_interval_hours = 12\n"
        f"{site_line}"
    )

    it.use_count = (it.use_count or 0) + 1
    it.last_used_at = utcnow()
    write_audit(
        action="install.config",
        details={"token_id": str(it.id), "label": it.label, "ip": request.remote_addr},
        commit=False,
    )
    db.session.commit()
    return current_app.response_class(config_text, mimetype="text/plain; charset=utf-8")


def _installer_cmd_text(install_url: str) -> str:
    """Génère un .cmd Windows double-cliquable : s'auto-élève (UAC) puis lance le
    bootstrap d'installation. ASCII pur + CRLF (console Windows)."""
    lines = [
        "@echo off",
        "setlocal",
        "REM === Installateur de l'agent TrueSight ===",
        "REM Double-cliquez ce fichier (ou clic droit > Executer en tant qu'administrateur).",
        "net session >nul 2>&1",
        "if %errorlevel% NEQ 0 (",
        "  echo Demande d'elevation des privileges (UAC)...",
        "  powershell -NoProfile -ExecutionPolicy Bypass -Command \"Start-Process -FilePath '%~f0' -Verb RunAs\"",
        "  exit /b",
        ")",
        "echo.",
        "echo Installation de l'agent TrueSight en cours...",
        "echo.",
        ("powershell -NoProfile -ExecutionPolicy Bypass -Command "
         "\"try { iwr -useb '" + install_url + "' | iex } "
         "catch { Write-Host ('ECHEC: ' + $_.Exception.Message) -ForegroundColor Red }\""),
        "echo.",
        "echo Termine. Vous pouvez fermer cette fenetre.",
        "pause",
        "endlocal",
    ]
    return "\r\n".join(lines) + "\r\n"


@bp.get("/install/<token>/installer.cmd")
def install_cmd(token):
    """Installeur Windows double-cliquable (.cmd) gardé par le jeton d'installation.

    Contient le lien bootstrap (avec le jeton) ; à l'exécution, le .cmd s'auto-élève
    puis télécharge le paquet + ``config.ini`` et installe le service.
    """
    it = _resolve_install_token(token)
    if it is None:
        return jsonify({"error": "lien invalide ou expiré"}), 403
    base = request.host_url.rstrip("/")
    body = _installer_cmd_text(f"{base}/install.ps1?t={token}")
    resp = current_app.response_class(body, mimetype="application/octet-stream")
    resp.headers["Content-Disposition"] = 'attachment; filename="TrueSight-Installer.cmd"'
    return resp
