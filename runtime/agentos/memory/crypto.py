"""Chiffrement de la mémoire avant envoi.

Le chiffrement s'appuie sur `cryptography` (paquet Debian
`python3-cryptography`). Il n'est délibérément pas réimplémenté ici :
écrire soi-même un chiffrement authentifié est le genre d'erreur qui ne
se voit pas — le résultat a l'air de fonctionner et ne protège rien.

Conséquence assumée : si la bibliothèque manque alors que le chiffrement
est demandé, la synchro refuse de démarrer. Elle ne se rabat pas sur du
clair. Une machine qui envoie sa mémoire en clair alors que l'opérateur
a demandé le contraire est un problème pire qu'une synchro à l'arrêt.
"""

from __future__ import annotations

import base64
import hashlib
import os

MAGIC = b"aos1"          # préfixe de version du format
NONCE_BYTES = 12         # taille imposée par AES-GCM
SALT = b"agent-os/remote-memory/v1"

#: Paramètres scrypt. n=2^15 et r=8 demandent 128·n·r = 32 Mio, soit très
#: exactement la limite par défaut d'OpenSSL — qui refuse alors la
#: dérivation. Il faut donc relever `maxmem` explicitement, sans quoi le
#: chiffrement échoue au premier démarrage.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2


class CryptoUnavailable(RuntimeError):
    """La bibliothèque de chiffrement manque alors qu'elle est requise."""


def _aesgcm(key: bytes):
    # On attrape toute exception, pas seulement ImportError : une
    # installation dont les liaisons natives manquent laisse
    # « import cryptography » réussir puis échoue à l'import des primitives,
    # parfois par une panique du binding Rust plutôt que par une ImportError.
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as exc:  # noqa: BLE001 - installation cassée, pas seulement absente
        raise CryptoUnavailable(
            "chiffrement demandé mais « cryptography » est inutilisable "
            f"({type(exc).__name__}: {exc}) — installer python3-cryptography "
            "et python3-cffi, ou mettre remote.encrypt = false en sachant que "
            "la mémoire partira alors en clair"
        ) from exc
    return AESGCM(key)


def derive_key(secret: str) -> bytes:
    """Dérive une clé de 32 octets depuis la phrase secrète de l'opérateur.

    scrypt plutôt qu'un simple hachage : la clé est souvent une phrase
    choisie par un humain, donc devinable. Le coût de dérivation est ce qui
    rend une attaque par dictionnaire non rentable.
    """
    if not secret:
        raise ValueError("AGENTOS_REMOTE_KEY est vide")
    return hashlib.scrypt(
        secret.encode("utf-8"), salt=SALT,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32,
    )


def encrypt(plaintext: str, key: bytes) -> str:
    """Chiffre et renvoie une chaîne transportable en JSON."""
    nonce = os.urandom(NONCE_BYTES)
    sealed = _aesgcm(key).encrypt(nonce, plaintext.encode("utf-8"), MAGIC)
    return base64.b64encode(MAGIC + nonce + sealed).decode("ascii")


def decrypt(payload: str, key: bytes) -> str:
    """Déchiffre une valeur produite par `encrypt`."""
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("charge chiffrée illisible") from exc
    if not raw.startswith(MAGIC):
        raise ValueError("format inconnu : cette valeur n'a pas été chiffrée par agent-os")
    nonce = raw[len(MAGIC):len(MAGIC) + NONCE_BYTES]
    sealed = raw[len(MAGIC) + NONCE_BYTES:]
    return _aesgcm(key).decrypt(nonce, sealed, MAGIC).decode("utf-8")


def available() -> bool:
    """Vrai si le chiffrement peut réellement s'exécuter.

    La sonde va jusqu'à un aller-retour complet, sans se contenter d'un
    import du paquet racine : une installation aux liaisons natives
    incomplètes passe ce premier test puis échoue au premier chiffrement,
    c'est-à-dire une fois la synchro déjà partie.
    """
    try:
        probe = os.urandom(32)
        nonce = os.urandom(NONCE_BYTES)
        cipher = _aesgcm(probe)
        return cipher.decrypt(nonce, cipher.encrypt(nonce, b"probe", MAGIC), MAGIC) == b"probe"
    except Exception:  # noqa: BLE001
        return False
