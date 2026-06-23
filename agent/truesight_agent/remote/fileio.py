"""Transfert de fichiers pendant une session de bureau à distance.

Réutilise le canal de la session distante (relais pass-through) : aucun nouveau
port, aucune route serveur. Le DOWNLOAD (agent → viewer) part en trames binaires
(type 0x20, cf. ``remote/__init__.py``) ; l'UPLOAD (viewer → agent) arrive en JSON
base64 (le viewer n'émet que du texte vers l'agent).

Contexte de privilèges : ce code s'exécute dans le MÊME processus que la session
(le compagnon en session utilisateur), donc les accès disque ont les droits de
l'utilisateur connecté — modèle RDP, pas SYSTEM. Le système de fichiers (ACL
Windows) reste donc le garde-fou réel ; on n'ajoute pas de liste blanche de
chemins (un explorateur de type Ninja navigue librement, borné par les droits du
compte). On valide tout de même la forme des chemins (absolus, normalisés) et on
borne la taille.

Tolérant : toute opération renvoie un code d'erreur plutôt que de lever, pour ne
jamais faire tomber la session de bureau à distance.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import time

from . import (
    FILE_CHUNK_FLAG_LAST,
    FILE_CHUNK_HEADER_SIZE,  # noqa: F401  (documenté ; l'en-tête est construit ici)
    FILE_CHUNK_SIZE,
    MAX_FILE_BYTES,
    MSG_TYPE_FILE_CHUNK,
    PROTOCOL_VERSION,
)

_logger = logging.getLogger("truesight.remote.fileio")

# Nombre maximum d'entrées renvoyées pour un listage de dossier (borne le JSON).
_MAX_LIST_ENTRIES = 2000


def build_file_chunk_frame(transfer_id: int, seq: int, payload: bytes, last: bool) -> bytes:
    """Assemble l'en-tête 11 octets (type 0x20) + octets bruts du chunk."""
    flags = FILE_CHUNK_FLAG_LAST if last else 0
    header = struct.pack(
        "<BBIIB",
        PROTOCOL_VERSION, MSG_TYPE_FILE_CHUNK,
        int(transfer_id) & 0xFFFFFFFF, int(seq) & 0xFFFFFFFF, flags,
    )
    return header + payload


def normalize_path(path) -> str | None:
    """Normalise un chemin reçu du viewer (séparateurs '/' tolérés) en chemin
    absolu Windows. Renvoie None si le chemin n'est pas exploitable."""
    if not isinstance(path, str):
        return None
    p = path.strip().replace("/", os.sep)
    if not p:
        return None
    # On exige un chemin absolu (lettre de lecteur ou UNC) ; pas de relatif.
    p = os.path.normpath(p)
    if not os.path.isabs(p):
        return None
    try:
        # realpath résout les .. résiduels et les liens : la cible réelle est ce
        # qui compte pour les contrôles d'accès qui suivent.
        return os.path.realpath(p)
    except OSError:
        return p


def safe_basename(name) -> str | None:
    """Réduit un nom de fichier d'upload à son seul basename, sans séparateur ni
    caractère de contrôle. Empêche toute traversée via le nom. None si invalide."""
    if not isinstance(name, str):
        return None
    base = os.path.basename(name.strip().replace("/", os.sep))
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip()
    # Caractères interdits dans un nom de fichier Windows.
    if not base or base in (".", "..") or re.search(r'[<>:"/\\|?*]', base):
        return None
    return base[:255]


def list_roots() -> list[dict]:
    """Emplacements de départ ergonomiques : dossiers du profil + lecteurs fixes."""
    roots: list[dict] = []
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if profile and os.path.isdir(profile):
        roots.append({"label": "Profil", "path": profile})
        for label, sub in (("Bureau", "Desktop"), ("Documents", "Documents"),
                           ("Téléchargements", "Downloads")):
            cand = os.path.join(profile, sub)
            if os.path.isdir(cand):
                roots.append({"label": label, "path": cand})
    # Lecteurs présents (A: → Z:).
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            roots.append({"label": drive, "path": drive})
    return roots


