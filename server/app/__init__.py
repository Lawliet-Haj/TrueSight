"""App factory TrueSight (cf. SPEC §7).

``create_app()`` :
- charge la configuration depuis l'environnement ;
- initialise la base de données ;
- enregistre les blueprints (api_agent, api_dashboard, web) ;
- au démarrage : ``db.create_all()`` + ``seed.ensure_admin()`` + ``seed.ensure_alert_rules()`` ;
- lance le thread de fond ``tasks.start_background(app)`` ;
- durcit les cookies de session (Secure / HttpOnly / SameSite=Lax).
"""
import logging

from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .extensions import db


def create_app(config_object: type | None = None) -> Flask:
    """Crée et configure l'application Flask."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = Flask(__name__)
    app.config.from_object(config_object or Config)

    # Derrière nginx : faire confiance à X-Forwarded-For / X-Forwarded-Proto
    # (1 seul proxy). Sans cela, request.remote_addr vaut l'IP interne de nginx
    # → audit faussé ET rate-limit /enroll global à tout le parc (429 en masse
    # lors d'un déploiement GPO). Désactivable via TRUST_PROXY=false (dev direct).
    if app.config.get("TRUST_PROXY", True):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Durcissement explicite des cookies de session (défense en profondeur).
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")

    # --- Extensions ---
    db.init_app(app)

    # --- Modèles (import pour enregistrement des tables) ---
    from . import models  # noqa: F401

    # --- Blueprints ---
    from .api_agent import bp as api_agent_bp
    from .api_dashboard import bp as api_dashboard_bp
    from .api_deploy import bp as api_deploy_bp
    from .api_users import bp as api_users_bp
    from .web import bp as web_bp

    app.register_blueprint(api_agent_bp)
    app.register_blueprint(api_dashboard_bp)
    app.register_blueprint(api_deploy_bp)
    app.register_blueprint(api_users_bp)
    app.register_blueprint(web_bp)

    # --- Healthcheck simple (utile derrière nginx / Docker) ---
    @app.get("/healthz")
    def healthz():
        """Point de santé léger (ne touche pas la base)."""
        return jsonify({"status": "ok"}), 200

    # --- Gestion d'erreurs JSON pour l'API ---
    _register_error_handlers(app)

    # --- En-têtes de sécurité HTTP (défense en profondeur) ---
    _register_security_headers(app)

    # --- Initialisation base + seed ---
    with app.app_context():
        from . import seed

        db.create_all()
        seed.ensure_admin()
        seed.ensure_alert_rules()

    # --- Thread de fond (alertes + purge) ---
    from . import tasks

    tasks.start_background(app)

    return app


def _register_security_headers(app: Flask):
    """Ajoute des en-têtes de sécurité HTTP sur toutes les réponses.

    - ``X-Content-Type-Options: nosniff`` : pas de sniffing de type MIME.
    - ``X-Frame-Options: DENY`` + ``frame-ancestors 'none'`` : anti-clickjacking
      (le dashboard n'est jamais censé être affiché dans une iframe).
    - ``Referrer-Policy`` / ``Permissions-Policy`` : limite les fuites et désactive
      les API navigateur inutiles (caméra, micro, géoloc).
    - ``Strict-Transport-Security`` : envoyé UNIQUEMENT sur une requête HTTPS
      (``request.is_secure`` reflète X-Forwarded-Proto via ProxyFix), pour ne pas
      casser l'accès HTTP de la pile de test.
    - ``Content-Security-Policy`` : envoyée si ``CONTENT_SECURITY_POLICY`` est
      définie (désactivée par défaut tant qu'elle n'a pas été validée navigateur,
      pour ne pas risquer de bloquer le bureau à distance / les CDN).
    """
    from flask import request

    hsts_enabled = app.config.get("ENABLE_HSTS", True)
    csp = (app.config.get("CONTENT_SECURITY_POLICY") or "").strip()

    @app.after_request
    def _set_security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        if csp:
            resp.headers.setdefault("Content-Security-Policy", csp)
        if hsts_enabled and request.is_secure:
            resp.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return resp


def _register_error_handlers(app: Flask):
    """Enregistre des gestionnaires d'erreurs renvoyant du JSON pour les routes API."""
    from flask import request

    def _is_api():
        return request.path.startswith("/api/")

    @app.errorhandler(404)
    def not_found(_e):
        if _is_api():
            return jsonify({"error": "ressource introuvable"}), 404
        return _e  # HTTPException werkzeug : réponse valide telle quelle.

    @app.errorhandler(400)
    def bad_request(_e):
        if _is_api():
            return jsonify({"error": "requête invalide"}), 400
        return _e

    @app.errorhandler(500)
    def server_error(_e):
        if _is_api():
            return jsonify({"error": "erreur interne du serveur"}), 500
        return _e
