"""Préférence IPv4 pour les connexions sortantes de l'agent.

Beaucoup de réseaux de postes ont une **IPv6 cassée** (pas de route, filtrée),
alors que le serveur TrueSight est souvent en **double pile** (DNS A + AAAA, ex.
l'hôte Hostinger ``srv778935.hstgr.cloud``). Conséquence : une tentative de
connexion sur deux part en IPv6 et échoue (prise de main « écran noir », polls qui
time-out), l'autre en IPv4 et réussit.

On force donc la résolution DNS du process à **privilégier l'IPv4** : si un nom a
au moins une adresse IPv4, on ne renvoie que les IPv4 ; sinon (hôte IPv6-only) on
renvoie tout. C'est sûr (l'IPv4 du serveur est joignable partout), idempotent, et
ça ne dépend ni du DNS du serveur ni d'une entrée ``hosts`` par poste.

À appeler le plus tôt possible dans CHAQUE process de l'agent (service, compagnon,
helper, console) — cf. ``__main__.main`` et ``runner.run``.
"""
from __future__ import annotations

import socket

_orig_getaddrinfo = socket.getaddrinfo
_applied = False


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 - signature socket
    """``getaddrinfo`` filtré : ne garde que l'IPv4 quand elle existe."""
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    # Ne filtre que si l'appelant n'a pas déjà imposé une famille (family == 0).
    if family in (0, socket.AF_UNSPEC):
        v4 = [r for r in res if r[0] == socket.AF_INET]
        if v4:
            return v4
    return res


def prefer_ipv4() -> None:
    """Installe la préférence IPv4 pour tout le process (idempotent, jamais fatal)."""
    global _applied
    if _applied:
        return
    try:
        socket.getaddrinfo = _ipv4_first
        _applied = True
    except Exception:  # noqa: BLE001 - ne doit jamais empêcher l'agent de démarrer.
        pass
