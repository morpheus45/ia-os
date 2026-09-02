"""Outils d'automatisation : l'agent programme son propre travail.

C'est ce qui sépare une machine qui répond d'une machine qui fonctionne
seule. L'agent peut créer un planning récurrent, déposer un travail
différé, et consulter ce qui tourne.

Les plannings sont marqués comme sensibles : une récurrence créée par un
modèle s'exécutera indéfiniment sans que personne ne la relise. Quand
l'agent tourne sans surveillance, ces outils sont retirés de sa panoplie
et seule la console peut créer une récurrence.
"""

from __future__ import annotations

from datetime import datetime

from ..scheduler.cron import Cron, CronError
from .registry import Registry, ToolError


def register(registry: Registry, scheduler) -> None:
    def schedule_add(name: str, cron: str, job: str, payload: str = "") -> str:
        import json

        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError as exc:
            raise ToolError(f"payload JSON invalide : {exc}") from exc
        if not isinstance(parsed, dict):
            raise ToolError("le payload doit être un objet JSON")
        if job not in scheduler.handlers:
            raise ToolError(
                f"travail « {job} » inconnu ; disponibles : "
                f"{', '.join(scheduler.handler_names())}"
            )
        try:
            row = scheduler.add_schedule(name, cron, job, parsed)
        except CronError as exc:
            raise ToolError(str(exc)) from exc
        when = datetime.fromtimestamp(row["next_fire"]).isoformat(timespec="minutes")
        return f"planning « {name} » créé ({cron}), prochain déclenchement {when}"

    def schedule_list() -> str:
        rows = scheduler.list_schedules()
        if not rows:
            return "aucun planning"
        lines = []
        for row in rows:
            state = "actif" if row["enabled"] else "désactivé"
            when = (datetime.fromtimestamp(row["next_fire"]).isoformat(timespec="minutes")
                    if row["next_fire"] else "jamais")
            lines.append(f"{row['name']} · {row['cron']} · {row['job']} · {state} · → {when}")
        return "\n".join(lines)

    def schedule_remove(name: str) -> str:
        return (f"planning « {name} » supprimé" if scheduler.remove_schedule(name)
                else f"planning « {name} » introuvable")

    def job_enqueue(job: str, payload: str = "", delay_s: int = 0) -> str:
        import json

        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError as exc:
            raise ToolError(f"payload JSON invalide : {exc}") from exc
        if job not in scheduler.handlers:
            raise ToolError(f"travail « {job} » inconnu ; disponibles : "
                            f"{', '.join(scheduler.handler_names())}")
        uid = scheduler.queue.enqueue(job, parsed, delay_s=delay_s, origin="agent")
        return f"travail {uid} déposé" + (f", différé de {delay_s}s" if delay_s else "")

    def job_status(uid: str = "", limit: int = 10) -> str:
        if uid:
            row = scheduler.queue.get(uid)
            if row is None:
                return f"{uid} introuvable"
            return (f"{row['name']} · {row['state']} · essai {row['attempts']}/"
                    f"{row['max_attempts']}" + (f" · erreur : {row['error']}" if row["error"] else ""))
        rows = scheduler.queue.list(limit=limit)
        if not rows:
            return "aucun travail"
        return "\n".join(f"{r['uid']} · {r['name']} · {r['state']}" for r in rows)

    def cron_explain(cron: str, count: int = 5) -> str:
        """Montre les prochains déclenchements — le meilleur moyen de vérifier
        qu'une expression veut bien dire ce qu'on croit."""
        try:
            parsed = Cron(cron)
        except CronError as exc:
            raise ToolError(str(exc)) from exc
        moment = datetime.now()
        out = []
        for _ in range(max(1, min(count, 20))):
            moment = parsed.next_after(moment)
            if moment is None:
                out.append("(plus aucune occurrence)")
                break
            out.append(moment.isoformat(timespec="minutes"))
        return "\n".join(out)

    registry.register(
        "schedule_add", "Crée un planning récurrent en notation cron à cinq champs.",
        {"type": "object",
         "properties": {
             "name": {"type": "string"},
             "cron": {"type": "string", "description": "ex. « 0 3 * * * » pour 3 h chaque nuit"},
             "job": {"type": "string"},
             "payload": {"type": "string", "default": "", "description": "objet JSON"}},
         "required": ["name", "cron", "job"]},
        schedule_add, sensitive=True,
    )
    registry.register(
        "schedule_list", "Liste les plannings et leur prochain déclenchement.",
        {"type": "object", "properties": {}, "required": []}, schedule_list)
    registry.register(
        "schedule_remove", "Supprime un planning.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        schedule_remove, sensitive=True)
    registry.register(
        "job_enqueue", "Dépose un travail à exécuter, éventuellement différé.",
        {"type": "object",
         "properties": {"job": {"type": "string"},
                        "payload": {"type": "string", "default": ""},
                        "delay_s": {"type": "integer", "default": 0}},
         "required": ["job"]},
        job_enqueue, sensitive=True)
    registry.register(
        "job_status", "État d'un travail, ou des derniers travaux.",
        {"type": "object",
         "properties": {"uid": {"type": "string", "default": ""},
                        "limit": {"type": "integer", "default": 10}},
         "required": []},
        job_status)
    registry.register(
        "cron_explain", "Donne les prochains déclenchements d'une expression cron.",
        {"type": "object",
         "properties": {"cron": {"type": "string"}, "count": {"type": "integer", "default": 5}},
         "required": ["cron"]},
        cron_explain)
