"""Point d'entrée du paquet : ``python -m truesight_agent`` et exécutable .exe.

Deux contextes sont gérés automatiquement :

1. **Console / débogage** (``python -m truesight_agent``) : lance l'agent en mode
   console, avec les options :
     - ``--enroll-only`` : effectue uniquement l'enrôlement puis quitte ;
     - ``--version``     : affiche la version et quitte.

2. **Service Windows** : lorsque l'exécutable est lancé par le gestionnaire de
   services (sans argument) ou avec une sous-commande de service
   (``install`` / ``start`` / ``stop`` / ``remove``), on délègue à pywin32.

3. **Helper bureau à distance** : la sous-commande ``remote-helper`` (avec
   ``--token`` et ``--ws-url``) lance la session de capture/injection dans la
   session interactive de l'utilisateur. Elle est invoquée automatiquement par
   le service (via CreateProcessAsUser, cf. remote/launcher.py) ; elle n'est pas
   destinée à un usage manuel.

En production, l'agent tourne en service Windows (voir truesight_agent/service.py
et install-service.ps1) ; le mode console sert au diagnostic.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__, config as cfg, runner

# Sous-commandes interprétées comme du contrôle de service (déléguées à pywin32).
_SERVICE_COMMANDS = {"install", "update", "remove", "start", "stop", "restart", "debug"}
# Options pywin32 qui PRÉCÈDENT la commande (ex. « --startup auto install ») : leur
# présence signale aussi une invocation de service.
_SERVICE_OPTION_FLAGS = {"--startup", "--username", "--password", "--interactive", "--perfmonini", "--perfmondll", "--wait"}


def build_parser() -> argparse.ArgumentParser:
    """Construit l'analyseur d'arguments du mode console."""
    parser = argparse.ArgumentParser(
        prog="truesight_agent",
        description="Agent de supervision TrueSight (mode console / débogage).",
    )
    parser.add_argument(
        "--enroll-only",
        action="store_true",
        help="Effectue uniquement l'enrôlement de l'agent puis quitte.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"TrueSight Agent {__version__}",
        help="Affiche la version de l'agent et quitte.",
    )
    return parser


def _looks_like_service_invocation(argv: list[str]) -> bool:
    """Détermine si l'invocation concerne le service Windows.

    - Exécutable figé (.exe) lancé sans argument → démarrage par le SCM.
    - Première sous-commande dans la liste des commandes de service.
    """
    # Lancé par le gestionnaire de services Windows : aucun argument utilisateur.
    if cfg.is_frozen() and len(argv) <= 1:
        return True
    # pywin32 accepte des options AVANT la commande (« --startup auto install ») :
    # on détecte donc une commande de service OU une option de service n'importe
    # où dans les arguments, pas seulement en position 1.
    lowered = [a.lower() for a in argv[1:]]
    if any(tok in _SERVICE_COMMANDS for tok in lowered):
        return True
    if any(tok in _SERVICE_OPTION_FLAGS for tok in lowered):
        return True
    return False


def _run_remote_helper(argv: list[str]) -> int:
    """Lance une session distante (sous-commande ``remote-helper``).

    Invoquée par le service dans la session interactive de l'utilisateur (voir
    remote/launcher.py) pour le bureau à distance. Bloque jusqu'à la fin.

    Par défaut (``--kind remote``), démarre la capture/injection (module remote).
    Pour cohérence avec le terminal, ``--kind terminal --shell <powershell|cmd>``
    démarre une session de terminal PTY. En pratique le terminal tourne INLINE
    dans le process agent (cf. runner) et n'a pas besoin de ce helper ; ce chemin
    n'existe que par cohérence / diagnostic.
    """
    parser = argparse.ArgumentParser(
        prog="truesight_agent remote-helper",
        description="Helper de session distante (usage interne).",
    )
    parser.add_argument("--token", required=True, help="Jeton de session (usage unique).")
    parser.add_argument("--ws-url", required=True, dest="ws_url",
                        help="URL WebSocket du relais (wss://.../ws/remote/agent?token=...).")
    parser.add_argument("--kind", default="remote", choices=["remote", "terminal"],
                        help="Type de session : 'remote' (bureau) ou 'terminal' (shell PTY).")
    parser.add_argument("--shell", default="powershell", choices=["powershell", "cmd"],
                        help="Shell à lancer si --kind terminal.")
    # argv[2:] : on saute le nom du programme et la sous-commande 'remote-helper'.
    args = parser.parse_args(argv[2:])

    # Logging console actif : le helper tourne dans la session utilisateur, ses
    # logs vont dans le fichier tournant commun (et console si dispo).
    runner.setup_logging(console=True)

    # On respecte le réglage TLS de l'agent (verify_tls) si la config est lisible.
    verify_tls = True
    try:
        verify_tls = cfg.load_config().verify_tls
    except (FileNotFoundError, ValueError):
        pass

    if args.kind == "terminal":
        from .terminal import session as terminal_session
        return terminal_session.run(
            args.token, args.ws_url, shell=args.shell, verify_tls=verify_tls
        )

    from .remote import session as remote_session
    return remote_session.run(args.token, args.ws_url, verify_tls=verify_tls)


def main(argv: list[str] | None = None) -> int:
    """Point d'entrée principal (console, service ou helper selon le contexte)."""
    argv = argv if argv is not None else sys.argv

    # Sous-commande helper bureau à distance (lancée par le service).
    if len(argv) >= 2 and argv[1].lower() == "remote-helper":
        return _run_remote_helper(argv)

    # Contexte service Windows : on délègue entièrement à service.main.
    if _looks_like_service_invocation(argv):
        from . import service
        return service.main(argv)

    # Contexte console : analyse des options et lancement du runner.
    parser = build_parser()
    args = parser.parse_args(argv[1:])
    return runner.run(console=True, enroll_only=args.enroll_only)


if __name__ == "__main__":
    sys.exit(main())
