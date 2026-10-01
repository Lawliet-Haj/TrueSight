"""Codes de secours de la double authentification (cf. ``tests/test_mfa_recovery.py``).

Le 29/09/2026, le seul superadmin a perdu son téléphone : plus de code, donc
plus d'accès, et aucun chemin de récupération. Les codes de secours comblent ce
trou : dix codes à usage unique, remis UNE fois (activation ou régénération),
acceptés à l'écran de connexion à la place du code du téléphone.

Stockage : seules les EMPREINTES SHA-256 sont conservées, comme les jetons
d'invitation. Un hachage lent n'ajouterait rien ici : le secret TOTP lui-même est
en base, donc qui lit la base contourne déjà la double authentification. La
défense tient à la longueur des codes (10 caractères sur 32, soit 50 bits) et à
la limite de tentatives de l'écran de connexion.

Alphabet de Crockford (sans I, L, O, U) : rien à confondre en recopiant un code
à la main. À la saisie, O vaut 0 et I ou L valent 1, casse et tirets ignorés.
"""
import hashlib
import secrets

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
COUNT = 10
LENGTH = 10
_TRANSLATE = str.maketrans({"O": "0", "I": "1", "L": "1"})


def normalize(code: str) -> str:
    """Forme canonique d'un code saisi : majuscules, sans espace ni tiret."""
    return (code or "").upper().replace("-", "").replace(" ", "").strip().translate(_TRANSLATE)


def is_totp_format(code: str) -> bool:
    """Un code du téléphone : exactement six chiffres (espaces tolérés)."""
    brut = (code or "").replace(" ", "").strip()
    return len(brut) == 6 and brut.isdigit()


def _hash(code: str) -> str:
    return hashlib.sha256(normalize(code).encode("ascii", "ignore")).hexdigest()


def generate() -> tuple[list[str], list[str]]:
    """Tire ``COUNT`` codes neufs ; renvoie (codes à montrer, empreintes à stocker)."""
    codes = []
    while len(codes) < COUNT:
        brut = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
        code = brut[:5] + "-" + brut[5:]
        if code not in codes:
            codes.append(code)
    return codes, [_hash(c) for c in codes]


def remaining(user) -> int:
    return len(user.mfa_recovery_codes or [])


def consume(user, code: str) -> bool:
    """Consomme ``code`` s'il fait partie des codes restants de ``user``.

    Le code utilisé est RETIRÉ : il ne rouvrira plus rien. On affecte une liste
    neuve (pas de modification sur place) pour que la colonne JSON soit bien
    marquée comme modifiée. L'appelant valide la transaction.
    """
    empreintes = list(user.mfa_recovery_codes or [])
    cible = _hash(code)
    trouve = next((e for e in empreintes if secrets.compare_digest(e, cible)), None)
    if trouve is None:
        return False
    empreintes.remove(trouve)
    user.mfa_recovery_codes = empreintes
    return True
