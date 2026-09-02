"""File de travaux persistée en base.

Persistée et non en mémoire : une coupure de courant ne doit pas effacer
ce que la machine avait à faire. Les états vivent donc dans SQLite, et la
reprise après redémarrage consiste à récupérer les travaux restés en
cours — sans quoi ils resteraient invisibles et bloqués pour toujours.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from ..memory.ids import ulid

log = logging.getLogger("agentos.queue")

PENDING, RUNNING, DONE, DEAD = "pending", "running", "done", "dead"


@dataclass(slots=True)
class Job:
    uid: str
    name: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    origin: str
    created: float

    @property
    def last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


class JobQueue:
    """Travaux à exécuter, avec reprises et backoff."""

    def __init__(
        self,
        store,
        *,
        max_attempts: int = 5,
        backoff_base: float = 5.0,
        backoff_cap: float = 900.0,
    ) -> None:
        self.store = store
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap

    # -- écriture --------------------------------------------------------

    def enqueue(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        delay_s: float = 0.0,
        max_attempts: int | None = None,
        origin: str = "manual",
        uid: str | None = None,
    ) -> str:
        job_uid = uid or ulid()
        now = time.time()
        self.store.write(
            "INSERT INTO jobs(uid, name, payload, state, run_after, max_attempts, created, origin) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                job_uid, name, json.dumps(payload or {}, ensure_ascii=False), PENDING,
                now + max(0.0, delay_s), max_attempts or self.max_attempts, now, origin,
            ),
        )
        return job_uid

    def claim(self) -> Job | None:
        """Prend le prochain travail prêt, de façon atomique.

        La sélection et le passage à « running » tiennent dans une seule
        instruction : deux ouvriers qui interrogent la file en même temps ne
        peuvent pas repartir avec le même travail.
        """
        row = self.store.write(
            "UPDATE jobs SET state = ?, started = ?, attempts = attempts + 1 "
            "WHERE id = (SELECT id FROM jobs WHERE state = ? AND run_after <= ? "
            "            ORDER BY run_after, id LIMIT 1) "
            "RETURNING uid, name, payload, attempts, max_attempts, origin, created",
            (RUNNING, time.time(), PENDING, time.time()),
        ).fetchone()
        if row is None:
            return None
        return Job(
            uid=row["uid"], name=row["name"], payload=_loads(row["payload"]),
            attempts=row["attempts"], max_attempts=row["max_attempts"],
            origin=row["origin"], created=row["created"],
        )

    def complete(self, uid: str, result: Any = None) -> None:
        self.store.write(
            "UPDATE jobs SET state = ?, finished = ?, result = ?, error = NULL WHERE uid = ?",
            (DONE, time.time(), _dumps(result), uid),
        )

    def fail(self, uid: str, error: str, *, retry: bool = True) -> bool:
        """Enregistre un échec. Renvoie True si le travail sera réessayé."""
        row = self.store.execute(
            "SELECT attempts, max_attempts FROM jobs WHERE uid = ?", (uid,)
        ).fetchone()
        if row is None:
            return False

        exhausted = row["attempts"] >= row["max_attempts"]
        if not retry or exhausted:
            self.store.write(
                "UPDATE jobs SET state = ?, finished = ?, error = ? WHERE uid = ?",
                (DEAD, time.time(), error[:4000], uid),
            )
            log.error("travail %s abandonné après %d tentatives : %s",
                      uid, row["attempts"], error[:300])
            return False

        delay = self.backoff(row["attempts"])
        self.store.write(
            "UPDATE jobs SET state = ?, run_after = ?, error = ? WHERE uid = ?",
            (PENDING, time.time() + delay, error[:4000], uid),
        )
        log.warning("travail %s en échec (essai %d/%d), nouvelle tentative dans %.0fs : %s",
                    uid, row["attempts"], row["max_attempts"], delay, error[:200])
        return True

    def backoff(self, attempts: int) -> float:
        """Délai exponentiel plafonné, bruité.

        Le bruit désynchronise des travaux repartis ensemble : sans lui, une
        panne commune les ferait tous retenter exactement au même instant, et
        retomber en panne pour la même raison.
        """
        raw = min(self.backoff_cap, self.backoff_base * (2 ** max(0, attempts - 1)))
        return raw * (0.5 + random.random() / 2)

    # -- reprise ---------------------------------------------------------

    def reap_stale(self, timeout_s: float) -> int:
        """Remet en file les travaux abandonnés par un processus disparu.

        Un travail « running » dont personne ne tient plus la trace — arrêt
        brutal, OOM, coupure — ne se terminera jamais tout seul. On le
        considère comme un échec ordinaire, ce qui lui rend le bénéfice des
        tentatives restantes.
        """
        cutoff = time.time() - timeout_s
        rows = self.store.execute(
            "SELECT uid, attempts, max_attempts FROM jobs WHERE state = ? AND started < ?",
            (RUNNING, cutoff),
        ).fetchall()
        for row in rows:
            if row["attempts"] >= row["max_attempts"]:
                self.store.write(
                    "UPDATE jobs SET state = ?, finished = ?, error = ? WHERE uid = ?",
                    (DEAD, time.time(), "interrompu, tentatives épuisées", row["uid"]),
                )
            else:
                self.store.write(
                    "UPDATE jobs SET state = ?, run_after = ?, error = ? WHERE uid = ?",
                    (PENDING, time.time() + self.backoff(row["attempts"]),
                     "interrompu, remis en file", row["uid"]),
                )
        if rows:
            log.warning("%d travaux interrompus repris", len(rows))
        return len(rows)

    # -- lecture ---------------------------------------------------------

    def get(self, uid: str) -> dict[str, Any] | None:
        row = self.store.execute("SELECT * FROM jobs WHERE uid = ?", (uid,)).fetchone()
        return _row_to_dict(row) if row else None

    def list(self, *, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if state:
            rows = self.store.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY created DESC LIMIT ?",
                (state, limit)).fetchall()
        else:
            rows = self.store.execute(
                "SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.store.execute("SELECT state, count(*) AS n FROM jobs GROUP BY state").fetchall()
        return {row["state"]: row["n"] for row in rows}

    def purge(self, older_than_days: float = 30.0) -> int:
        """Efface les travaux terminés anciens. Les morts sont conservés :
        c'est la trace dont on a besoin pour comprendre une panne."""
        cutoff = time.time() - older_than_days * 86400
        cursor = self.store.write(
            "DELETE FROM jobs WHERE state = ? AND finished < ?", (DONE, cutoff))
        return cursor.rowcount


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {"value": value}


def _row_to_dict(row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = _loads(data.get("payload") or "{}")
    return data
