"""agentosctl — pilotage de la machine depuis le terminal.

Deux voies d'accès selon la commande. Tout ce qui engage l'agent passe par
l'API du démon, pour partager son état plutôt que d'en ouvrir un second.
Tout ce qui ne fait que lire ou réparer la base fonctionne aussi démon
éteint — c'est précisément quand il ne démarre pas qu'on a besoin de
`doctor` et de `memory`.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
from typing import Any

from . import config as config_module
from . import net


def _api(config, path: str, *, method: str = "GET", payload: Any = None) -> dict:
    url = f"http://{config.api.host}:{config.api.port}{path}"
    headers = {"X-Agentos-Token": config.api.token} if config.api.token else {}
    try:
        return net.request_json(url, method=method, payload=payload,
                                headers=headers, timeout=300, attempts=1)
    except net.HttpError as exc:
        if exc.status == 401:
            raise SystemExit(
                "jeton refusé — renseigner AGENTOS_API_TOKEN ou api.token") from exc
        if exc.status:
            raise SystemExit(f"erreur {exc.status} : {_message(exc.body)}") from exc
        raise SystemExit(
            f"démon injoignable sur {config.api.host}:{config.api.port} — "
            "vérifier « systemctl status agentos »"
        ) from exc


def _message(body: str) -> str:
    try:
        return json.loads(body).get("erreur", body)
    except (json.JSONDecodeError, AttributeError):
        return body[:300]


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


# -- commandes -------------------------------------------------------------

def cmd_status(config, args) -> int:
    status = _api(config, "/api/status")
    if args.json:
        _print(status)
        return 0

    print(f"agent-os {status['version']} · nœud {status['noeud']} · "
          f"debout depuis {status['debout_depuis_s']} s")
    memory = status["memoire"]
    print(f"  mémoire   : {memory['episodes']} épisodes, {memory['facts']} faits, "
          f"{memory['bytes'] / 1e6:.1f} Mo, index {memory['index']['entries']} "
          f"({memory['index']['backend']})")
    for name, backend in status["cerveau"].items():
        mark = "→" if backend["actif"] else " "
        state = "disponible" if backend["disponible"] else "indisponible"
        if backend["disjoncteur_ouvert"]:
            state = f"écarté {backend['reouverture_dans_s']} s"
        print(f"  {mark} {name:<10}: {backend['modele']} — {state}")
    scheduler = status["ordonnanceur"]
    print(f"  travaux   : {scheduler['travaux']}")
    if scheduler["prochain"]:
        nxt = scheduler["prochain"]
        print(f"  prochain  : {nxt['nom']} ({nxt['cron']}) dans {nxt['dans_s']} s")
    remote = status["distant"]
    if remote.get("actif"):
        print(f"  distant   : {remote['en_attente']} en attente, "
              f"chiffré={remote['chiffre']}, plafond={remote['plafond_confiance']}")
    else:
        print("  distant   : désactivé")
    return 0


def cmd_ask(config, args) -> int:
    task = " ".join(args.task)
    result = _api(config, "/api/task", method="POST",
                  payload={"task": task, "async": args.background,
                           "max_turns": args.max_turns})
    if args.background:
        print(f"travail {result['uid']} déposé")
        return 0
    print(result["texte"])
    if not result["termine"]:
        print(f"\n(arrêt : {result['arret']}, {result['tours']} tours)", file=sys.stderr)
        return 2
    return 0


def cmd_memory(config, args) -> int:
    if args.action in ("search", "recent", "stats") and args.offline:
        return _memory_offline(config, args)

    if args.action == "search":
        body = _api(config, f"/api/memory/search?q={_quote(args.query)}&limit={args.limit}")
        for row in body["resultats"]:
            flag = "" if row["confiance"] == "operator" else f" ⚠{row['confiance']}"
            print(f"[{row['portee']}{flag}] {row['score']:.4f} {row['texte'][:160]}")
        return 0
    if args.action == "recent":
        for row in _api(config, f"/api/memory/recent?limit={args.limit}")["souvenirs"]:
            print(f"[{row['type']}] {row['texte'][:160]}")
        return 0
    if args.action == "record":
        _print(_api(config, "/api/memory", method="POST",
                    payload={"content": args.query, "trust": "operator"}))
        return 0
    if args.action == "forget":
        _print(_api(config, f"/api/memory/{_quote(args.query)}", method="DELETE"))
        return 0
    if args.action == "stats":
        _print(_api(config, "/api/status")["memoire"])
        return 0
    if args.action in ("reindex", "compact"):
        return _memory_offline(config, args)
    return 1


def _memory_offline(config, args) -> int:
    """Opère directement sur la base, démon éteint.

    Réindexer avec le démon en marche produirait deux index divergents : la
    commande exige donc l'arrêt du service, et le dit plutôt que de laisser
    l'utilisateur le découvrir plus tard.
    """
    from .memory import Memory

    if args.action in ("reindex", "compact") and _daemon_alive(config):
        raise SystemExit(
            f"« {args.action} » exige que le démon soit arrêté "
            "(systemctl stop agentos), sinon deux index divergeraient")

    memory = Memory(config)
    try:
        if args.action == "reindex":
            print(f"{memory.reindex()} entrées réindexées")
        elif args.action == "compact":
            print(f"{memory.compact()} épisodes purgés")
        elif args.action == "stats":
            _print(memory.stats())
        elif args.action == "search":
            for row in memory.search(args.query, limit=args.limit):
                flag = "" if row.trust == "operator" else f" ⚠{row.trust}"
                print(f"[{row.scope}{flag}] {row.score:.4f} {row.text[:160]}")
        elif args.action == "recent":
            for row in memory.recent(args.limit):
                print(f"[{row['kind']}] {row['content'][:160]}")
    finally:
        memory.close()
    return 0


def cmd_jobs(config, args) -> int:
    if args.action == "list":
        for row in _api(config, f"/api/jobs?limit={args.limit}")["travaux"]:
            error = f" — {row['error'][:80]}" if row.get("error") else ""
            print(f"{row['uid']} {row['name']:<12} {row['state']:<8} "
                  f"essai {row['attempts']}/{row['max_attempts']}{error}")
        return 0
    _print(_api(config, "/api/jobs", method="POST",
                payload={"job": args.name, "delay_s": args.delay}))
    return 0


def cmd_schedule(config, args) -> int:
    if args.action == "list":
        for row in _api(config, "/api/schedules")["plannings"]:
            state = "actif" if row["enabled"] else "off"
            print(f"{row['name']:<20} {row['cron']:<16} → {row['job']:<12} [{state}]")
        return 0
    if args.action == "add":
        _print(_api(config, "/api/schedules", method="POST",
                    payload={"name": args.name, "cron": args.cron, "job": args.job}))
        return 0
    _print(_api(config, f"/api/schedules/{_quote(args.name)}", method="DELETE"))
    return 0


def cmd_sync(config, args) -> int:
    report = _api(config, "/api/sync", method="POST")
    print(f"{report['pousses']} poussé(s), {report['tires']} tiré(s), "
          f"{report['conflits']} conflit(s) en {report['duree_s']} s")
    for error in report["erreurs"]:
        print(f"  erreur : {error}", file=sys.stderr)
    return 1 if report["erreurs"] else 0


def cmd_doctor(config, args) -> int:
    """Diagnostic hors ligne : ce qu'il faut savoir quand rien ne démarre."""
    from .memory import crypto, embed

    problems = 0

    def check(label: str, ok: bool, detail: str = "", fatal: bool = True) -> None:
        nonlocal problems
        mark = "ok  " if ok else ("ÉCHEC" if fatal else "avert")
        print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not ok and fatal:
            problems += 1

    print(f"agent-os · nœud {config.remote.node_id}\n")

    for anomalie in config.problemes:
        check("configuration", False, anomalie, fatal=False)

    database = config.memory.database
    parent = os.path.dirname(database) or "."
    # Le répertoire peut ne pas exister au premier démarrage : ce qui compte
    # est qu'il soit créable, donc que le premier ancêtre existant soit
    # inscriptible. Exiger son existence signalerait à tort une machine
    # fraîchement installée comme cassée.
    ancestor = pathlib.Path(parent).absolute()
    while not ancestor.exists() and ancestor.parent != ancestor:
        ancestor = ancestor.parent
    writable = os.access(ancestor, os.W_OK)
    check("répertoire d'état inscriptible", writable,
          parent if ancestor == pathlib.Path(parent).absolute()
          else f"{parent} (à créer sous {ancestor})")

    if writable:
        free = shutil.disk_usage(ancestor).free
        check("espace disque", free > 2 * 1024**3, f"{free / 1e9:.1f} Go libres", fatal=False)

    try:
        from .memory.store import Store

        store = Store(database, node=config.remote.node_id)
        stats = store.stats()
        check("base de mémoire", True,
              f"{stats['episodes']} épisodes, {stats['bytes'] / 1e6:.1f} Mo")
        store.close()
    except Exception as exc:  # noqa: BLE001
        check("base de mémoire", False, str(exc))

    check("clé Anthropic", bool(config.anthropic_key),
          "absente — le backend distant sera écarté" if not config.anthropic_key else "présente",
          fatal=False)
    check("serveur de modèle local", net.reachable(config.brain.local_url, timeout=2),
          config.brain.local_url, fatal=False)

    embedder = embed.build(config)
    live = [b.name for b in embedder.backends if b.available()]
    check("embeddings", bool(live), f"disponibles : {', '.join(live)}")

    if config.remote.enabled:
        check("URL Supabase", bool(config.remote.url), config.remote.url or "absente")
        check("clé de service Supabase", bool(config.remote_api_key))
        if config.remote.encrypt:
            check("chiffrement distant", crypto.available() and bool(config.remote_key),
                  "cryptography utilisable et clé présente")
    else:
        check("synchro distante", True, "désactivée", fatal=False)

    check("démon en marche", _daemon_alive(config),
          f"{config.api.host}:{config.api.port}", fatal=False)

    print()
    if problems:
        print(f"{problems} problème(s) bloquant(s).")
    else:
        print("Aucun problème bloquant.")
    return 1 if problems else 0


