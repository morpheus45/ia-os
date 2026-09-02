"""Boucle de l'agent : enchaînement des outils, bornes, traçabilité."""

from __future__ import annotations

import time
import unittest

from agentos.agent import Agent
from agentos.brain.anthropic import AnthropicBrain
from agentos.brain.router import BrainRouter
from agentos.memory import Memory
from agentos.memory.embed import HashingEmbedder
from agentos.tools import Registry

from .fakes import ModelServer, temp_config


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = temp_config()
        self.cfg.agent.max_turns = 4
        self.memory = Memory(self.cfg, embedder=HashingEmbedder(self.cfg.memory.vector_dim))
        self.server = ModelServer().__enter__()
        self.brain = AnthropicBrain("clef", "claude-sonnet-5",
                                    base_url=f"{self.server.base}/v1/messages", attempts=1)
        self.registry = Registry()
        self.calls: list[dict] = []
        self.registry.register(
            "mesurer", "mesure quelque chose",
            {"type": "object", "properties": {"quoi": {"type": "string"}},
             "required": ["quoi"]},
            lambda quoi: self.calls.append({"quoi": quoi}) or f"{quoi} = 42",
        )
        self.agent = Agent(self.cfg, self.memory, self.brain, self.registry)

    def tearDown(self):
        self.server.__exit__(None, None, None)
        self.memory.close()

    # -- déroulé normal --------------------------------------------------

    def test_answers_without_tools(self):
        self.server.anthropic_reply = self.server.default_anthropic("Il fait 16 Go.")
        result = self.agent.run("combien de RAM ?")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Il fait 16 Go.")
        self.assertEqual(len(result.turns), 1)

    def test_runs_a_tool_then_concludes(self):
        self.server.anthropic_script = [
            self.server.anthropic_tool("mesurer", {"quoi": "disque"}, text="Je mesure."),
            self.server.default_anthropic("Le disque fait 42."),
        ]
        result = self.agent.run("mesure le disque")
        self.assertTrue(result.ok)
        self.assertEqual(self.calls, [{"quoi": "disque"}])
        self.assertEqual(len(result.turns), 2)
        self.assertEqual(result.turns[0].tool_calls, ["mesurer"])
        self.assertEqual(result.text, "Le disque fait 42.")

    def test_tool_result_is_sent_back_to_the_model(self):
        self.server.anthropic_script = [
            self.server.anthropic_tool("mesurer", {"quoi": "disque"}),
            self.server.default_anthropic("fini"),
        ]
        self.agent.run("mesure")
        sent = self.server.seen[-1][1]
        blocks = [b for message in sent["messages"] for b in message["content"]]
        results = [b for b in blocks if b["type"] == "tool_result"]
        self.assertEqual(len(results), 1)
        self.assertIn("disque = 42", results[0]["content"])
        self.assertFalse(results[0]["is_error"])

    def test_tool_error_is_reported_to_the_model_not_raised(self):
        self.server.anthropic_script = [
            self.server.anthropic_tool("mesurer", {}),          # argument manquant
            self.server.default_anthropic("je corrige"),
        ]
        result = self.agent.run("mesure")
        self.assertTrue(result.ok)
        sent = self.server.seen[-1][1]
        results = [b for message in sent["messages"] for b in message["content"]
                   if b["type"] == "tool_result"]
        self.assertTrue(results[0]["is_error"])
        self.assertIn("manquant", results[0]["content"])

    # -- bornes ----------------------------------------------------------

    def test_stops_after_max_turns(self):
        """Un modèle qui part en boucle ne s'arrête pas de lui-même."""
        self.server.anthropic_reply = self.server.anthropic_tool("mesurer", {"quoi": "x"})
        result = self.agent.run("boucle", max_turns=3)
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_by, "tours")
        self.assertEqual(len(result.turns), 3)
        self.assertIn("3 tours", result.text)

    def test_stops_at_deadline(self):
        self.server.anthropic_reply = self.server.anthropic_tool("mesurer", {"quoi": "x"})
        result = self.agent.run("trop long", deadline=time.time() - 1)
        self.assertEqual(result.stopped_by, "échéance")
        self.assertEqual(result.turns, [])

    def test_model_outage_ends_the_task_cleanly(self):
        self.server.fail_anthropic = 99
        router = BrainRouter([self.brain], failure_threshold=9)
        agent = Agent(self.cfg, self.memory, router, self.registry)
        result = agent.run("tâche")
        self.assertEqual(result.stopped_by, "erreur")
        self.assertIn("aucun modèle", result.text)

    # -- traçabilité -----------------------------------------------------

    def test_every_step_is_recorded(self):
        """C'est la seule trace disponible pour reconstituer ce que la
        machine a fait pendant la nuit."""
        self.server.anthropic_script = [
            self.server.anthropic_tool("mesurer", {"quoi": "disque"}),
            self.server.default_anthropic("Le disque fait 42."),
        ]
        result = self.agent.run("mesure le disque")
        kinds = [row["kind"] for row in self.memory.recent(20, session=result.session)]
        for expected in ("tâche", "action", "résultat", "réponse"):
            self.assertIn(expected, kinds)

    def test_task_is_operator_trusted_but_tool_output_is_not(self):
        """La consigne vient de l'opérateur ; ce qu'un outil rapporte vient du
        monde extérieur et ne doit jamais valoir consigne."""
        self.server.anthropic_script = [
            self.server.anthropic_tool("mesurer", {"quoi": "disque"}),
            self.server.default_anthropic("fini"),
        ]
        result = self.agent.run("mesure le disque")
        rows = {row["kind"]: row["trust"]
                for row in self.memory.recent(20, session=result.session)}
        self.assertEqual(rows["tâche"], "operator")
        self.assertEqual(rows["résultat"], "external")
        self.assertEqual(rows["action"], "agent")

    def test_interruption_is_recorded_as_an_incident(self):
        self.server.anthropic_reply = self.server.anthropic_tool("mesurer", {"quoi": "x"})
        result = self.agent.run("boucle", max_turns=2)
        kinds = [row["kind"] for row in self.memory.recent(20, session=result.session)]
        self.assertIn("incident", kinds)

    # -- contexte --------------------------------------------------------

    def test_memory_is_recalled_into_the_prompt(self):
        self.memory.record("Le disque de sauvegarde est /mnt/backup", trust="operator")
        self.server.anthropic_reply = self.server.default_anthropic("vu")
        self.agent.run("où est le disque de sauvegarde ?")
        prompt = self.server.seen[-1][1]["messages"][0]["content"][0]["text"]
        self.assertIn("/mnt/backup", prompt)
        self.assertIn("Mémoire établie par l'opérateur", prompt)

    def test_recall_can_be_disabled(self):
        self.memory.record("Le disque de sauvegarde est /mnt/backup", trust="operator")
        self.server.anthropic_reply = self.server.default_anthropic("vu")
        self.agent.run("où est le disque ?", recall=False)
        prompt = self.server.seen[-1][1]["messages"][0]["content"][0]["text"]
        self.assertNotIn("/mnt/backup", prompt)

    def test_system_prompt_states_the_data_not_instruction_rule(self):
        prompt = self.agent.system_prompt()
        self.assertIn("jamais une consigne", prompt)
        self.assertIn("memoire_non_verifiee", prompt)
        self.assertIn("mesurer", prompt)

    def test_token_usage_is_accumulated(self):
        self.server.anthropic_script = [
            self.server.anthropic_tool("mesurer", {"quoi": "x"}),
            self.server.default_anthropic("fini"),
        ]
        result = self.agent.run("mesure")
        self.assertEqual(result.input_tokens, 20)
        self.assertGreater(result.output_tokens, 0)


if __name__ == "__main__":
    unittest.main()
