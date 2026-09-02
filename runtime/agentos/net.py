"""Client HTTP minimal, partagé par les backends de modèle et les outils.

Volontairement bâti sur `urllib` : le runtime doit démarrer sur une machine
fraîchement installée, avant tout `pip install`, et une dépendance de moins
dans le chemin de boot est une panne de moins au premier démarrage.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

log = logging.getLogger("agentos.net")

#: Codes qui méritent une nouvelle tentative : surcharge et pannes amont.
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class HttpError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable


def _opener(url: str) -> urllib.request.OpenerDirector:
    """Construit un opener adapté à la cible.

    Le trafic vers la boucle locale ne doit jamais traverser un proxy : sur
    une machine configurée avec `HTTPS_PROXY`, laisser urllib appliquer sa
    règle par défaut ferait sortir vers Internet une requête destinée au
    serveur de modèle local.
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    if host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127."):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def request_json(
    url: str,
    *,
    method: str = "POST",
    payload: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 120.0,
    attempts: int = 3,
    backoff: float = 1.5,
) -> dict[str, Any]:
    """Envoie une requête JSON et renvoie la réponse décodée.

    Les tentatives ne concernent que les pannes transitoires. Une erreur de
    requête — 400, 401, 404 — est renvoyée immédiatement : la réessayer ne
    ferait que retarder le diagnostic.
    """
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    base_headers = {"Accept": "application/json", "User-Agent": "agent-os/0.1"}
    if body is not None:
        base_headers["Content-Type"] = "application/json"
    base_headers.update(headers or {})

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, headers=base_headers, method=method)
        try:
            with _opener(url).open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            retryable = exc.code in RETRYABLE_STATUS
            last = HttpError(
                f"HTTP {exc.code} sur {url}: {detail[:300]}",
                status=exc.code, body=detail, retryable=retryable,
            )
            if not retryable or attempt == attempts:
                raise last
            delay = _retry_after(exc.headers) or _jitter(backoff, attempt)

        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            last = HttpError(f"{url} injoignable : {exc}", retryable=True)
            if attempt == attempts:
                raise last
            delay = _jitter(backoff, attempt)

        except json.JSONDecodeError as exc:
            raise HttpError(f"réponse non-JSON de {url}: {exc}") from exc

        log.debug("nouvelle tentative %d/%d sur %s dans %.1fs", attempt, attempts, url, delay)
        time.sleep(delay)

    raise last or HttpError(f"échec sur {url}")


def _retry_after(headers) -> float | None:
    """Respecte `Retry-After` quand le serveur l'indique."""
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return max(0.0, min(60.0, float(value)))
    except ValueError:
        return None


def _jitter(base: float, attempt: int) -> float:
    """Backoff exponentiel avec bruit, plafonné.

    Le bruit évite que plusieurs jobs repartis en même temps ne se
    resynchronisent sur le même créneau à chaque tentative.
    """
    return min(30.0, base ** attempt) * (0.5 + random.random() / 2)


def reachable(url: str, timeout: float = 2.0) -> bool:
    """Teste l'ouverture TCP sans envoyer de requête applicative."""
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname or "127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False
