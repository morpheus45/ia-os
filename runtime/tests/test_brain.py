"""Traduction des protocoles et bascule entre backends."""

from __future__ import annotations

import time
import unittest

from agentos.brain.anthropic import AnthropicBrain
from agentos.brain.base import Message, ToolCall, ToolResult, ToolSpec
from agentos.brain.local import LocalBrain
from agentos.brain.router import BrainRouter

from .fakes import ModelServer

TOOLS = [ToolSpec(
    "memory_search", "cherche en mémoire",
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
)]

CONVO = [
    Message("user", "quel disque ?"),
    Message("assistant", "je regarde", tool_calls=[ToolCall("tu_0", "memory_search", {"query": "x"})]),
    Message("user", tool_results=[ToolResult("tu_0", "500 Go")]),
]


class AnthropicEncodingTest(unittest.TestCase):
    def test_encodes_system_tools_and_tool_blocks(self):
        with ModelServer() as server:
            server.anthropic_reply = server.anthropic_tool("memory_search", {"query": "disque"})
            brain = AnthropicBrain("clef-test", "claude-sonnet-5", base_url=f"{server.base}/v1/messages")
            reply = brain.complete(CONVO, system="tu es agent-os", tools=TOOLS, max_tokens=1000)

            self.assertTrue(reply.wants_tools)
            self.assertEqual(reply.tool_calls[0].arguments, {"query": "disque"})
            self.assertEqual(reply.stop_reason, "tool_use")
            self.assertEqual(reply.input_tokens, 10)

            sent = server.seen[-1][1]
            # `system` est un champ à part, il ne doit pas entrer dans messages.
            self.assertEqual(sent["system"], "tu es agent-os")
            self.assertEqual(len(sent["messages"]), 3)
            self.assertEqual(sent["tools"][0]["input_schema"]["required"], ["query"])
            self.assertEqual(sent["messages"][1]["content"][1]["type"], "tool_use")
            # L'API exige que le tool_result ouvre le message qui y répond.
            self.assertEqual(sent["messages"][2]["content"][0]["type"], "tool_result")

    def test_missing_key_is_not_healthy(self):
        self.assertFalse(AnthropicBrain("", "m").healthy())


class LocalEncodingTest(unittest.TestCase):
    def test_encodes_openai_dialect(self):
        with ModelServer() as server:
            server.local_reply = {
                "model": "modele-local",
                "choices": [{"finish_reason": "tool_calls", "message": {
                    "content": "je cherche",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {
                            "name": "memory_search", "arguments": '{"query": "disque"}'}},
                    ]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
            brain = LocalBrain(f"{server.base}/v1", "modele-local")
            reply = brain.complete(CONVO, system="sys", tools=TOOLS)

            self.assertEqual(reply.tool_calls[0].arguments, {"query": "disque"})
            self.assertEqual(reply.stop_reason, "tool_use")

            sent = server.seen[-1][1]
            self.assertEqual([m["role"] for m in sent["messages"]],
                             ["system", "user", "assistant", "tool"])
            self.assertEqual(sent["messages"][3]["tool_call_id"], "tu_0")
            self.assertEqual(sent["tools"][0]["type"], "function")

    def test_malformed_tool_arguments_do_not_raise(self):
        """Un petit modèle quantifié produit du JSON approximatif : la boucle
        doit pouvoir le signaler au modèle, pas mourir dessus."""
        with ModelServer() as server:
            server.local_reply = {
                "model": "m", "choices": [{"finish_reason": "tool_calls", "message": {
                    "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
                        "name": "broken", "arguments": "{ceci n'est pas du json"}}]}}],
            }
            reply = LocalBrain(f"{server.base}/v1").complete([Message("user", "va")])
            self.assertEqual(reply.tool_calls[0].arguments, {})


class RouterTest(unittest.TestCase):
    def test_falls_back_then_opens_breaker_then_recovers(self):
        with ModelServer() as server:
            server.fail_anthropic = 99
            anthropic = AnthropicBrain("clef", "claude-sonnet-5",
                                       base_url=f"{server.base}/v1/messages", attempts=1)
            local = LocalBrain(f"{server.base}/v1", "modele-local", attempts=1)
            router = BrainRouter([anthropic, local], failure_threshold=2, cooldown_s=30)

            # 1er appel : Anthropic échoue, le local prend le relais.
            self.assertEqual(router.complete(CONVO).backend, "local")
            self.assertFalse(router.status()["anthropic"]["disjoncteur_ouvert"])

            # 2e échec consécutif : le disjoncteur s'ouvre.
            self.assertEqual(router.complete(CONVO).backend, "local")
            self.assertTrue(router.status()["anthropic"]["disjoncteur_ouvert"])
            self.assertTrue(router.status()["local"]["actif"])

            # Disjoncteur ouvert : plus aucun appel distant n'est tenté.
            before = len([p for p, _ in server.seen if p == "/v1/messages"])
            started = time.monotonic()
            router.complete(CONVO)
            elapsed = time.monotonic() - started
            after = len([p for p, _ in server.seen if p == "/v1/messages"])
            self.assertEqual(before, after, "le backend écarté ne doit plus être appelé")
            self.assertLess(elapsed, 0.5)

            # Rétablissement une fois le délai écoulé.
            server.fail_anthropic = 0
            router._breakers["anthropic"].opened_until = 0.0
            self.assertEqual(router.complete(CONVO).backend, "anthropic")
            self.assertEqual(router.status()["anthropic"]["echecs_consecutifs"], 0)

    def test_all_backends_down_raises_with_detail(self):
        with ModelServer() as server:
            server.fail_anthropic = 99
            server.fail_local = 99
            router = BrainRouter(
                [AnthropicBrain("k", base_url=f"{server.base}/v1/messages", attempts=1),
                 LocalBrain(f"{server.base}/v1", attempts=1)],
                failure_threshold=5,
            )
            with self.assertRaises(Exception) as caught:
                router.complete(CONVO)
            self.assertIn("anthropic", str(caught.exception))
            self.assertIn("local", str(caught.exception))

    def test_rejects_empty_backend_list(self):
        with self.assertRaises(ValueError):
            BrainRouter([])


if __name__ == "__main__":
    unittest.main()
