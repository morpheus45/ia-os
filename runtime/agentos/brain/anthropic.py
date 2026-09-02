"""Backend API Claude, parlé directement en HTTP.

Pas de SDK : une dépendance de plus signifie une mise à jour de plus à
gérer sur une machine censée tourner sans surveillance, pour une surface
d'API dont on n'utilise qu'une route.
"""

from __future__ import annotations

from typing import Any, Sequence

from .. import net
from .base import Brain, BrainError, Message, Reply, ToolCall, ToolSpec

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicBrain(Brain):
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5",
        *,
        timeout: float = 120.0,
        base_url: str = API_URL,
        attempts: int = 2,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = base_url
        # Peu de tentatives internes : c'est le routeur qui doit basculer
        # vers le backend suivant. S'acharner ici ne ferait que retarder la
        # bascule de plusieurs délais d'expiration cumulés.
        self.attempts = attempts

    # -- traduction ------------------------------------------------------

    @staticmethod
    def _encode(messages: Sequence[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in messages:
            blocks: list[dict[str, Any]] = []
            # Les résultats d'outil viennent en tête : l'API exige qu'ils
            # ouvrent le message utilisateur qui répond à un `tool_use`.
            for result in message.tool_results:
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": result.call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                })
            if message.text:
                blocks.append({"type": "text", "text": message.text})
            for call in message.tool_calls:
                blocks.append({
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                })
            if blocks:
                out.append({"role": message.role, "content": blocks})
        return out

    @staticmethod
    def _encode_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {"name": tool.name, "description": tool.description, "input_schema": tool.schema}
            for tool in tools
        ]

    # -- appel -----------------------------------------------------------

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str = "",
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> Reply:
        if not self.api_key:
            raise BrainError("clé ANTHROPIC_API_KEY absente", retryable=False)

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": self._encode(messages),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self._encode_tools(tools)

        try:
            body = net.request_json(
                self.base_url,
                payload=payload,
                headers={"x-api-key": self.api_key, "anthropic-version": API_VERSION},
                timeout=self.timeout,
                attempts=self.attempts,
            )
        except net.HttpError as exc:
            raise BrainError(str(exc), retryable=exc.retryable, status=exc.status) from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in body.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input") or {},
                ))

        usage = body.get("usage") or {}
        return Reply(
            text="".join(text_parts).strip(),
            tool_calls=calls,
            stop_reason=body.get("stop_reason") or "end_turn",
            backend=self.name,
            model=body.get("model", self.model),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )

    def healthy(self) -> bool:
        """Une clé absente rend le backend inutilisable ; sinon on suppose
        l'API joignable et on laisse le routeur constater l'échec réel.

        Sonder l'API pour de bon coûterait un appel facturé à chaque
        vérification, pour une information que le prochain appel donnera
        gratuitement.
        """
        return bool(self.api_key)
