"""API HTTP locale et console.

Le serveur n'écoute que sur la boucle locale. Il n'a ni TLS ni gestion de
comptes, et c'est délibéré : les ajouter donnerait l'illusion qu'il peut
être exposé. Pour un accès distant, il faut un reverse proxy qui termine
le TLS et authentifie — ou, plus simplement, un tunnel SSH.

Les routes de lecture sont ouvertes à quiconque atteint la boucle locale ;
les routes qui agissent exigent un jeton. La distinction compte sur une
machine où d'autres services tournent : lire l'état est sans conséquence,
déclencher une tâche ou créer une récurrence ne l'est pas.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("agentos.api")

CONSOLE = Path(__file__).parent / "console" / "index.html"
MAX_BODY = 1_000_000


class Router:
    """Table de routage minimale : méthode + chemin exact ou préfixe."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[Callable, bool]] = {}

    def add(self, method: str, path: str, handler: Callable, *, guarded: bool = False) -> None:
        self.routes[(method, path)] = (handler, guarded)

    def match(self, method: str, path: str):
        exact = self.routes.get((method, path))
        if exact:
            return exact, {}
        # Une seule forme paramétrée est nécessaire : /prefixe/{valeur}
        for (route_method, route_path), entry in self.routes.items():
            if route_method != method or not route_path.endswith("/*"):
                continue
            prefix = route_path[:-1]
            if path.startswith(prefix) and len(path) > len(prefix):
                return entry, {"value": urllib.parse.unquote(path[len(prefix):])}
        return None, {}


