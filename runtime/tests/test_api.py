"""API locale : routage, jeton, et démarrage complet du runtime."""

from __future__ import annotations

import json
import socket
import unittest
import urllib.error
import urllib.request

from agentos.daemon import Runtime

from .fakes import ModelServer, temp_config


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ApiTest(unittest.TestCase):
    """Démarre un vrai runtime — mémoire, ordonnanceur, API — et l'interroge."""

    @classmethod
    def setUpClass(cls):
        cls.server = ModelServer().__enter__()
        cls.cfg, cls.root = temp_config()
        cls.cfg.api.port = free_port()
        cls.cfg.api.token = "jeton-de-test"
        cls.cfg.brain.order = ["anthropic"]
        cls.cfg.anthropic_key = "clef"
        cls.cfg.brain.anthropic_model = "claude-sonnet-5"
        cls.cfg.scheduler.tick_s = 0.05
        cls.cfg.remote.enabled = False
        cls.runtime = Runtime(cls.cfg)
        # Le backend pointe vers le faux serveur plutôt que vers l'API réelle.
        cls.runtime.brain.backends[0].base_url = f"{cls.server.base}/v1/messages"
        cls.runtime.brain.backends[0].attempts = 1
        cls.runtime.start()

    @classmethod
    def tearDownClass(cls):
        cls.runtime.stop()
        cls.server.__exit__(None, None, None)

    # -- utilitaires -----------------------------------------------------

    def call(self, path, *, method="GET", payload=None, token="jeton-de-test"):
        url = f"http://127.0.0.1:{self.cfg.api.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Agentos-Token"] = token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    # -- lecture ---------------------------------------------------------

    def test_status_reports_every_subsystem(self):
        status, body = self.call("/api/status", token="")
        self.assertEqual(status, 200)
        for key in ("memoire", "cerveau", "ordonnanceur", "distant", "outils"):
            self.assertIn(key, body)
        self.assertEqual(body["noeud"], self.cfg.remote.node_id)
        self.assertIn("anthropic", body["cerveau"])

    def test_console_is_served_at_the_root(self):
        url = f"http://127.0.0.1:{self.cfg.api.port}/"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=10) as response:
            page = response.read().decode()
            self.assertIn("agent-os", page)
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])

    def test_unknown_route_is_404(self):
        status, body = self.call("/api/inexistant")
        self.assertEqual(status, 404)
        self.assertIn("erreur", body)

    def test_tools_are_listed(self):
        _, body = self.call("/api/tools")
        names = {tool["nom"] for tool in body["outils"]}
        self.assertIn("memory_search", names)
        self.assertIn("shell", names)

    # -- jeton -----------------------------------------------------------

    def test_read_routes_need_no_token(self):
        self.assertEqual(self.call("/api/jobs", token="")[0], 200)
        self.assertEqual(self.call("/api/schedules", token="")[0], 200)

    def test_acting_routes_require_the_token(self):
        status, body = self.call("/api/memory", method="POST",
                                 payload={"content": "x"}, token="")
        self.assertEqual(status, 401)
        self.assertIn("jeton", body["erreur"])

    def test_wrong_token_is_refused(self):
        status, _ = self.call("/api/memory", method="POST",
                              payload={"content": "x"}, token="mauvais")
        self.assertEqual(status, 401)

    # -- écriture --------------------------------------------------------

    def test_console_writes_are_operator_trusted(self):
        """La console est la seule voie par laquelle l'humain parle : c'est
        aussi la seule qui puisse produire du « operator »."""
        status, body = self.call("/api/memory", method="POST",
                                 payload={"content": "le disque de sauvegarde est /mnt/backup"})
        self.assertEqual(status, 200)
        row = self.runtime.memory.store.episode(body["uid"])
        self.assertEqual(row["trust"], "operator")

    def test_memory_search_round_trip(self):
        self.call("/api/memory", method="POST",
                  payload={"content": "le port de la sauvegarde est 8443"})
        _, body = self.call("/api/memory/search?q=8443")
        self.assertTrue(any("8443" in r["texte"] for r in body["resultats"]))
        self.assertIn("confiance", body["resultats"][0])

    def test_malformed_json_is_reported(self):
        url = f"http://127.0.0.1:{self.cfg.api.port}/api/memory"
        request = urllib.request.Request(
            url, data=b"{pas du json", method="POST",
            headers={"Content-Type": "application/json", "X-Agentos-Token": "jeton-de-test"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            opener.open(request, timeout=10)
            self.fail("aurait dû échouer")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_missing_field_is_reported(self):
        status, body = self.call("/api/memory", method="POST", payload={})
        self.assertEqual(status, 400)
        self.assertIn("content", body["erreur"])

    # -- plannings et travaux --------------------------------------------

    def test_default_maintenance_schedules_exist(self):
        _, body = self.call("/api/schedules")
        names = {row["name"] for row in body["plannings"]}
        self.assertIn("compactage-memoire", names)
        self.assertIn("purge-travaux", names)
        self.assertNotIn("synchro-distante", names, "pas de synchro si elle est désactivée")

    def test_schedule_lifecycle(self):
        status, body = self.call("/api/schedules", method="POST",
                                 payload={"name": "essai", "cron": "0 4 * * *", "job": "compact"})
        self.assertEqual(status, 200)
        self.assertEqual(body["planning"]["cron"], "0 4 * * *")
        self.assertEqual(self.call("/api/schedules/essai", method="DELETE")[1]["supprime"], True)

    def test_invalid_cron_is_rejected_with_the_field_name(self):
        status, body = self.call("/api/schedules", method="POST",
                                 payload={"name": "x", "cron": "99 * * * *", "job": "compact"})
        self.assertEqual(status, 400)
        self.assertIn("minute", body["erreur"])

    def test_unknown_job_is_404(self):
        status, _ = self.call("/api/jobs", method="POST", payload={"job": "inexistant"})
        self.assertEqual(status, 404)

    def test_maintenance_job_runs_for_real(self):
        status, body = self.call("/api/jobs", method="POST", payload={"job": "checkpoint"})
        self.assertEqual(status, 200)
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            row = self.runtime.scheduler.queue.get(body["uid"])
            if row["state"] in ("done", "dead"):
                break
            time.sleep(0.05)
        self.assertEqual(row["state"], "done", row.get("error"))

    # -- tâche -----------------------------------------------------------

    def test_task_runs_through_the_agent(self):
        self.server.anthropic_reply = self.server.default_anthropic("Tout va bien.")
        status, body = self.call("/api/task", method="POST",
                                 payload={"task": "fais le point"})
        self.assertEqual(status, 200)
        self.assertTrue(body["termine"])
        self.assertEqual(body["texte"], "Tout va bien.")

    def test_async_task_is_queued(self):
        _, body = self.call("/api/task", method="POST",
                            payload={"task": "en différé", "async": True})
        self.assertEqual(body["mode"], "differe")
        self.assertIsNotNone(self.runtime.scheduler.queue.get(body["uid"]))

    def test_sync_route_reports_when_disabled(self):
        status, body = self.call("/api/sync", method="POST")
        self.assertEqual(status, 409)
        self.assertIn("désactivée", body["erreur"])


class UnattendedToolsTest(unittest.TestCase):
    def test_scheduled_task_loses_the_sensitive_tools(self):
        """Un travail déclenché à 3 h du matin n'a pas de témoin : les outils
        capables d'écrire ou de créer une récurrence lui sont retirés."""
        cfg, _ = temp_config()
        cfg.remote.enabled = False
        runtime = Runtime(cfg)
        try:
            from agentos import tools as tools_module

            unattended = tools_module.build(cfg, runtime.memory, runtime.scheduler,
                                            allow_sensitive=False)
            attended = {spec.name for spec in runtime.registry.specs()}
            restricted = {spec.name for spec in unattended.specs()}
            self.assertIn("schedule_add", attended)
            self.assertNotIn("schedule_add", restricted)
            self.assertNotIn("fs_write", restricted)
            self.assertIn("memory_search", restricted)
        finally:
            runtime.memory.close()


if __name__ == "__main__":
    unittest.main()
