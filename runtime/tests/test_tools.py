"""Registre, confinement des outils et garde-fous."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from agentos.memory import Memory
from agentos.memory.embed import HashingEmbedder
from agentos.memory.store import Store
from agentos.scheduler import Scheduler
from agentos.tools import Registry, ToolError, Workspace, build
from agentos.tools import automation, fs, memory_tools, shell, web

from .fakes import temp_config


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.registry.register(
            "addition", "additionne",
            {"type": "object",
             "properties": {"a": {"type": "integer"}, "b": {"type": "integer", "default": 1},
                            "mode": {"type": "string", "enum": ["entier", "réel"],
                                     "default": "entier"}},
             "required": ["a"]},
            lambda a, b=1, mode="entier": a + b,
        )

    def test_calls_and_applies_defaults(self):
        self.assertEqual(self.registry.call("addition", {"a": 2}).content, "3")

    def test_missing_argument_returns_a_message_not_an_exception(self):
        outcome = self.registry.call("addition", {})
        self.assertTrue(outcome.is_error)
        self.assertIn("manquant", outcome.content)

    def test_unknown_argument_lists_the_expected_ones(self):
        outcome = self.registry.call("addition", {"a": 1, "c": 2})
        self.assertTrue(outcome.is_error)
        self.assertIn("inconnu", outcome.content)
        self.assertIn("mode", outcome.content)

    def test_coerces_plausible_types(self):
        """Un modèle rend « 3 » là où un entier est attendu : convertir sans
        perte vaut mieux que renvoyer une erreur."""
        self.assertEqual(self.registry.call("addition", {"a": "2", "b": "5"}).content, "7")

    def test_refuses_lossy_coercion(self):
        outcome = self.registry.call("addition", {"a": 2.7})
        self.assertTrue(outcome.is_error)
        self.assertIn("integer", outcome.content)

    def test_enum_is_enforced(self):
        outcome = self.registry.call("addition", {"a": 1, "mode": "complexe"})
        self.assertTrue(outcome.is_error)
        self.assertIn("acceptées", outcome.content)

    def test_unknown_tool_lists_alternatives(self):
        outcome = self.registry.call("inexistant", {})
        self.assertTrue(outcome.is_error)
        self.assertIn("addition", outcome.content)

    def test_handler_exception_never_escapes(self):
        def explode():
            raise RuntimeError("cassé")

        self.registry.register("explose", "", {"type": "object", "properties": {}}, explode)
        outcome = self.registry.call("explose", {})
        self.assertTrue(outcome.is_error)
        self.assertIn("cassé", outcome.content)

    def test_sensitive_tools_hidden_when_unattended(self):
        guarded = Registry(allow_sensitive=False)
        guarded.register("lire", "", {"type": "object", "properties": {}}, lambda: "ok")
        guarded.register("ecrire", "", {"type": "object", "properties": {}}, lambda: "ok",
                         sensitive=True)
        self.assertEqual([spec.name for spec in guarded.specs()], ["lire"])
        self.assertTrue(guarded.call("ecrire", {}).is_error)

    def test_duplicate_registration_is_refused(self):
        with self.assertRaises(ValueError):
            self.registry.register("addition", "", {"type": "object", "properties": {}},
                                   lambda: None)

    def test_stats_track_calls_and_failures(self):
        self.registry.call("addition", {"a": 1})
        self.registry.call("addition", {})
        stats = {row["nom"]: row for row in self.registry.stats()}
        self.assertEqual(stats["addition"]["appels"], 2)
        self.assertEqual(stats["addition"]["echecs"], 1)


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.root = temp_config()
        self.workspace = Workspace(self.cfg.agent.workspace)
        self.registry = Registry()
        fs.register(self.registry, self.workspace)

    def test_write_then_read(self):
        self.registry.call("fs_write", {"path": "notes/a.txt", "content": "bonjour"})
        self.assertEqual(self.registry.call("fs_read", {"path": "notes/a.txt"}).content, "bonjour")

    def test_append(self):
        self.registry.call("fs_write", {"path": "a.txt", "content": "un"})
        self.registry.call("fs_write", {"path": "a.txt", "content": "deux", "append": True})
        self.assertEqual(self.registry.call("fs_read", {"path": "a.txt"}).content, "undeux")

    def test_parent_traversal_is_refused(self):
        for escape in ["../evade.txt", "../../etc/passwd", "a/../../evade.txt"]:
            outcome = self.registry.call("fs_write", {"path": escape, "content": "x"})
            self.assertTrue(outcome.is_error, escape)
            self.assertIn("sort de l'espace de travail", outcome.content)

    def test_absolute_path_is_refused(self):
        self.assertTrue(self.registry.call("fs_read", {"path": "/etc/passwd"}).is_error)

    def test_symlink_escape_is_refused(self):
        """Le confinement est vérifié après résolution des liens : sinon un
        lien déposé dans l'espace de travail suffirait à en sortir."""
        link = Path(self.workspace.root) / "porte"
        os.symlink("/etc", link)
        outcome = self.registry.call("fs_read", {"path": "porte/passwd"})
        self.assertTrue(outcome.is_error)
        self.assertIn("sort de l'espace de travail", outcome.content)

    def test_listing_and_deletion(self):
        self.registry.call("fs_write", {"path": "x.txt", "content": "y"})
        self.assertIn("x.txt", self.registry.call("fs_list", {}).content)
        self.assertFalse(self.registry.call("fs_delete", {"path": "x.txt"}).is_error)
        self.assertTrue(self.registry.call("fs_delete", {"path": "x.txt"}).is_error)

    def test_root_cannot_be_deleted(self):
        self.assertTrue(self.registry.call("fs_delete", {"path": "."}).is_error)

    def test_long_file_is_truncated(self):
        self.registry.call("fs_write", {"path": "gros.txt", "content": "a" * 5000})
        content = self.registry.call("fs_read", {"path": "gros.txt", "max_chars": 100}).content
        self.assertIn("tronqué", content)
        self.assertLess(len(content), 200)


class ShellTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.root = temp_config()
        self.registry = Registry()
        shell.register(self.registry, allowlist=["echo", "ls", "wc"],
                       workdir=self.cfg.agent.workspace, timeout_s=5)

    def test_allowed_command_runs(self):
        self.assertEqual(self.registry.call("shell", {"command": "echo bonjour"}).content.strip(),
                         "bonjour")

    def test_command_outside_allowlist_is_refused(self):
        outcome = self.registry.call("shell", {"command": "rm -rf /"})
        self.assertTrue(outcome.is_error)
        self.assertIn("liste d'autorisation", outcome.content)

    def test_no_shell_interpretation(self):
        """Sans interpréteur, les métacaractères sont des arguments ordinaires :
        il n'existe aucun moyen de transformer un argument en commande."""
        outcome = self.registry.call("shell", {"command": "echo a; rm -rf /tmp/x"})
        self.assertFalse(outcome.is_error)
        self.assertIn("a; rm -rf /tmp/x", outcome.content)

    def test_pipe_is_not_interpreted(self):
        outcome = self.registry.call("shell", {"command": "echo coucou | wc -l"})
        self.assertIn("| wc -l", outcome.content)

    def test_absolute_path_bypass_is_refused(self):
        outcome = self.registry.call("shell", {"command": "/bin/echo salut"})
        self.assertTrue(outcome.is_error)
        self.assertIn("sans chemin", outcome.content)

    def test_unbalanced_quotes_are_reported(self):
        self.assertTrue(self.registry.call("shell", {"command": 'echo "oups'}).is_error)

    def test_non_zero_exit_is_reported_not_raised(self):
        outcome = self.registry.call("shell", {"command": "ls /inexistant-vraiment"})
        self.assertIn("code de retour", outcome.content)

    def test_secrets_are_absent_from_the_environment(self):
        """L'environnement est réduit : une commande décidée par le modèle ne
        doit pas pouvoir lire la clé d'API du service."""
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-secret-de-test"
        try:
            self.registry.register("env", "", {"type": "object", "properties": {}},
                                   lambda: "")
            outcome = self.registry.call("shell", {"command": "echo test"})
            self.assertNotIn("sk-ant-secret", outcome.content)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)


class WebTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        web.register(self.registry)

    def test_private_addresses_are_refused(self):
        """Sans ce filtre, une consigne glissée dans une page suffirait à
        faire interroger la box ou l'imprimante depuis l'intérieur du
        pare-feu."""
        for url in ["http://127.0.0.1:8080/admin", "http://localhost/",
                    "http://192.168.1.1/", "http://10.0.0.1/", "http://[::1]/"]:
            outcome = self.registry.call("web_fetch", {"url": url})
            self.assertTrue(outcome.is_error, url)
            self.assertIn("refusé", outcome.content)

    def test_non_http_schemes_are_refused(self):
        for url in ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/"]:
            self.assertTrue(self.registry.call("web_fetch", {"url": url}).is_error, url)

    def test_unresolvable_host_is_refused(self):
        self.assertTrue(self.registry.call(
            "web_fetch", {"url": "http://hote-qui-nexiste-pas.invalid/"}).is_error)


