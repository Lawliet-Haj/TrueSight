"""Copilote IA de TrueSight.

Couche d'assistance par-dessus la donnée du parc et le pipeline de commandes :
le modèle lit la télémétrie via des outils en LECTURE SEULE et PROPOSE des actions
(jamais exécutées directement) que l'administrateur confirme — la confirmation
repasse par les endpoints existants déjà audités.

Point d'entrée : ``run_chat_turn(message, agent_id, history)``.
"""
from .client import is_configured
from .loop import run_chat_turn

__all__ = ["run_chat_turn", "is_configured"]
