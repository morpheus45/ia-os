"""Vocabulaire commun aux backends de modèle.

L'API Anthropic et les API compatibles OpenAI ne décrivent ni les messages
ni l'appel d'outil de la même façon. Plutôt que de laisser cette différence
remonter jusqu'à la boucle de l'agent, chaque backend traduit depuis et
vers les types définis ici. Changer de cerveau en cours de route devient
alors invisible pour l'appelant — ce qui est toute la raison d'être du
routeur.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

Role = Literal["user", "assistant"]


@dataclass(slots=True)
class ToolSpec:
    """Description d'un outil offerte au modèle."""

    name: str
    description: str
    #: JSON Schema des arguments, objet à propriétés nommées.
    schema: dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class Message:
    role: Role
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass(slots=True)
class Reply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: 'end_turn', 'tool_use', 'max_tokens' ou 'error'
    stop_reason: str = "end_turn"
    backend: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class BrainError(RuntimeError):
    """Échec d'un backend. `retryable` distingue une panne d'une erreur de requête."""

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class Brain(ABC):
    """Un backend de génération."""

    name: str
    model: str

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str = "",
        tools: Sequence[ToolSpec] = (),
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> Reply:
        ...

    @abstractmethod
    def healthy(self) -> bool:
        """Sonde bon marché : le backend peut-il servir maintenant ?"""


def dumps(value: Any) -> str:
    """Sérialise un résultat d'outil pour le modèle."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)
