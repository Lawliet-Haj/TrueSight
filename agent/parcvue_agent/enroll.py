"""Flux d'enrôlement de l'agent ParcVue.

Au premier démarrage (ou si l'état local est absent), l'agent appelle
``POST /api/v1/enroll`` avec son empreinte machine et reçoit en retour un
``agent_id`` + ``agent_token`` qu'il persiste dans state.json.

Le flux est **idempotent** côté serveur (unicité sur ``machine_id``) : un
agent déjà enrôlé reçoit simplement une rotation de token. Côté agent, si un
état valide existe déjà, on charge simplement les credentials sans réenrôler
(sauf demande explicite via ``force``).
"""

from __future__ import annotations

import logging

from . import __version__, config as cfg
from .client import ApiClient

_logger = logging.getLogger("parcvue.enroll")


def ensure_enrolled(
    client: ApiClient,
    agent_config: cfg.AgentConfig,
    force: bool = False,
) -> cfg.AgentState:
    """Garantit que l'agent est enrôlé et que ``client`` porte ses credentials.

    - Si un état valide existe et ``force`` est False : on réutilise l'état.
    - Sinon : on effectue l'enrôlement et on persiste le résultat.

    Retourne l'``AgentState`` à jour. Lève une exception uniquement si
    l'enrôlement est impossible (token invalide, etc.) — le runner réessaiera.
    """
    state = cfg.load_state()

    if state.is_enrolled and not force:
        _logger.info("Agent déjà enrôlé (agent_id=%s), réutilisation de l'état.", state.agent_id)
        client.set_credentials(state.agent_id, state.agent_token)
        return state

    if force:
        _logger.info("Réenrôlement forcé demandé (token probablement révoqué).")

    return _perform_enroll(client, agent_config)


def _perform_enroll(client: ApiClient, agent_config: cfg.AgentConfig) -> cfg.AgentState:
    """Effectue l'appel d'enrôlement et persiste l'état obtenu."""
    machine_id = cfg.get_machine_id()
    hostname = cfg.get_hostname()
    os_version = cfg.get_os_version()

    _logger.info(
        "Enrôlement en cours : hostname=%s, machine_id=%s, os=%s",
        hostname, machine_id, os_version,
    )

    result = client.enroll(
        enrollment_token=agent_config.enrollment_token,
        machine_id=machine_id,
        hostname=hostname,
        os_version=os_version,
        agent_version=__version__,
    )

    if not result.ok:
        if result.status_code == 401:
            raise EnrollmentError(
                "Token d'enrôlement refusé par le serveur (HTTP 401). "
                "Vérifier 'enrollment_token' dans config.ini."
            )
        raise EnrollmentError(f"Enrôlement échoué : {result.error}")

    data = result.data or {}
    agent_id = data.get("agent_id")
    agent_token = data.get("agent_token")

    if not agent_id or not agent_token:
        raise EnrollmentError(
            f"Réponse d'enrôlement incomplète (agent_id/agent_token manquant) : {data}"
        )

    state = cfg.AgentState(agent_id=agent_id, agent_token=agent_token)
    cfg.save_state(state)
    client.set_credentials(agent_id, agent_token)

    _logger.info("Enrôlement réussi : agent_id=%s", agent_id)
    return state


class EnrollmentError(Exception):
    """Erreur lors de l'enrôlement de l'agent."""
