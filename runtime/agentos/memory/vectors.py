"""Index vectoriel : quantification int8 et recherche par produit scalaire.

Le compromis est dicté par la machine cible — 16 Go de RAM partagés avec
un modèle local. En float32, 768 dimensions coûtent 3 Ko par souvenir :
200 000 souvenirs occupent 600 Mo qu'on ne peut pas se permettre. Quantifiés
en int8 après normalisation, ils tiennent dans 150 Mo, et l'erreur
introduite reste sous le bruit du modèle d'embedding lui-même.

Les vecteurs étant normalisés avant quantification, le produit scalaire
est directement le cosinus : aucune division au moment de la requête.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from array import array
from dataclasses import dataclass
from typing import Iterable, Sequence

try:  # chemin rapide, présent dans l'image (paquet python3-numpy)
    import numpy as _np
except ImportError:  # pragma: no cover - dépend de l'environnement
    _np = None

#: Au-delà, le scan linéaire en Python pur devient plus lent que la seconde.
#: Sans numpy on refuse d'indexer davantage plutôt que de rendre la
#: recherche inutilisable en silence.
PURE_PYTHON_LIMIT = 20_000


def normalize(vector: Sequence[float]) -> list[float]:
    """Ramène un vecteur à la norme 1. Un vecteur nul est renvoyé tel quel."""
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return list(vector)
    return [component / norm for component in vector]


def quantize(vector: Sequence[float]) -> tuple[float, bytes]:
    """Normalise puis quantifie en int8 symétrique.

    Renvoie l'échelle et les octets. Déquantifier, c'est multiplier par
    l'échelle — d'où un produit scalaire calculable en entiers puis
    corrigé une seule fois à la fin.
    """
    unit = normalize(vector)
    peak = max((abs(component) for component in unit), default=0.0)
    if peak == 0.0:
        return 1.0, bytes(len(unit))
    scale = peak / 127.0
    packed = array("b", (max(-127, min(127, round(component / scale))) for component in unit))
    return scale, packed.tobytes()


def dequantize(scale: float, data: bytes) -> list[float]:
    return [value * scale for value in array("b", data)]


@dataclass(slots=True)
class Hit:
    uid: str
    score: float


class VectorIndex:
    """Index en mémoire, adossé à la table `vectors` pour la persistance.

    L'index est borné : au-delà de `capacity`, les entrées les plus
    anciennement mises à jour sont évincées de la RAM. Elles restent en
    base et restent atteignables par la recherche plein texte, donc rien
    n'est perdu — seule la voie sémantique se restreint aux souvenirs
    récents, ce qui est le bon arbitrage sur une machine contrainte.
    """

    def __init__(self, dim: int, capacity: int = 200_000) -> None:
        self.dim = dim
        self.capacity = capacity
        self._lock = threading.RLock()
        self._uids: list[str] = []
        self._scopes: list[str] = []
        self._scales: list[float] = []
        self._updated: list[float] = []
        self._rows: list[bytes] = []
        self._position: dict[str, int] = {}
        self._matrix = None  # cache numpy, invalidé à chaque écriture

    def __len__(self) -> int:
        return len(self._uids)

    # -- écriture --------------------------------------------------------

    def add(self, uid: str, scope: str, scale: float, data: bytes, updated: float) -> None:
        if len(data) != self.dim:
            raise ValueError(f"vecteur de dimension {len(data)}, index en dimension {self.dim}")
        with self._lock:
            index = self._position.get(uid)
            if index is None:
                if _np is None and len(self._uids) >= PURE_PYTHON_LIMIT:
                    raise RuntimeError(
                        f"index vectoriel plafonné à {PURE_PYTHON_LIMIT} entrées sans numpy ; "
                        "installer python3-numpy"
                    )
                index = len(self._uids)
                self._uids.append(uid)
                self._scopes.append(scope)
                self._scales.append(scale)
                self._updated.append(updated)
                self._rows.append(data)
                self._position[uid] = index
            else:
                self._scopes[index] = scope
                self._scales[index] = scale
                self._updated[index] = updated
                self._rows[index] = data
            self._matrix = None
        if len(self._uids) > self.capacity:
            self._evict()

    def remove(self, uid: str) -> bool:
        """Retire une entrée en échangeant avec la dernière (O(1))."""
        with self._lock:
            index = self._position.pop(uid, None)
            if index is None:
                return False
            last = len(self._uids) - 1
            if index != last:
                for store in (self._uids, self._scopes, self._scales, self._updated, self._rows):
                    store[index] = store[last]
                self._position[self._uids[index]] = index
            for store in (self._uids, self._scopes, self._scales, self._updated, self._rows):
                store.pop()
            self._matrix = None
            return True

    def _evict(self) -> None:
        """Ramène l'index à sa capacité en retirant les plus anciens."""
        with self._lock:
            excess = len(self._uids) - self.capacity
            if excess <= 0:
                return
            order = sorted(range(len(self._updated)), key=self._updated.__getitem__)
            for index in order[:excess]:
                self.remove(self._uids[index])

    def load(self, rows: Iterable[tuple[str, str, float, bytes, float]]) -> int:
        """Charge l'index depuis la base. Les lignes doivent venir triées
        par `updated` décroissant : la troncature garde alors les plus récentes."""
        count = 0
        for uid, scope, scale, data, updated in rows:
            if len(self._uids) >= self.capacity:
                break
            if _np is None and len(self._uids) >= PURE_PYTHON_LIMIT:
                break
            try:
                self.add(uid, scope, scale, data, updated)
            except ValueError:
                continue  # dimension obsolète : ignorée, sera réindexée
            count += 1
        return count

    # -- lecture ---------------------------------------------------------

    def _numpy_matrix(self):
        if self._matrix is None or len(self._matrix) != len(self._uids):
            if not self._uids:
                self._matrix = _np.zeros((0, self.dim), dtype=_np.int8)
            else:
                self._matrix = _np.frombuffer(b"".join(self._rows), dtype=_np.int8).reshape(
                    len(self._uids), self.dim
                )
        return self._matrix

    def search(
        self,
        query: Sequence[float],
        limit: int = 10,
        *,
        scope: str | None = None,
        restrict: set[str] | None = None,
        min_score: float | None = None,
    ) -> list[Hit]:
        """Renvoie les entrées les plus proches, cosinus décroissant.

        `min_score` écarte les voisins trop éloignés. Sans lui, la recherche
        rend toujours `limit` résultats quelle que soit la requête, ce qui
        n'est pas une recherche mais un tirage.
        """
        # La dimension est vérifiée avant tout court-circuit : sur un index
        # encore vide, renvoyer une liste vide masquerait une erreur de
        # configuration qui n'apparaîtrait qu'une fois la mémoire remplie.
        unit = normalize(query)
        if len(unit) != self.dim:
            raise ValueError(f"requête de dimension {len(unit)}, index en dimension {self.dim}")

        with self._lock:
            if not self._uids:
                return []

            if _np is not None:
                scores = self._numpy_matrix() @ _np.asarray(unit, dtype=_np.float32)
                scores = scores * _np.asarray(self._scales, dtype=_np.float32)
                candidates = enumerate(scores.tolist())
            else:
                candidates = self._pure_python_scores(unit)

            hits = [
                Hit(self._uids[index], float(score))
                for index, score in candidates
                if (scope is None or self._scopes[index] == scope)
                and (restrict is None or self._uids[index] in restrict)
                and (min_score is None or score >= min_score)
            ]
            hits.sort(key=lambda hit: hit.score, reverse=True)
            return hits[:limit]

    def _pure_python_scores(self, unit: Sequence[float]):
        for index, data in enumerate(self._rows):
            row = array("b", data)
            total = 0.0
            for component, value in zip(unit, row):
                total += component * value
            yield index, total * self._scales[index]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "entries": len(self._uids),
                "dim": self.dim,
                "capacity": self.capacity,
                "bytes": len(self._uids) * self.dim,
                "backend": "numpy" if _np is not None else "python",
            }


def pack_floats(vector: Sequence[float]) -> bytes:
    """Sérialise un vecteur float32 — utilisé par la synchro distante."""
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_floats(data: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(data) // 4}f", data))