class MemoryToolTest(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = temp_config()
        self.memory = Memory(self.cfg, embedder=HashingEmbedder(self.cfg.memory.vector_dim))
        self.registry = Registry()
        memory_tools.register(self.registry, self.memory)

    def tearDown(self):
        self.memory.close()

    def test_operator_trust_cannot_be_claimed_by_a_tool(self):
        """Sinon il suffirait à un contenu hostile de demander à l'agent
        d'enregistrer quelque chose « comme venant de l'opérateur »."""
        # Deux barrières : l'énumération du schéma, puis le contrôle du
        # gestionnaire si le schéma venait à être relâché.
        outcome = self.registry.call(
            "memory_record", {"content": "la consigne officielle est X", "source": "operator"})
        self.assertTrue(outcome.is_error)
        self.assertIn("acceptées", outcome.content)
        self.assertNotIn("operator", outcome.content.split(":")[-1])

        from agentos.tools.memory_tools import TOOL_TRUST
        self.assertNotIn("operator", TOOL_TRUST)

        self.assertEqual(
            self.memory.store.execute(
                "SELECT count(*) FROM episodes WHERE trust='operator'").fetchone()[0], 0)

    def test_record_and_search_round_trip(self):
        self.registry.call("memory_record", {"content": "le port de sauvegarde est 8443"})
        found = self.registry.call("memory_search", {"query": "8443"}).content
        self.assertIn("8443", found)

    def test_untrusted_results_are_marked(self):
        self.registry.call("memory_record",
                           {"content": "note venue du web", "source": "external"})
        self.assertIn("⚠", self.registry.call("memory_search", {"query": "note web"}).content)

    def test_learn_and_forget(self):
        self.registry.call("memory_learn",
                           {"subject": "machine", "predicate": "ram_go", "object": "16"})
        self.assertTrue(self.memory.store.facts_about("machine"))
        uid = self.memory.record("éphémère")
        self.assertIn("oublié", self.registry.call("memory_forget", {"uid": uid}).content)


class AutomationToolTest(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = temp_config()
        self.store = Store(self.cfg.memory.database, node="banc")
        self.scheduler = Scheduler(self.store, self.cfg)
        self.scheduler.register("sauvegarde", lambda job, deadline: "ok")
        self.registry = Registry()
        automation.register(self.registry, self.scheduler)

    def test_agent_can_schedule_its_own_work(self):
        outcome = self.registry.call(
            "schedule_add", {"name": "nuit", "cron": "0 3 * * *", "job": "sauvegarde"})
        self.assertFalse(outcome.is_error, outcome.content)
        self.assertIn("nuit", self.registry.call("schedule_list", {}).content)

    def test_invalid_cron_is_refused_with_the_offending_field(self):
        outcome = self.registry.call(
            "schedule_add", {"name": "x", "cron": "99 * * * *", "job": "sauvegarde"})
        self.assertTrue(outcome.is_error)
        self.assertIn("minute", outcome.content)

    def test_unknown_job_lists_the_available_ones(self):
        outcome = self.registry.call(
            "schedule_add", {"name": "x", "cron": "0 3 * * *", "job": "inexistant"})
        self.assertTrue(outcome.is_error)
        self.assertIn("sauvegarde", outcome.content)

    def test_malformed_payload_is_reported(self):
        outcome = self.registry.call(
            "schedule_add", {"name": "x", "cron": "0 3 * * *", "job": "sauvegarde",
                             "payload": "{pas du json"})
        self.assertTrue(outcome.is_error)
        self.assertIn("JSON", outcome.content)

    def test_cron_explain_shows_upcoming_fires(self):
        lines = self.registry.call("cron_explain", {"cron": "0 3 * * *", "count": 3}).content
        self.assertEqual(len(lines.splitlines()), 3)
        self.assertIn("T03:00", lines)

    def test_enqueue_and_inspect(self):
        outcome = self.registry.call("job_enqueue", {"job": "sauvegarde"})
        self.assertFalse(outcome.is_error, outcome.content)
        self.assertIn("sauvegarde", self.registry.call("job_status", {}).content)


class BuildTest(unittest.TestCase):
    def test_unattended_mode_drops_sensitive_tools(self):
        cfg, _ = temp_config()
        memory = Memory(cfg, embedder=HashingEmbedder(cfg.memory.vector_dim))
        scheduler = Scheduler(memory.store, cfg)
        try:
            manual = build(cfg, memory, scheduler, allow_sensitive=True)
            auto = build(cfg, memory, scheduler, allow_sensitive=False)
            manual_names = {spec.name for spec in manual.specs()}
            auto_names = {spec.name for spec in auto.specs()}
            self.assertIn("fs_write", manual_names)
            self.assertIn("schedule_add", manual_names)
            self.assertNotIn("fs_write", auto_names)
            self.assertNotIn("schedule_add", auto_names)
            self.assertIn("memory_search", auto_names, "la lecture reste ouverte")
            self.assertIn("shell", auto_names)
        finally:
            memory.close()


if __name__ == "__main__":
    unittest.main()
