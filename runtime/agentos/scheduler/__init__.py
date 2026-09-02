"""Ordonnanceur : horloge cron, ouvriers, reprise après panne.

Deux mécanismes se complètent. Les *plannings* décrivent une récurrence en
notation cron ; à chaque tour d'horloge, ceux qui sont dus déposent un
*travail* dans la file. Les ouvriers, eux, ne connaissent que la file.
Cette séparation permet à un travail d'être réessayé sans dérégler la
récurrence, et à une tâche ponctuelle d'emprunter exactement le même
chemin d'exécution qu'une tâche périodique.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .cron import Cron, CronError
from .queue import DEAD, DONE, PENDING, RUNNING, Job, JobQueue

log = logging.getLogger("agentos.scheduler")

__all__ = ["Scheduler", "JobQueue", "Job", "Cron", "CronError",
           "PENDING", "RUNNING", "DONE", "DEAD"]

#: Un gestionnaire reçoit le travail et l'échéance au-delà de laquelle il
#: doit renoncer de lui-même.
Handler = Callable[[Job, float], Any]


class Scheduler:
    """Fait tourner les travaux, périodiques comme ponctuels."""

    def __init__(self, store, config, *, queue: JobQueue | None = None) -> None:
        self.store = store
        self.config = config
        self.queue = queue or JobQueue(
            store,
            max_attempts=config.scheduler.max_attempts,
            backoff_base=config.scheduler.backoff_base_s,
            backoff_cap=config.scheduler.backoff_cap_s,
        )
        self.handlers: dict[str, Handler] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._running: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- gestionnaires ---------------------------------------------------

    def register(self, name: str, handler: Handler) -> None:
        self.handlers[name] = handler

    def handler_names(self) -> list[str]:
        return sorted(self.handlers)

    # -- plannings -------------------------------------------------------

    def add_schedule(
        self,
        name: str,
        expression: str,
        job: str,
        payload: dict[str, Any] | None = None,
        *,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Crée ou remplace un planning. L'expression est validée d'emblée.

        Refuser tout de suite une expression fautive vaut mieux que la
        stocker : un planning invalide ne se manifesterait qu'au moment où
        il aurait dû tourner, c'est-à-dire trop tard pour être remarqué.
        """
        import json

        cron = Cron(expression)  # lève CronError si l'expression est fautive
        if job not in self.handlers:
            log.warning("planning « %s » : gestionnaire « %s » inconnu pour l'instant", name, job)
        next_fire = cron.next_timestamp() or 0.0
        self.store.write(
            "INSERT INTO schedules(name, cron, job, payload, enabled, next_fire) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET cron=excluded.cron, job=excluded.job, "
            "payload=excluded.payload, enabled=excluded.enabled, next_fire=excluded.next_fire",
            (name, cron.expression, job, json.dumps(payload or {}, ensure_ascii=False),
             int(enabled), next_fire),
        )
        return self.get_schedule(name) or {}

    def remove_schedule(self, name: str) -> bool:
        return self.store.write("DELETE FROM schedules WHERE name = ?", (name,)).rowcount > 0

    def enable_schedule(self, name: str, enabled: bool) -> bool:
        return self.store.write(
            "UPDATE schedules SET enabled = ? WHERE name = ?", (int(enabled), name)
        ).rowcount > 0

    def get_schedule(self, name: str) -> dict[str, Any] | None:
        row = self.store.execute("SELECT * FROM schedules WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def list_schedules(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store.execute(
            "SELECT * FROM schedules ORDER BY next_fire").fetchall()]

    def fire_due(self, now: float | None = None) -> int:
        """Dépose un travail pour chaque planning arrivé à échéance.

        Un planning dont l'échéance est largement dépassée — machine
        éteinte toute la nuit — se déclenche une fois au retour, puis
        repart sur sa cadence normale. On rattrape donc l'occurrence
        manquée sans rejouer toutes celles de l'absence.
        """
        import json

        now = now if now is not None else time.time()
        fired = 0
        for row in self.store.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_fire <= ? AND next_fire > 0",
            (now,),
        ).fetchall():
            try:
                cron = Cron(row["cron"])
            except CronError as exc:
                log.error("planning « %s » désactivé, expression invalide : %s", row["name"], exc)
                self.store.write("UPDATE schedules SET enabled = 0 WHERE name = ?", (row["name"],))
                continue

            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            payload = {**payload, "_schedule": row["name"]}

            self.queue.enqueue(row["job"], payload, origin=f"planning:{row['name']}")
            self.store.write(
                "UPDATE schedules SET last_fire = ?, next_fire = ? WHERE name = ?",
                (now, cron.next_timestamp(now) or 0.0, row["name"]),
            )
            fired += 1
        return fired

    # -- exécution -------------------------------------------------------

    def run_one(self) -> bool:
        """Exécute un travail si la file en propose un. Renvoie True si oui."""
        job = self.queue.claim()
        if job is None:
            return False

        handler = self.handlers.get(job.name)
        if handler is None:
            self.queue.fail(job.uid, f"gestionnaire « {job.name} » inconnu", retry=False)
            return True

        deadline = time.time() + self.config.scheduler.job_timeout_s
        with self._lock:
            self._running[job.uid] = time.time()
        try:
            result = handler(job, deadline)
        except Exception as exc:  # noqa: BLE001 - toute panne d'un travail est rattrapée
            log.exception("travail %s (%s) en erreur", job.uid, job.name)
            self.queue.fail(job.uid, f"{type(exc).__name__}: {exc}")
        else:
            self.queue.complete(job.uid, result)
        finally:
            with self._lock:
                self._running.pop(job.uid, None)
        return True

    def _worker(self, index: int) -> None:
        idle = self.config.scheduler.tick_s
        while not self._stop.is_set():
            try:
                busy = self.run_one()
            except Exception:  # noqa: BLE001 - un ouvrier ne meurt jamais
                log.exception("ouvrier %d : erreur inattendue", index)
                busy = False
            if not busy:
                self._stop.wait(idle)

    def _ticker(self) -> None:
        # Au démarrage, les travaux laissés « en cours » par l'instance
        # précédente n'ont plus personne pour les terminer.
        self.queue.reap_stale(self.config.scheduler.job_timeout_s)
        last_reap = time.time()

        while not self._stop.is_set():
            try:
                self.fire_due()
                if time.time() - last_reap > 60:
                    self.queue.reap_stale(self.config.scheduler.job_timeout_s)
                    last_reap = time.time()
            except Exception:  # noqa: BLE001
                log.exception("horloge de l'ordonnanceur : erreur inattendue")
            self._stop.wait(self.config.scheduler.tick_s)

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self._threads.append(threading.Thread(target=self._ticker, name="agentos-cron", daemon=True))
        for index in range(max(1, self.config.scheduler.concurrency)):
            self._threads.append(threading.Thread(
                target=self._worker, args=(index,), name=f"agentos-worker-{index}", daemon=True))
        for thread in self._threads:
            thread.start()
        log.info("ordonnanceur démarré : %d ouvriers, %d plannings",
                 self.config.scheduler.concurrency, len(self.list_schedules()))

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    # -- observation -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = dict(self._running)
        schedules = self.list_schedules()
        return {
            "en_marche": bool(self._threads) and not self._stop.is_set(),
            "ouvriers": self.config.scheduler.concurrency,
            "travaux": self.queue.counts(),
            "en_cours": [
                {"uid": uid, "depuis_s": round(time.time() - started, 1)}
                for uid, started in running.items()
            ],
            "plannings": len(schedules),
            "prochain": _next_schedule(schedules),
            "gestionnaires": self.handler_names(),
        }


def _next_schedule(schedules: list[dict[str, Any]]) -> dict[str, Any] | None:
    upcoming = [s for s in schedules if s["enabled"] and s["next_fire"] > 0]
    if not upcoming:
        return None
    soonest = min(upcoming, key=lambda s: s["next_fire"])
    return {
        "nom": soonest["name"],
        "cron": soonest["cron"],
        "quand": datetime.fromtimestamp(soonest["next_fire"]).isoformat(timespec="seconds"),
        "dans_s": max(0, round(soonest["next_fire"] - time.time())),
    }
