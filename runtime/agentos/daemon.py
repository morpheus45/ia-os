"""Assemblage et cycle de vie du runtime.

Ce module tient l'ordre de construction et l'ordre d'arrêt. Les deux
comptent : la mémoire doit exister avant l'ordonnanceur qui l'utilise, et
à l'arrêt, l'ordonnanceur doit se taire avant que la base ne se ferme,
faute de quoi un ouvrier écrirait dans une connexion fermée.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
from typing import Any

from . import agent as agent_module
from . import brain as brain_module
from . import config as config_module
from . import tools as tools_module
from .api import Api
from .memory import Memory
from .memory import sync as sync_module
from .scheduler import Scheduler

log = logging.getLogger("agentos")

#: Plannings posés au premier démarrage. Ils décrivent l'entretien courant
#: d'une machine qui tourne seule — sans eux, la base grossit indéfiniment
#: et la mémoire distante ne part jamais.
DEFAULT_SCHEDULES = [
    ("synchro-distante", "*/5 * * * *", "sync", {}),
    ("compactage-memoire", "30 4 * * *", "compact", {}),
    ("purge-travaux", "0 5 * * 0", "purge", {}),
    ("point-de-reprise", "0 * * * *", "checkpoint", {}),
]


class Runtime:
    """Le processus agent-os : mémoire, cerveau, ordonnanceur, API."""

    def __init__(self, config=None) -> None:
        self.config = config or config_module.load()
        self.memory = Memory(self.config)
        self.brain = brain_module.build(self.config)
        self.scheduler = Scheduler(self.memory.store, self.config)
        # La panoplie complète : c'est la conduite manuelle, où l'opérateur
        # voit passer les actions. Les travaux autonomes en reçoivent une
        # version réduite, construite au moment de leur exécution.
        self.registry = tools_module.build(self.config, self.memory, self.scheduler)
        self.agent = agent_module.Agent(self.config, self.memory, self.brain, self.registry)
        self.sync = self._build_sync()
        self.api = Api(self)
        self._stop = threading.Event()
        self._register_handlers()

    def _build_sync(self):
        try:
            return sync_module.build(self.memory.store, self.config)
        except Exception as exc:  # noqa: BLE001
            # Une synchro mal configurée ne doit pas empêcher la machine de
            # travailler en local ; elle doit en revanche se voir.
            log.error("synchro distante indisponible : %s", exc)
            return None

    # -- travaux ---------------------------------------------------------

    def _register_handlers(self) -> None:
        self.scheduler.register("agent", self._job_agent)
        self.scheduler.register("sync", self._job_sync)
        self.scheduler.register("compact", self._job_compact)
        self.scheduler.register("purge", self._job_purge)
        self.scheduler.register("checkpoint", self._job_checkpoint)

    def _job_agent(self, job, deadline: float) -> dict[str, Any]:
        """Exécute une tâche, avec une panoplie réduite si personne ne regarde.

        Un travail lancé depuis la console est surveillé ; un travail
        déclenché par un planning à 3 h du matin ne l'est pas. Les outils
        capables d'écrire ou de créer une récurrence sont donc retirés dans
        le second cas.
        """
        task = job.payload.get("task")
        if not task:
            raise ValueError("le travail « agent » attend un champ « task »")

        attended = job.origin in ("console", "cli")
        registry = self.registry if attended else tools_module.build(
            self.config, self.memory, self.scheduler, allow_sensitive=False)
        runner = self.agent if attended else agent_module.Agent(
            self.config, self.memory, self.brain, registry)

        result = runner.run(task, deadline=deadline,
                            session=job.payload.get("session") or f"job-{job.uid}")
        if not result.ok:
            raise RuntimeError(f"tâche non aboutie ({result.stopped_by}) : {result.text[:300]}")
        return {"texte": result.text, "tours": len(result.turns),
                "surveille": attended, "session": result.session}

    def _job_sync(self, job, deadline: float) -> dict[str, Any]:
        if self.sync is None:
            return {"ignore": "synchro désactivée"}
        report = self.sync.run()
        if not report.ok:
            raise RuntimeError(report.summary())
        return {"pousses": report.pushed, "tires": report.pulled,
                "conflits": report.conflicts}

    def _job_compact(self, job, deadline: float) -> dict[str, Any]:
        return {"episodes_purges": self.memory.compact()}

    def _job_purge(self, job, deadline: float) -> dict[str, Any]:
        return {"travaux_purges": self.scheduler.queue.purge(
            older_than_days=float(job.payload.get("jours", 30)))}

    def _job_checkpoint(self, job, deadline: float) -> dict[str, Any]:
        """Replie le WAL. Sans cela il croît jusqu'à dépasser la base
        elle-même sur une machine qui écrit en continu."""
        self.memory.store.checkpoint()
        return {"taille_octets": self.memory.store.stats()["bytes"]}

    # -- plannings par défaut --------------------------------------------

    def ensure_default_schedules(self) -> int:
        existing = {row["name"] for row in self.scheduler.list_schedules()}
        created = 0
        for name, expression, job, payload in DEFAULT_SCHEDULES:
            if name in existing:
                continue
            if job == "sync" and self.sync is None:
                continue
            self.scheduler.add_schedule(name, expression, job, payload)
            created += 1
        if created:
            log.info("%d plannings d'entretien créés", created)
        return created

    # -- cycle de vie ----------------------------------------------------

    def start(self) -> None:
        self.ensure_default_schedules()
        self.scheduler.start()
        self.api.start()
        self.memory.record(
            f"démarrage du runtime sur {self.config.remote.node_id}",
            kind="système", actor="system", trust="agent")
        notify("READY=1", f"STATUS=agent-os en marche, {len(self.registry)} outils")
        log.info("agent-os démarré (nœud %s)", self.config.remote.node_id)

    def stop(self) -> None:
        # L'ordre est celui de la construction, à l'envers : sans cela un
        # ouvrier encore vivant écrirait dans une base déjà fermée.
        notify("STOPPING=1")
        log.info("arrêt demandé")
        self._stop.set()
        self.api.stop()
        self.scheduler.stop()
        self.memory.close()
        log.info("agent-os arrêté")

    def run_forever(self) -> int:
        """Boucle principale, jusqu'à SIGTERM ou SIGINT."""
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, lambda *_: self._stop.set())

        self.start()
        watchdog = _watchdog_interval()
        try:
            while not self._stop.is_set():
                self._stop.wait(watchdog or 5.0)
                if watchdog:
                    notify("WATCHDOG=1")
        finally:
            self.stop()
        return 0

    def status(self) -> dict[str, Any]:
        return self.api.status({}, {}, {})


