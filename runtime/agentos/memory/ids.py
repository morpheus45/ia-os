"""Identifiants ULID : triables par date, uniques sans coordination.

Deux machines qui écrivent hors ligne doivent produire des identifiants
qui ne collisionnent pas et qui restent ordonnés par date une fois la
mémoire fusionnée. Un entier auto-incrémenté ne peut faire ni l'un ni
l'autre ; un UUID4 fait le premier mais pas le second.
"""

from __future__ import annotations

import os
import time

#: Crockford base32 : ni I, L, O, U — pas de confusion à la relecture.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_LEN = 10  # 48 bits d'horodatage en millisecondes
_RAND_LEN = 16  # 80 bits d'aléa

_last_ms = 0
_last_rand = 0


def _encode(value: int, length: int) -> str:
    out = bytearray(length)
    for i in range(length - 1, -1, -1):
        out[i] = ord(_ALPHABET[value & 0x1F])
        value >>= 5
    return out.decode("ascii")


def ulid(now_ms: int | None = None) -> str:
    """Renvoie un ULID de 26 caractères, strictement croissant.

    Deux appels dans la même milliseconde incrémentent la partie aléatoire
    au lieu de la retirer, ce qui garde l'ordre lexicographique aligné sur
    l'ordre d'écriture — sans quoi deux épisodes de la même milliseconde
    pourraient ressortir dans le désordre.
    """
    global _last_ms, _last_rand

    ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if ms == _last_ms:
        _last_rand += 1
        if _last_rand >= 1 << 80:  # débordement : on attend la ms suivante
            ms += 1
            _last_ms = ms
            _last_rand = int.from_bytes(os.urandom(10), "big")
    else:
        if ms < _last_ms:
            # Horloge reculée (NTP, reprise d'hibernation) : on ne régresse pas.
            ms = _last_ms
            _last_rand += 1
        else:
            _last_ms = ms
            _last_rand = int.from_bytes(os.urandom(10), "big")

    return _encode(ms & ((1 << 48) - 1), _TIME_LEN) + _encode(_last_rand & ((1 << 80) - 1), _RAND_LEN)


def timestamp_ms(value: str) -> int:
    """Extrait l'horodatage d'un ULID, en millisecondes epoch."""
    if len(value) != _TIME_LEN + _RAND_LEN:
        raise ValueError(f"ULID invalide : {value!r}")
    out = 0
    for char in value[:_TIME_LEN]:
        index = _ALPHABET.find(char.upper())
        if index < 0:
            raise ValueError(f"ULID invalide : {value!r}")
        out = (out << 5) | index
    return out
