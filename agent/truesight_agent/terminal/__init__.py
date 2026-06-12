"""Module « Terminal interactif » de l'agent TrueSight.

Ce paquet ajoute un **shell interactif** (PowerShell / cmd) accessible depuis le
navigateur, façon « SSH-dans-le-navigateur ». Il réutilise le **même relais
WebSocket** que le bureau à distance (``/ws/remote/agent`` côté agent), mais
parle un protocole **texte JSON** dans les deux sens (pas de trames binaires).

Différence majeure avec le module ``remote/`` :
  - Le bureau à distance doit capturer/injecter dans la session interactive de
    l'utilisateur ⇒ il lance un *helper* en session active (CreateProcessAsUser).
  - Le terminal n'a **aucune** contrainte de session 0 : un shell tourne très
    bien dans le process de l'agent (en SYSTEM, c'est même un shell admin). Il
    s'exécute donc **inline**, dans un simple thread, sans helper.

Le PTY est fourni par ``pywinpty`` (ConPTY). Tout le module est *jamais-crash* :
une erreur de session terminal ne doit jamais faire tomber l'agent.
"""

from __future__ import annotations

__all__ = ["session"]