def list_dir(path: str) -> dict:
    """Liste un dossier. Renvoie ``{path, parent, entries}`` ou ``{error: code}``.

    ``entries`` = ``[{name, is_dir, size, mtime}]`` (dossiers d'abord, puis tri
    alphabétique). ``parent`` = dossier parent (pour « remonter »), None à la racine.
    """
    real = normalize_path(path)
    if real is None or not os.path.isdir(real):
        return {"error": "not_found"}
    try:
        entries = []
        with os.scandir(real) as it:
            for entry in it:
                if len(entries) >= _MAX_LIST_ENTRIES:
                    break
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    st = entry.stat(follow_symlinks=False)
                    entries.append({
                        "name": entry.name,
                        "is_dir": bool(is_dir),
                        "size": 0 if is_dir else int(st.st_size),
                        "mtime": _iso(st.st_mtime),
                    })
                except OSError:
                    continue
    except PermissionError:
        return {"error": "denied"}
    except OSError as exc:
        _logger.info("Listage impossible (%s) : %s", real, exc)
        return {"error": "io"}

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    parent = os.path.dirname(real)
    if parent == real:  # racine d'un lecteur : pas de parent
        parent = None
    return {"path": real, "parent": parent, "entries": entries}


def open_download(path: str):
    """Ouvre un fichier pour le download. Renvoie ``(fh, name, size, None)`` ou
    ``(None, None, 0, code_erreur)``."""
    real = normalize_path(path)
    if real is None:
        return None, None, 0, "bad_path"
    if not os.path.isfile(real):
        return None, None, 0, "not_found"
    try:
        size = os.path.getsize(real)
    except OSError:
        return None, None, 0, "io"
    if size > MAX_FILE_BYTES:
        return None, None, 0, "too_big"
    try:
        fh = open(real, "rb")
    except PermissionError:
        return None, None, 0, "denied"
    except OSError:
        return None, None, 0, "io"
    return fh, os.path.basename(real), size, None


def read_chunks(fh):
    """Itère le contenu d'un fichier par blocs de ``FILE_CHUNK_SIZE`` octets."""
    while True:
        block = fh.read(FILE_CHUNK_SIZE)
        if not block:
            return
        yield block


def open_upload(dest_dir: str, name: str, size):
    """Prépare un upload : valide le dossier + le nom + la taille, ouvre un fichier
    temporaire ``.partial`` DANS le dossier cible (rename atomique ensuite).

    Renvoie ``(fh, tmp_path, final_path, None)`` ou ``(None, None, None, code)``.
    """
    real_dir = normalize_path(dest_dir)
    if real_dir is None or not os.path.isdir(real_dir):
        return None, None, None, "bad_path"
    if not os.access(real_dir, os.W_OK):
        return None, None, None, "denied"
    base = safe_basename(name)
    if base is None:
        return None, None, None, "bad_path"
    try:
        if size is not None and int(size) > MAX_FILE_BYTES:
            return None, None, None, "too_big"
    except (TypeError, ValueError):
        pass
    final_path = _unique_path(os.path.join(real_dir, base))
    tmp_path = final_path + ".tspart"
    try:
        fh = open(tmp_path, "wb")
    except PermissionError:
        return None, None, None, "denied"
    except OSError:
        return None, None, None, "io"
    return fh, tmp_path, final_path, None


def finalize_upload(tmp_path: str, final_path: str) -> bool:
    """Renomme atomiquement le ``.tspart`` vers sa destination finale."""
    try:
        os.replace(tmp_path, final_path)
        return True
    except OSError as exc:
        _logger.info("Finalisation de l'upload impossible : %s", exc)
        _cleanup(tmp_path)
        return False


def _cleanup(tmp_path: str) -> None:
    """Supprime un fichier temporaire d'upload (best effort)."""
    try:
        if tmp_path and os.path.isfile(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass


def _unique_path(path: str) -> str:
    """Évite l'écrasement : ``fichier.txt`` → ``fichier (1).txt`` si déjà présent."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    for i in range(1, 1000):
        cand = f"{root} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
    return path  # dernier recours : on écrasera (très improbable)


def _iso(epoch: float) -> str:
    """Horodatage local ISO court (YYYY-MM-DD HH:MM) pour l'affichage."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except (OSError, ValueError):
        return ""