# -- intégration systemd ---------------------------------------------------

def notify(*messages: str) -> None:
    """Envoie un message au superviseur systemd, s'il y en a un.

    Sans cela, `Type=notify` attendrait indéfiniment et systemd finirait par
    tuer un service parfaitement fonctionnel. Hors systemd, la variable
    d'environnement est absente et la fonction ne fait rien.
    """
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):          # espace de noms abstrait Linux
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall("\n".join(messages).encode("utf-8"))
    except OSError as exc:
        log.debug("notification systemd impossible : %s", exc)


def _watchdog_interval() -> float:
    """Moitié de l'intervalle exigé par systemd, comme le veut l'usage.

    Battre exactement à l'intervalle laisserait une gigue ordinaire dépasser
    l'échéance et provoquer un redémarrage sans raison.
    """
    raw = os.environ.get("WATCHDOG_USEC")
    if not raw or os.environ.get("WATCHDOG_PID", str(os.getpid())) != str(os.getpid()):
        return 0.0
    try:
        return max(1.0, int(raw) / 1_000_000 / 2)
    except ValueError:
        return 0.0


def setup_logging(level: str = "INFO", journal: bool = True) -> None:
    """Journalisation vers stderr, que systemd capte et horodate lui-même."""
    handler = logging.StreamHandler(sys.stderr)
    if journal and os.environ.get("JOURNAL_STREAM"):
        # Sous journald, l'horodatage et le nom du service sont déjà ajoutés.
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def main(argv: list[str] | None = None) -> int:
    setup_logging(os.environ.get("AGENTOS_LOG", "INFO"))
    try:
        runtime = Runtime()
    except Exception as exc:  # noqa: BLE001
        log.critical("démarrage impossible : %s", exc)
        notify("STATUS=démarrage impossible", f"ERRNO=1")
        return 1
    return runtime.run_forever()
