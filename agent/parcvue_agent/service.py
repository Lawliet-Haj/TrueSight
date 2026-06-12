"""Service Windows de l'agent ParcVue (pywin32).

Encapsule le runner dans un service Windows nommé ``ParcVueAgent`` :
- ``SvcDoRun``  : crée le runner et lance les boucles ;
- ``SvcStop``   : déclenche l'arrêt propre du runner.

Installation / contrôle (en administrateur) :
    python -m parcvue_agent.service install
    python -m parcvue_agent.service start
    python -m parcvue_agent.service stop
    python -m parcvue_agent.service remove

En production, le service est généralement installé via l'exécutable .exe
(voir install-service.ps1). Le mode console reste accessible via
``python -m parcvue_agent``.
"""

from __future__ import annotations

import logging
import sys

# Imports pywin32 tolérants : permettent d'importer le module hors Windows
# (ex. pour la documentation) sans crasher. L'usage réel exige pywin32.
try:
    import servicemanager  # type: ignore
    import win32event  # type: ignore
    import win32service  # type: ignore
    import win32serviceutil  # type: ignore
    _PYWIN32_AVAILABLE = True
    _ServiceFrameworkBase = win32serviceutil.ServiceFramework
except ImportError:  # pragma: no cover - hors Windows / pywin32 absent.
    servicemanager = None  # type: ignore
    win32event = None  # type: ignore
    win32service = None  # type: ignore
    win32serviceutil = None  # type: ignore

    # Classe de base de repli : permet d'IMPORTER le module même sans pywin32
    # (l'instanciation réelle du service échouera proprement via service.main).
    class _ServiceFrameworkBase:  # type: ignore[no-redef]
        """Base factice utilisée uniquement quand pywin32 est absent."""

        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover
            raise RuntimeError("pywin32 est requis pour le mode service Windows.")

    _PYWIN32_AVAILABLE = False

from . import runner as runner_module

_logger = logging.getLogger("parcvue.service")


class ParcVueService(_ServiceFrameworkBase):  # type: ignore[misc]
    """Définition du service Windows ParcVue."""

    # Nom interne du service (utilisé par sc.exe / install-service.ps1).
    _svc_name_ = "ParcVueAgent"
    # Nom affiché dans la console des services.
    _svc_display_name_ = "Agent ParcVue"
    # Description visible dans services.msc.
    _svc_description_ = (
        "Agent de supervision ParcVue : inventaire matériel/logiciel, "
        "métriques et exécution de commandes à distance."
    )

    def __init__(self, args) -> None:
        _ServiceFrameworkBase.__init__(self, args)
        # Événement signalé par le SCM pour demander l'arrêt.
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._runner: runner_module.AgentRunner | None = None

    def SvcStop(self) -> None:
        """Demande d'arrêt du service (appelée par le gestionnaire de services)."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        _logger.info("Service ParcVue : arrêt demandé par le SCM.")
        if self._runner is not None:
            self._runner.stop()
        # Réveille SvcDoRun.
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self) -> None:
        """Point d'exécution principal du service."""
        # Journalise le démarrage dans le journal d'événements Windows.
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        try:
            runner_module.setup_logging(console=False)
            _logger.info("Service ParcVue : démarrage.")
            self._runner = runner_module.create_runner()
            # run() est bloquant jusqu'à l'arrêt (SvcStop appelle runner.stop()).
            self._runner.run()
        except Exception as exc:  # noqa: BLE001 - on journalise toute erreur fatale.
            _logger.error("Service ParcVue : erreur fatale : %s", exc)
            try:
                servicemanager.LogErrorMsg(f"Agent ParcVue : erreur fatale : {exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            _logger.info("Service ParcVue : arrêté.")


def main(argv: list[str] | None = None) -> int:
    """Gestion en ligne de commande du service (install/start/stop/remove).

    Si pywin32 est absent, on signale clairement l'erreur sans crasher.
    """
    argv = argv if argv is not None else sys.argv

    # Garde-fou : la sous-commande « remote-helper » (bureau à distance) ne
    # concerne pas le service Windows. Normalement interceptée en amont par
    # __main__.main ; ce repli évite de la passer par erreur au SCM si
    # service.main est appelé directement.
    if len(argv) >= 2 and argv[1].lower() == "remote-helper":
        from . import __main__ as agent_main
        return agent_main._run_remote_helper(argv)

    if not _PYWIN32_AVAILABLE:
        sys.stderr.write(
            "pywin32 est requis pour le mode service Windows. "
            "Installer les dépendances : pip install -r requirements.txt\n"
        )
        return 1

    if len(argv) == 1:
        # Lancé sans argument par le SCM : on démarre la boucle de dispatch.
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ParcVueService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0

    # Délègue à pywin32 la gestion des sous-commandes (install, start, ...).
    win32serviceutil.HandleCommandLine(ParcVueService, argv=argv)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
