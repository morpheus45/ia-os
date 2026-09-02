"""Assemblage du cerveau depuis la configuration."""

from __future__ import annotations

import logging

from .anthropic import AnthropicBrain
from .base import Brain, BrainError, Message, Reply, ToolCall, ToolResult, ToolSpec
from .local import LocalBrain
from .router import BrainRouter

log = logging.getLogger("agentos.brain")

__all__ = [
    "Brain", "BrainError", "BrainRouter", "Message", "Reply",
    "ToolCall", "ToolResult", "ToolSpec", "build",
]


def build(config) -> BrainRouter:
    """Construit le routeur dans l'ordre de préférence configuré."""
    factories = {
        "anthropic": lambda: AnthropicBrain(
            config.anthropic_key,
            config.brain.anthropic_model,
            timeout=config.brain.timeout_s,
        ),
        "local": lambda: LocalBrain(
            config.brain.local_url,
            config.brain.local_model,
        ),
    }
    backends: list[Brain] = []
    for name in config.brain.order:
        factory = factories.get(name)
        if factory is None:
            log.warning("backend inconnu ignoré : %s", name)
            continue
        backends.append(factory())
    if not backends:
        raise SystemExit(
            f"aucun backend valide dans brain.order={config.brain.order!r} — "
            f"valeurs possibles : {', '.join(factories)}"
        )
    return BrainRouter(
        backends,
        failure_threshold=config.brain.failure_threshold,
        cooldown_s=config.brain.cooldown_s,
    )