class Api:
    """Assemble les routes autour du runtime."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.config = runtime.config
        self.router = Router()
        self._register()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.started_at = time.time()

    # -- routes ----------------------------------------------------------

    def _register(self) -> None:
        add = self.router.add
        add("GET", "/api/status", self.status)
        add("GET", "/api/memory/search", self.memory_search)
        add("GET", "/api/memory/recent", self.memory_recent)
        add("POST", "/api/memory", self.memory_record, guarded=True)
        add("DELETE", "/api/memory/*", self.memory_forget, guarded=True)
        add("GET", "/api/jobs", self.jobs)
        add("POST", "/api/jobs", self.job_create, guarded=True)
        add("GET", "/api/schedules", self.schedules)
        add("POST", "/api/schedules", self.schedule_create, guarded=True)
        add("DELETE", "/api/schedules/*", self.schedule_delete, guarded=True)
        add("POST", "/api/task", self.task, guarded=True)
        add("POST", "/api/sync", self.sync, guarded=True)
        add("GET", "/api/tools", self.tools)

    def status(self, query, body, params) -> dict[str, Any]:
        runtime = self.runtime
        status: dict[str, Any] = {
            "nom": self.config.agent.name,
            "noeud": self.config.remote.node_id,
            "version": _version(),
            "debout_depuis_s": round(time.time() - self.started_at),
            "memoire": runtime.memory.stats(),
            "cerveau": runtime.brain.status(),
            "ordonnanceur": runtime.scheduler.status(),
            "outils": runtime.registry.stats(),
        }
        status["distant"] = runtime.sync.status() if runtime.sync else {"actif": False}
        return status

    def memory_search(self, query, body, params) -> dict[str, Any]:
        text = (query.get("q") or [""])[0]
        limit = _int(query.get("limit"), 10, 1, 100)
        scope = (query.get("scope") or [""])[0] or None
        results = self.runtime.memory.search(text, limit=limit, scope=scope)
        return {"requete": text, "resultats": [
            {"uid": r.uid, "portee": r.scope, "texte": r.text, "score": round(r.score, 5),
             "confiance": r.trust, "via": r.sources, "quand": r.ts, "meta": r.meta}
            for r in results]}

    def memory_recent(self, query, body, params) -> dict[str, Any]:
        limit = _int(query.get("limit"), 30, 1, 200)
        kind = (query.get("kind") or [""])[0] or None
        rows = self.runtime.memory.recent(limit, kind=kind)
        return {"souvenirs": [
            {"uid": r["uid"], "quand": r["ts"], "type": r["kind"], "acteur": r["actor"],
             "confiance": r["trust"], "texte": r["content"][:2000]} for r in rows]}

    def memory_record(self, query, body, params) -> dict[str, Any]:
        content = _required(body, "content")
        # Écrire par cette route, c'est l'humain qui parle : c'est le seul
        # chemin par lequel le niveau « operator » peut être attribué.
        uid = self.runtime.memory.record(
            content, kind=body.get("kind", "consigne"), actor="operator",
            trust=body.get("trust", "operator"),
        )
        return {"uid": uid}

    def memory_forget(self, query, body, params) -> dict[str, Any]:
        return {"oublie": self.runtime.memory.forget(params["value"])}

    def jobs(self, query, body, params) -> dict[str, Any]:
        state = (query.get("state") or [""])[0] or None
        return {"travaux": self.runtime.scheduler.queue.list(
            state=state, limit=_int(query.get("limit"), 50, 1, 200))}

    def job_create(self, query, body, params) -> dict[str, Any]:
        name = _required(body, "job")
        if name not in self.runtime.scheduler.handlers:
            raise ApiError(404, f"travail inconnu : {name}")
        uid = self.runtime.scheduler.queue.enqueue(
            name, body.get("payload") or {},
            delay_s=float(body.get("delay_s") or 0), origin="console")
        return {"uid": uid}

    def schedules(self, query, body, params) -> dict[str, Any]:
        return {"plannings": self.runtime.scheduler.list_schedules()}

    def schedule_create(self, query, body, params) -> dict[str, Any]:
        from .scheduler.cron import CronError

        try:
            row = self.runtime.scheduler.add_schedule(
                _required(body, "name"), _required(body, "cron"), _required(body, "job"),
                body.get("payload") or {}, enabled=bool(body.get("enabled", True)))
        except CronError as exc:
            raise ApiError(400, str(exc)) from exc
        return {"planning": row}

    def schedule_delete(self, query, body, params) -> dict[str, Any]:
        return {"supprime": self.runtime.scheduler.remove_schedule(params["value"])}

    def task(self, query, body, params) -> dict[str, Any]:
        """Lance une tâche. Synchrone par défaut, déposée en file si `async`.

        Une tâche longue lancée en synchrone tiendrait la connexion HTTP
        pendant tout son déroulement ; la voie asynchrone rend la main
        immédiatement et laisse suivre l'avancement par /api/jobs.
        """
        prompt = _required(body, "task")
        if body.get("async"):
            uid = self.runtime.scheduler.queue.enqueue(
                "agent", {"task": prompt}, origin="console")
            return {"uid": uid, "mode": "differe"}
        result = self.runtime.agent.run(prompt, max_turns=_int_value(body.get("max_turns")))
        return {
            "texte": result.text, "termine": result.ok, "arret": result.stopped_by,
            "session": result.session, "tours": len(result.turns),
            "jetons": {"entree": result.input_tokens, "sortie": result.output_tokens},
        }

    def sync(self, query, body, params) -> dict[str, Any]:
        if self.runtime.sync is None:
            raise ApiError(409, "synchro distante désactivée")
        report = self.runtime.sync.run()
        return {"pousses": report.pushed, "tires": report.pulled,
                "conflits": report.conflicts, "erreurs": report.errors,
                "duree_s": round(report.duration_s, 2)}

    def tools(self, query, body, params) -> dict[str, Any]:
        return {"outils": [
            {"nom": spec.name, "description": spec.description}
            for spec in self.runtime.registry.specs()]}

    # -- cycle de vie ----------------------------------------------------

    def serve_forever(self) -> None:
        self._server = ThreadingHTTPServer(
            (self.config.api.host, self.config.api.port), _handler_factory(self))
        self._server.daemon_threads = True
        log.info("console sur http://%s:%d", self.config.api.host, self.config.api.port)
        self._server.serve_forever()

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="agentos-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _handler_factory(api: Api):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "agent-os"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        # -- utilitaires -------------------------------------------------

        def _send(self, status: int, payload: Any, content_type="application/json") -> None:
            if content_type == "application/json":
                raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            else:
                raw = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            # La console est servie depuis la même origine et ne charge rien
            # d'ailleurs : une CSP stricte coûte une ligne et ferme la porte
            # à l'injection d'un script tiers dans une page qui affiche du
            # contenu mémorisé, lequel peut venir du réseau.
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def _authorised(self) -> bool:
            expected = api.config.api.token
            if not expected:
                return True
            header = self.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else self.headers.get(
                "X-Agentos-Token", "")
            # Comparaison à temps constant : une comparaison naïve laisse
            # deviner le jeton octet par octet par la mesure du temps.
            import hmac

            return hmac.compare_digest(token, expected)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > MAX_BODY:
                raise ApiError(413, "corps de requête trop volumineux")
            try:
                parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ApiError(400, f"JSON invalide : {exc}") from exc
            if not isinstance(parsed, dict):
                raise ApiError(400, "le corps doit être un objet JSON")
            return parsed

        # -- répartition -------------------------------------------------

        def _dispatch(self, method: str) -> None:
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path.rstrip("/") or "/"

            if method == "GET" and path in ("/", "/index.html"):
                try:
                    return self._send(200, CONSOLE.read_bytes(), "text/html")
                except OSError:
                    return self._send(404, {"erreur": "console absente"})

            entry, params = api.router.match(method, path)
            if entry is None:
                return self._send(404, {"erreur": f"route inconnue : {method} {path}"})

            handler, guarded = entry
            if guarded and not self._authorised():
                return self._send(401, {"erreur": "jeton requis pour cette action"})

            try:
                body = self._body() if method in ("POST", "PUT", "PATCH") else {}
                result = handler(urllib.parse.parse_qs(parts.query), body, params)
            except ApiError as exc:
                return self._send(exc.status, {"erreur": str(exc)})
            except Exception as exc:  # noqa: BLE001
                log.exception("erreur sur %s %s", method, path)
                return self._send(500, {"erreur": f"{type(exc).__name__}: {exc}"})
            return self._send(200, result)

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def do_DELETE(self):  # noqa: N802
            self._dispatch("DELETE")

    return Handler


def _required(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, f"champ « {key} » requis")
    return value


def _int(values, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int((values or [default])[0])))
    except (TypeError, ValueError):
        return default


def _int_value(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _version() -> str:
    from . import __version__

    return __version__
