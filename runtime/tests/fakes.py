"""Doublures pour les tests : serveur de modèle et configuration jetable."""

from __future__ import annotations

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentos import config as config_module


def temp_config(**overrides):
    """Configuration pointant sur une base neuve dans un répertoire temporaire."""
    cfg = config_module.Config()
    root = Path(tempfile.mkdtemp(prefix="agentos-test-"))
    cfg.memory.database = str(root / "memory.db")
    cfg.memory.vector_dim = 256
    cfg.agent.workspace = str(root / "workspace")
    cfg.remote.node_id = "banc-essai"
    for dotted, value in overrides.items():
        section, _, field = dotted.partition(".")
        setattr(getattr(cfg, section), field, value) if field else setattr(cfg, section, value)
    return cfg, root


class ModelServer:
    """Faux serveur exposant les deux dialectes d'API en même temps.

    Les scénarios se pilotent depuis les tests via les attributs publics :
    `fail_anthropic` fait répondre 503 le nombre de fois voulu, `seen`
    conserve les corps reçus pour vérifier l'encodage.
    """

    def __init__(self) -> None:
        self.fail_anthropic = 0
        self.fail_local = 0
        self.seen: list[tuple[str, dict]] = []
        self.anthropic_reply: dict | None = None
        self.local_reply: dict | None = None
        #: Réponses servies dans l'ordre, une par appel. Une fois la liste
        #: épuisée, `anthropic_reply` reprend la main — ce qui permet de
        #: scénariser une boucle d'outils puis de laisser un état stable.
        self.anthropic_script: list[dict] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- cycle de vie ----------------------------------------------------

    def __enter__(self) -> "ModelServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- réponses par défaut ---------------------------------------------

    @staticmethod
    def default_anthropic(text: str = "Bonjour.") -> dict:
        return {
            "model": "claude-sonnet-5", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }

    @staticmethod
    def anthropic_tool(name: str, arguments: dict, call_id: str = "tu_1",
                       text: str = "") -> dict:
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        content.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
        return {
            "model": "claude-sonnet-5", "stop_reason": "tool_use", "content": content,
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }

    @staticmethod
    def default_local(text: str = "Bonjour.") -> dict:
        return {
            "model": "modele-local",
            "choices": [{"finish_reason": "stop", "message": {"content": text}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }

    # -- implémentation --------------------------------------------------

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _send(self, code: int, obj: dict) -> None:
                raw = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                server.seen.append((self.path, body))

                if self.path == "/v1/messages":
                    if server.fail_anthropic > 0:
                        server.fail_anthropic -= 1
                        return self._send(503, {"error": "surcharge simulée"})
                    if server.anthropic_script:
                        return self._send(200, server.anthropic_script.pop(0))
                    return self._send(200, server.anthropic_reply or server.default_anthropic())

                if self.path.endswith("/chat/completions"):
                    if server.fail_local > 0:
                        server.fail_local -= 1
                        return self._send(503, {"error": "surcharge simulée"})
                    return self._send(200, server.local_reply or server.default_local())

                if self.path.endswith("/embeddings"):
                    count = len(body.get("input") or [""])
                    dim = 256
                    return self._send(200, {"data": [
                        {"embedding": [0.01 * ((i + j) % 17) for j in range(dim)]}
                        for i in range(count)
                    ]})

                self._send(404, {"error": "route inconnue"})

        return Handler
