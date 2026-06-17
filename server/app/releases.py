"""Helpers de gestion des paquets d'agent (auto-update + lien d'installation).

Le binaire de l'agent (dossier onedir PyInstaller zippé) est stocké sur le
disque, dans ``AGENT_RELEASE_DIR`` (volume Docker persistant) ; la table
``AgentRelease`` n'en garde que les métadonnées. Ce module centralise :

- la résolution du répertoire de stockage et du chemin d'un paquet ;
- le calcul d'empreinte SHA-256 ;
- la comparaison de versions sémantiques simples (``MAJOR.MINOR.PATCH``) pour
  décider si un agent doit se mettre à jour ;
- la lecture de la version embarquée dans un zip (``version.txt`` à la racine).
"""
from __future__ import annotations

import hashlib
import os
import re
import zipfile

from flask import current_app

from .extensions import db
from .models import AgentRelease

_VERSION_RE = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)")


def parse_version(value) -> tuple[int, int, int] | None:
    """Analyse une version ``MAJOR.MINOR.PATCH`` en tuple, ou None si illisible."""
    if not value:
        return None
    m = _VERSION_RE.match(str(value))
    if not m:
        return None
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def version_gt(release_version, agent_version) -> bool:
    """True si ``release_version`` doit remplacer ``agent_version`` sur l'agent.

    - version de release illisible → False (on n'annonce pas une release douteuse) ;
    - version d'agent illisible/absente → True (montée vers une version connue) ;
    - sinon : comparaison de tuples (strictement supérieure).
    """
    pr = parse_version(release_version)
    if pr is None:
        return False
    pa = parse_version(agent_version)
    if pa is None:
        return True
    return pr > pa


def release_dir() -> str:
    """Répertoire de stockage des paquets (créé au besoin)."""
    d = current_app.config.get("AGENT_RELEASE_DIR") or "/var/lib/truesight/releases"
    os.makedirs(d, exist_ok=True)
    return d


def release_path(release: AgentRelease) -> str:
    """Chemin absolu du fichier zip d'une release."""
    return os.path.join(release_dir(), release.filename)


def current_release() -> AgentRelease | None:
    """Release marquée ``is_current`` (la plus récente si plusieurs), ou None."""
    return (
        db.session.query(AgentRelease)
        .filter_by(is_current=True)
        .order_by(AgentRelease.published_at.desc())
        .first()
    )


def current_release_available() -> AgentRelease | None:
    """Release courante dont le fichier existe réellement sur le disque, sinon None."""
    rel = current_release()
    if rel is None:
        return None
    if not os.path.isfile(release_path(rel)):
        return None
    return rel


def sha256_file(path: str) -> str:
    """Empreinte SHA-256 hexadécimale d'un fichier (lecture par blocs de 1 Mo)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_zip_version(path: str) -> str | None:
    """Lit la version embarquée dans le zip (``version.txt`` à la racine).

    ``build.ps1`` écrit ce fichier dans le dossier onedir avant compression. On
    cherche un membre ``version.txt`` (à la racine ou dans le dossier onedir) et
    on renvoie la première ligne non vide normalisée. None si introuvable/illisible.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            candidates = [
                n for n in zf.namelist()
                if os.path.basename(n).lower() == "version.txt"
            ]
            # On privilégie le chemin le plus court (racine du paquet).
            candidates.sort(key=len)
            for name in candidates:
                with zf.open(name) as fh:
                    text = fh.read().decode("utf-8-sig", errors="ignore").strip()
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        return line
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    return None
