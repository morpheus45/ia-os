"""Production des vecteurs d'embedding.

Deux backends, essayés dans l'ordre. Le serveur local (llama.cpp ou
Ollama, tous deux compatibles avec la route OpenAI `/embeddings`) donne de
vrais vecteurs sémantiques. Quand il est absent — modèle pas encore
téléchargé, service arrêté, machine fraîchement installée — on retombe
sur un encodage lexical par hachage.

Ce repli n'est pas sémantique et ne prétend pas l'être : il rapproche des
formes voisines (« installer » / « installation », une faute de frappe et
son mot correct), pas des synonymes. Son intérêt est ailleurs — il est
déterministe, sans dépendance ni réseau, et il garde la recherche
vectorielle fonctionnelle au lieu de la faire disparaître.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Sequence

_WORD = re.compile(r"\w+", re.UNICODE)


class Embedder(ABC):
    """Interface commune. `dim` doit rester stable pour un index donné."""

    dim: int
    name: str

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def available(self) -> bool:
        return True


class HashingEmbedder(Embedder):
    """Encodage lexical déterministe par hachage de traits.

    Les traits sont les mots et les trigrammes de caractères. Chaque trait
    est projeté sur une dimension par BLAKE2b — et non par `hash()`, dont
    la graine change à chaque processus, ce qui rendrait tout index
    illisible au redémarrage suivant.
    """

    name = "hashing"

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        lowered = text.lower()
        words = _WORD.findall(lowered)
        features = list(words)
        for word in words:
            padded = f"^{word}$"
            features.extend(padded[i : i + 3] for i in range(max(1, len(padded) - 2)))
        return features

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                position = int.from_bytes(digest[:4], "big") % self.dim
                # Le bit de signe évite que des traits sans rapport tombant sur
                # la même dimension ne s'additionnent systématiquement.
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[position] += sign
            out.append(vector)
        return out


class OpenAICompatEmbedder(Embedder):
    """Client de la route `/embeddings` d'un serveur local.

    Fonctionne avec `llama-server --embeddings` comme avec Ollama. Le
    résultat de la sonde de disponibilité est mis en cache quelques
    secondes : sans cela, chaque écriture en mémoire paierait un aller-retour
    réseau juste pour découvrir que le service est toujours éteint.
    """

    name = "local"

    def __init__(self, url: str, model: str, dim: int, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.dim = dim
        self.timeout = timeout
        self._lock = threading.Lock()
        self._probe_at = 0.0
        self._probe_ok = False
        self._probe_ttl = 30.0

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.url}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def available(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now - self._probe_at < self._probe_ttl:
                return self._probe_ok
            self._probe_at = now
            try:
                self._post({"model": self.model, "input": "ping"})
                self._probe_ok = True
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                self._probe_ok = False
            return self._probe_ok

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        body = self._post({"model": self.model, "input": list(texts)})
        vectors = [item["embedding"] for item in body["data"]]
        for vector in vectors:
            if len(vector) != self.dim:
                raise ValueError(
                    f"le modèle {self.model} renvoie {len(vector)} dimensions, "
                    f"l'index en attend {self.dim} — corriger memory.vector_dim "
                    "puis lancer « agentosctl memory reindex »"
                )
        return vectors


class FallbackEmbedder(Embedder):
    """Enchaîne les backends : le premier disponible gagne.

    Une panne du serveur local en cours de route ne doit pas faire échouer
    une écriture en mémoire ; on dégrade vers le hachage et on repasse au
    modèle dès qu'il répond de nouveau.
    """

    name = "fallback"

    def __init__(self, backends: Sequence[Embedder]) -> None:
        if not backends:
            raise ValueError("au moins un backend d'embedding est requis")
        dims = {backend.dim for backend in backends}
        if len(dims) != 1:
            raise ValueError(f"backends de dimensions incompatibles : {sorted(dims)}")
        self.backends = list(backends)
        self.dim = self.backends[0].dim
        self.last_used = self.backends[-1].name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        errors: list[str] = []
        for backend in self.backends:
            if not backend.available():
                continue
            try:
                vectors = backend.embed(texts)
                self.last_used = backend.name
                return vectors
            except Exception as exc:  # noqa: BLE001 - on essaie le suivant
                errors.append(f"{backend.name}: {exc}")
        raise RuntimeError("aucun backend d'embedding disponible — " + "; ".join(errors))


def build(config) -> FallbackEmbedder:
    """Assemble la chaîne d'embedding depuis la configuration."""
    dim = config.memory.vector_dim
    return FallbackEmbedder(
        [
            OpenAICompatEmbedder(config.brain.local_url, config.brain.embed_model, dim),
            HashingEmbedder(dim),
        ]
    )
