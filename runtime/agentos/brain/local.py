"""Backend local : llama.cpp ou Ollama, via leur route compatible OpenAI.

C'est ce qui rend la machine autonome au sens strict — plus de réseau, plus
de compte, plus de facturation. Sur 16 Go de RAM sans GPU, la cible
réaliste est un modèle de 7 à 14 milliards de paramètres quantifié en 4
bits, qui délivre quelques jetons par seconde. Assez pour de
l'automatisation de fond, pas pour du dialogue interactif.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .. import net
from .base import Brain, BrainError, Message, Reply, ToolCall, ToolSpec


class LocalBrain(Brain):
    name = "local"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080/v1",
        model: str = "qwen2.5-7b-instruct-q4_k_m",
        *,
        timeout: float = 300.0,
        attempts: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.attempts = attempts
        # Générer sur CPU est lent : un délai calé sur celui de l'API
        # distante ferait échouer des réponses parfaitement en cours.
        self.timeout = timeout

    # -- traduction ------------------------------------------------------

    @staticmethod
    def _encode(messages: Sequence[Message], system: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for message in messages:
            # Le format OpenAI veut un message `tool` distinct par résultat,
            # là où l'API Claude les groupe dans un message utilisateur.
            for result in message.tool_results:
                out.append({
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                })
            if message.tool_calls:
                out.append({
                    "role": "assistant",
                    "content": message.text or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in message.tool_calls
                    ],
                })
            elif message.text:
                out.append({"role": message.role, "content": message.text})
        return out

    @staticmethod
    def _encode_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.schema,
                },
            }
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
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._encode(messages, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = self._encode_tools(tools)

        try:
            body = net.request_json(
                f"{self.base_url}/chat/completions",
                payload=payload,
                timeout=self.timeout,
                attempts=self.attempts,
            )
        except net.HttpError as exc:
            raise BrainError(str(exc), retryable=exc.retryable, status=exc.status) from exc

        choices = body.get("choices") or []
        if not choices:
            raise BrainError("réponse sans « choices » du serveur local", retryable=True)
        choice = choices[0]
        message = choice.get("message") or {}

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            calls.append(ToolCall(
                id=raw.get("id") or f"call_{len(calls)}",
                name=function.get("name", ""),
                arguments=_parse_arguments(function.get("arguments")),
            ))

        usage = body.get("usage") or {}
        return Reply(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            stop_reason=_STOP.get(choice.get("finish_reason", ""), "end_turn"),
            backend=self.name,
            model=body.get("model", self.model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )

    def healthy(self) -> bool:
        return net.reachable(self.base_url, timeout=1.5)


_STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Décode les arguments d'un appel d'outil.

    Un petit modèle quantifié produit régulièrement du JSON approximatif.
    Renvoyer un dict vide laisse la boucle de l'agent signaler l'erreur au
    modèle et retenter, ce qui aboutit plus souvent qu'un abandon immédiat.
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
