"""Routeur de cerveaux : préférence ordonnée, bascule sur panne.

La machine doit continuer à travailler quand le réseau tombe, et retrouver
le meilleur modèle quand il revient — sans intervention. Le routeur essaie
les backends dans l'ordre configuré et isole temporairement celui qui
échoue de façon répétée.

Le disjoncteur sert surtout à borner la latence : sans lui, chaque tâche
paierait le délai d'expiration complet du backend distant avant de se
rabattre sur le local, ce qui rend la machine inutilisable pendant une
coupure réseau plutôt que simplement plus lente.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Sequence

from .base import Brain, BrainError, Message, Reply, ToolSpec

log = logging.getLogger("agentos.brain")


@dataclass
class _Breaker:
    """État d'un backend : échecs consécutifs et fin de mise à l'écart."""

    failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""
    calls: int = 0
    successes: int = 0

    def open(self) -> bool:
        return time.monotonic() < self.opened_until


class BrainRouter(Brain):
    """Présente plusieurs backends comme un seul."""

    name = "router"

    def __init__(
        self,
        backends: Sequence[Brain],
        *,
        failure_threshold: int = 3,
        cooldown_s: float = 60.0,
    ) -> None:
        if not backends:
            raise ValueError("aucun backend de modèle configuré")
        self.backends = list(backends)
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._breakers = {backend.name: _Breaker() for backend in self.backends}
        self._lock = threading.Lock()
        self.active = self.backends[0].name

    @property
    def model(self) -> str:  # type: ignore[override]
        for backend in self.backends:
            if backend.name == self.active:
                return backend.model
        return self.backends[0].model

    # -- sélection -------------------------------------------------------

    def _candidates(self) -> list[Brain]:
        """Backends jugés utilisables, dans l'ordre de préférence.

        Si le disjoncteur les a tous ouverts, on les rend quand même : une
        tentative vouée à l'échec vaut mieux qu'un refus certain, et c'est
        elle qui refermera le disjoncteur quand le service reviendra.
        """
        with self._lock:
            ready = [b for b in self.backends if not self._breakers[b.name].open()]
        usable = [b for b in ready if b.healthy()]
        if usable:
            return usable
        return ready or list(self.backends)

    def _record(self, name: str, *, ok: bool, error: str = "") -> None:
        with self._lock:
            breaker = self._breakers[name]
            breaker.calls += 1
            if ok:
                breaker.failures = 0
                breaker.opened_until = 0.0
                breaker.successes += 1
                breaker.last_error = ""
                self.active = name
                return
            breaker.failures += 1
            breaker.last_error = error[:300]
            if breaker.failures >= self.failure_threshold:
                breaker.opened_until = time.monotonic() + self.cooldown_s
                log.warning(
                    "backend %s écarté %ds après %d échecs : %s",
                    name, int(self.cooldown_s), breaker.failures, error[:200],
                )

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
        errors: list[str] = []
        for backend in self._candidates():
            try:
                reply = backend.complete(
                    messages,
                    system=system,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except BrainError as exc:
                self._record(backend.name, ok=False, error=str(exc))
                errors.append(f"{backend.name}: {exc}")
                if not exc.retryable and exc.status in (400, 422):
                    # Requête malformée : les autres backends la rejetteront
                    # aussi, inutile de propager l'erreur à toute la chaîne.
                    raise
                continue
            except Exception as exc:  # noqa: BLE001
                self._record(backend.name, ok=False, error=repr(exc))
                errors.append(f"{backend.name}: {exc!r}")
                continue

            self._record(backend.name, ok=True)
            return reply

        raise BrainError("aucun backend n'a répondu — " + " | ".join(errors), retryable=True)

    def healthy(self) -> bool:
        return any(backend.healthy() for backend in self.backends)

    def status(self) -> dict[str, dict[str, object]]:
        now = time.monotonic()
        with self._lock:
            return {
                backend.name: {
                    "modele": backend.model,
                    "actif": backend.name == self.active,
                    "disponible": backend.healthy(),
                    "disjoncteur_ouvert": self._breakers[backend.name].open(),
                    "reouverture_dans_s": max(0, round(self._breakers[backend.name].opened_until - now)),
                    "echecs_consecutifs": self._breakers[backend.name].failures,
                    "appels": self._breakers[backend.name].calls,
                    "succes": self._breakers[backend.name].successes,
                    "derniere_erreur": self._breakers[backend.name].last_error,
                }
                for backend in self.backends
            }
