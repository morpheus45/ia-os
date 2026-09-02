"""Registre d'outils : déclaration, validation, appel.

Les arguments produits par un modèle sont des données non fiables — un
petit modèle quantifié en invente régulièrement la forme. La validation
faite ici tient donc lieu de frontière : un gestionnaire d'outil ne reçoit
jamais que ce qu'il a déclaré attendre, et une erreur de forme revient au
modèle sous forme de message plutôt que de faire tomber la boucle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..brain.base import ToolSpec

log = logging.getLogger("agentos.tools")

#: Types JSON Schema reconnus, avec le type Python correspondant.
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


class ToolError(Exception):
    """Erreur destinée au modèle : elle décrit ce qu'il faut corriger."""


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    #: JSON Schema de l'objet d'arguments.
    schema: dict[str, Any]
    handler: Callable[..., Any]
    #: Un outil marqué sensible est refusé quand l'agent tourne sans surveillance.
    sensitive: bool = False
    calls: int = 0
    failures: int = 0
    total_ms: float = 0.0

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.schema)


@dataclass
class Outcome:
    """Résultat d'un appel, tel qu'il repart vers le modèle."""

    content: str
    is_error: bool = False
    duration_ms: float = 0.0


class Registry:
    """Ensemble des outils offerts au modèle."""

    def __init__(self, *, allow_sensitive: bool = True) -> None:
        self._tools: dict[str, Tool] = {}
        self.allow_sensitive = allow_sensitive

    def register(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        *,
        sensitive: bool = False,
    ) -> Tool:
        if name in self._tools:
            raise ValueError(f"outil « {name} » déjà enregistré")
        tool = Tool(name, description, schema, handler, sensitive)
        self._tools[name] = tool
        return tool

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        """Outils présentés au modèle, dans un ordre stable.

        L'ordre est fixe pour que le préfixe de requête reste identique
        d'un appel à l'autre — c'est ce qui permet au cache de prompt de
        l'API de s'appliquer.
        """
        return [
            tool.spec()
            for _, tool in sorted(self._tools.items())
            if self.allow_sensitive or not tool.sensitive
        ]

    # -- validation ------------------------------------------------------

    def validate(self, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        """Vérifie les arguments contre le schéma et applique les défauts."""
        if not isinstance(arguments, dict):
            raise ToolError(f"les arguments de « {tool.name} » doivent former un objet JSON")

        properties = tool.schema.get("properties", {})
        required = tool.schema.get("required", [])

        missing = [key for key in required if key not in arguments]
        if missing:
            raise ToolError(
                f"argument(s) manquant(s) pour « {tool.name} » : {', '.join(missing)}"
            )

        unknown = [key for key in arguments if key not in properties]
        if unknown:
            raise ToolError(
                f"argument(s) inconnu(s) pour « {tool.name} » : {', '.join(unknown)} — "
                f"attendus : {', '.join(properties) or 'aucun'}"
            )

        clean: dict[str, Any] = {}
        for key, value in arguments.items():
            expected = properties[key].get("type")
            allowed = _TYPES.get(expected) if expected else None
            if allowed and not isinstance(value, allowed):
                # Un modèle renvoie souvent « 3 » là où un entier est attendu :
                # convertir vaut mieux que rejeter, tant que c'est sans perte.
                value = _coerce(value, expected, tool.name, key)
            choices = properties[key].get("enum")
            if choices and value not in choices:
                raise ToolError(
                    f"« {key} » vaut {value!r} ; valeurs acceptées : {', '.join(map(str, choices))}"
                )
            clean[key] = value

        for key, spec in properties.items():
            if key not in clean and "default" in spec:
                clean[key] = spec["default"]
        return clean

    # -- appel -----------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> Outcome:
        """Exécute un outil. Toute erreur revient au modèle, aucune ne remonte."""
        started = time.monotonic()
        tool = self._tools.get(name)
        if tool is None:
            return Outcome(
                f"outil « {name} » inconnu — disponibles : {', '.join(self.names())}",
                is_error=True,
            )
        if tool.sensitive and not self.allow_sensitive:
            return Outcome(
                f"outil « {name} » désactivé dans ce mode d'exécution", is_error=True
            )

        try:
            clean = self.validate(tool, arguments)
            result = tool.handler(**clean)
            content = result if isinstance(result, str) else _render(result)
            failed = False
        except ToolError as exc:
            content, failed = str(exc), True
        except Exception as exc:  # noqa: BLE001 - un outil ne fait jamais tomber la boucle
            log.exception("outil %s en erreur", name)
            content, failed = f"{type(exc).__name__}: {exc}", True

        elapsed = (time.monotonic() - started) * 1000
        tool.calls += 1
        tool.total_ms += elapsed
        tool.failures += int(failed)
        return Outcome(content, is_error=failed, duration_ms=elapsed)

    def stats(self) -> list[dict[str, Any]]:
        return [
            {
                "nom": tool.name,
                "appels": tool.calls,
                "echecs": tool.failures,
                "moyenne_ms": round(tool.total_ms / tool.calls, 1) if tool.calls else 0.0,
                "sensible": tool.sensitive,
            }
            for tool in sorted(self._tools.values(), key=lambda t: t.name)
        ]


def _coerce(value: Any, expected: str, tool_name: str, key: str) -> Any:
    """Convertit un argument mal typé quand c'est sans ambiguïté."""
    try:
        if expected == "integer" and isinstance(value, (str, float)):
            converted = int(value)
            if isinstance(value, float) and converted != value:
                raise ValueError("perte de précision")
            return converted
        if expected == "number" and isinstance(value, str):
            return float(value)
        if expected == "boolean" and isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "vrai", "1", "oui", "yes"}:
                return True
            if lowered in {"false", "faux", "0", "non", "no"}:
                return False
        if expected == "string" and isinstance(value, (int, float, bool)):
            return str(value)
        if expected == "array" and isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
    except (TypeError, ValueError):
        pass
    raise ToolError(
        f"« {key} » de « {tool_name} » doit être de type {expected}, reçu "
        f"{type(value).__name__}"
    )


def _render(value: Any) -> str:
    """Met un résultat sous une forme lisible par le modèle."""
    import json

    if value is None:
        return "(aucun résultat)"
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)
