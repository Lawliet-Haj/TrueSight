"""Calcul de l'état de santé d'un poste (Sain / Attention / Défectueux / Inconnu).

La santé est dérivée de signaux déjà disponibles, pour rester cohérente avec la
page Alertes et éviter de dupliquer la logique de seuils :

- **présence** : un poste hors ligne (pas de heartbeat récent) est ``unknown`` ;
- **alertes actives** (non résolues) : ``disk_low`` / ``offline`` → ``critical``,
  ``cpu_high`` / ``ram_high`` → ``warning`` ;
- **métriques live** (dernier heartbeat) : CPU/RAM au-dessus du seuil → ``warning``
  même si l'alerte persistée n'a pas encore été matérialisée par le thread de fond.

Statuts (ordre de gravité) : healthy < warning < critical, et unknown (hors ligne).
"""
from datetime import timezone

from .models import utcnow

# Libellés FR des catégories de problème (types de règles d'alerte).
PROBLEM_LABELS = {
    "offline": "Hors ligne",
    "disk_low": "Disque faible",
    "cpu_high": "CPU élevé",
    "ram_high": "RAM élevée",
}

# Catégories considérées critiques pour la santé du poste.
_CRITICAL_TYPES = {"disk_low", "offline"}

# Libellés courts pour les « raisons » de santé.
_REASON_LABELS = {
    "offline": "hors ligne",
    "disk_low": "disque faible",
    "cpu_high": "CPU élevé",
    "ram_high": "RAM élevée",
}

_SEVERITY_RANK = {"healthy": 0, "warning": 1, "critical": 2, "unknown": 3}


def is_online(agent, threshold_seconds: int) -> bool:
    """Détermine si un agent est en ligne selon sa dernière activité."""
    if agent.last_seen_at is None:
        return False
    last = agent.last_seen_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (utcnow() - last).total_seconds() < threshold_seconds


def _num(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def agent_health(agent, metric, active_alert_types, cfg, security=None) -> tuple[str, list]:
    """Renvoie ``(status, reasons)`` pour un poste.

    - ``metric`` : dernier point de métriques (ou None) ;
    - ``active_alert_types`` : ensemble des types d'alertes actives du poste ;
    - ``cfg`` : configuration de l'app (seuils) ;
    - ``security`` : dict ``{defender, windows_update}`` (ou None).
    """
    threshold = cfg.get("OFFLINE_THRESHOLD_SECONDS", 300)
    if not is_online(agent, threshold):
        return "unknown", ["hors ligne"]

    status = "healthy"
    reasons: list[str] = []

    for t in sorted(active_alert_types or []):
        if t == "offline":
            # Une alerte 'offline' active alors que le poste répond est obsolète :
            # on l'ignore pour la santé live (la présence prime).
            continue
        status = _escalate(status, "critical" if t in _CRITICAL_TYPES else "warning")
        reasons.append(_REASON_LABELS.get(t, t))

    # Signaux live (dernier heartbeat) : CPU / RAM.
    if metric is not None:
        cpu = _num(metric.cpu_pct)
        ram = _num(metric.ram_used_pct)
        if cpu is not None and cpu >= cfg.get("ALERT_CPU_HIGH_PCT", 90):
            status = _escalate(status, "warning")
            reasons.append(f"CPU {cpu:.0f}%")
        if ram is not None and ram >= cfg.get("ALERT_RAM_HIGH_PCT", 90):
            status = _escalate(status, "warning")
            reasons.append(f"RAM {ram:.0f}%")

    # Signaux sécurité (Defender + MAJ Windows en attente).
    if security:
        defender = security.get("defender") or {}
        if defender:
            if defender.get("enabled") is False:
                status = _escalate(status, "warning")
                reasons.append("antivirus désactivé")
            elif defender.get("realtime") is False:
                status = _escalate(status, "warning")
                reasons.append("protection temps réel désactivée")
        wu = security.get("windows_update") or {}
        crit = wu.get("pending_critical")
        if isinstance(crit, int) and crit > 0:
            status = _escalate(status, "warning")
            reasons.append(f"{crit} MAJ critique" + ("s" if crit > 1 else ""))

    # Dédoublonne les raisons en conservant l'ordre.
    seen = set()
    deduped = [r for r in reasons if not (r in seen or seen.add(r))]
    return status, deduped


def _escalate(current: str, candidate: str) -> str:
    """Garde le statut le plus grave entre ``current`` et ``candidate``."""
    return candidate if _SEVERITY_RANK[candidate] > _SEVERITY_RANK[current] else current