def _daemon_alive(config) -> bool:
    return net.reachable(f"http://{config.api.host}:{config.api.port}", timeout=1)


def _quote(value: str) -> str:
    import urllib.parse

    return urllib.parse.quote(value, safe="")


# -- analyse des arguments -------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentosctl", description="Pilotage d'une machine agent-os.")
    parser.add_argument("--json", action="store_true", help="sortie brute en JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="état du runtime")

    ask = sub.add_parser("ask", help="confier une tâche à l'agent")
    ask.add_argument("task", nargs="+")
    ask.add_argument("-b", "--background", action="store_true",
                     help="déposer en file au lieu d'attendre")
    ask.add_argument("--max-turns", type=int, default=None)

    memory = sub.add_parser("memory", help="inspection et entretien de la mémoire")
    memory.add_argument("action", choices=["search", "recent", "record", "forget",
                                           "stats", "reindex", "compact"])
    memory.add_argument("query", nargs="?", default="")
    memory.add_argument("-n", "--limit", type=int, default=10)
    memory.add_argument("--offline", action="store_true",
                        help="opérer sur la base sans passer par le démon")

    jobs = sub.add_parser("jobs", help="file de travaux")
    jobs.add_argument("action", choices=["list", "run"])
    jobs.add_argument("name", nargs="?", default="")
    jobs.add_argument("-n", "--limit", type=int, default=20)
    jobs.add_argument("--delay", type=float, default=0)

    schedule = sub.add_parser("schedule", help="plannings récurrents")
    schedule.add_argument("action", choices=["list", "add", "remove"])
    schedule.add_argument("name", nargs="?", default="")
    schedule.add_argument("--cron", default="")
    schedule.add_argument("--job", default="")

    sub.add_parser("sync", help="forcer une synchro distante")
    sub.add_parser("doctor", help="diagnostic, fonctionne démon éteint")
    return parser


COMMANDS = {
    "status": cmd_status, "ask": cmd_ask, "memory": cmd_memory,
    "jobs": cmd_jobs, "schedule": cmd_schedule, "sync": cmd_sync, "doctor": cmd_doctor,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # `doctor` doit fonctionner quand la configuration est illisible : c'est
    # exactement le cas qu'on lui demande de diagnostiquer.
    config = config_module.load(strict=(args.command != "doctor"))

    if args.command == "memory" and args.action in ("search", "record", "forget") \
            and not args.query:
        raise SystemExit(f"« memory {args.action} » attend un argument")
    if args.command == "schedule" and args.action == "add" and not (args.cron and args.job):
        raise SystemExit("« schedule add » attend --cron et --job")
    if args.command == "jobs" and args.action == "run" and not args.name:
        raise SystemExit("« jobs run » attend le nom du travail")

    try:
        return COMMANDS[args.command](config, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
