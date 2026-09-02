"""Recherche hybride : plein texte et vecteurs, fusionnés par rang.

Les deux voies échouent sur des choses différentes. Le plein texte rate
la reformulation (« combien de RAM ? » ne trouve pas « 16 Go de mémoire
vive ») ; les vecteurs ratent le terme exact et rare — un identifiant, un
code d'erreur, un nom propre — que BM25 trouve immédiatement.

On les fusionne par *reciprocal rank fusion* plutôt qu'en combinant les
scores. Un score BM25 et un cosinus ne vivent pas sur la même échelle et
leurs plages varient d'une requête à l'autre ; les rangs, eux, sont
comparables sans calibration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

_TOKEN = re.compile(r"\w+", re.UNICODE)


@dataclass
class Result:
    uid: str
    scope: str          # 'episode' ou 'fact'
    text: str
    score: float
    ts: float
    #: Niveau de confiance : décide si le contenu peut être présenté au
    #: modèle comme du contexte ou seulement comme de la donnée citée.
    trust: str = "agent"
    #: Origine du résultat : utile pour comprendre pourquoi il remonte.
    sources: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


def fts_query(text: str, *, mode: str = "or") -> str:
    """Transforme du texte libre en expression FTS5 sûre.

    Une requête utilisateur contient des guillemets, des astérisques, des
    parenthèses — tous opérateurs FTS5. On ne les échappe pas, on ne garde
    que les tokens et on les cite : une requête ne peut alors ni faire
    d'erreur de syntaxe ni changer de sens.
    """
    tokens = _TOKEN.findall(text)
    if not tokens:
        return ""
    quoted = ['"' + token.replace('"', '""') + '"' for token in tokens]
    return f" {'AND' if mode == 'and' else 'OR'} ".join(quoted)


def _fts_episodes(store, query: str, limit: int) -> list[tuple[str, float]]:
    if not query:
        return []
    rows = store.execute(
        "SELECT e.uid AS uid, bm25(episodes_fts) AS rank "
        "FROM episodes_fts JOIN episodes e ON e.id = episodes_fts.rowid "
        "WHERE episodes_fts MATCH ? ORDER BY rank LIMIT ?",
        (query, limit),
    ).fetchall()
    return [(row["uid"], row["rank"]) for row in rows]


def _fts_facts(store, query: str, limit: int) -> list[tuple[str, float]]:
    if not query:
        return []
    rows = store.execute(
        "SELECT f.uid AS uid, bm25(facts_fts) AS rank "
        "FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
        "WHERE facts_fts MATCH ? AND f.revoked = 0 ORDER BY rank LIMIT ?",
        (query, limit),
    ).fetchall()
    return [(row["uid"], row["rank"]) for row in rows]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], *, k: int = 60, weights: Sequence[float] | None = None
) -> dict[str, float]:
    """Fusionne des listes ordonnées. Le rang 0 est le meilleur.

    `k` amortit le poids des toutes premières places : sans lui, un premier
    rang dans une seule liste écraserait un consensus sur les rangs 2-3 de
    toutes les autres.
    """
    weights = list(weights or [1.0] * len(rankings))
    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        for rank, uid in enumerate(ranking):
            scores[uid] = scores.get(uid, 0.0) + weight / (k + rank + 1)
    return scores


def search(
    store,
    index,
    embedder,
    query: str,
    *,
    limit: int = 10,
    scope: str | None = None,
    candidates: int = 64,
    rrf_k: int = 60,
    use_vectors: bool = True,
    min_similarity: float | None = None,
) -> list[Result]:
    """Cherche dans la mémoire et renvoie les meilleurs résultats fusionnés."""
    match = fts_query(query)
    rankings: list[list[str]] = []
    labels: list[str] = []

    episode_hits = _fts_episodes(store, match, candidates) if scope != "fact" else []
    fact_hits = _fts_facts(store, match, candidates) if scope != "episode" else []
    if episode_hits:
        rankings.append([uid for uid, _ in episode_hits])
        labels.append("texte")
    if fact_hits:
        rankings.append([uid for uid, _ in fact_hits])
        labels.append("texte")

    if use_vectors and len(index) > 0:
        try:
            vector = embedder.embed_one(query)
            hits = index.search(vector, limit=candidates, scope=scope,
                                min_score=min_similarity)
            if hits:
                rankings.append([hit.uid for hit in hits])
                labels.append("vecteur")
        except Exception:
            # Un embedding indisponible dégrade la recherche, il ne la casse pas.
            pass

    if not rankings:
        return []

    fused = reciprocal_rank_fusion(rankings, k=rrf_k)
    origin: dict[str, list[str]] = {}
    for ranking, label in zip(rankings, labels):
        for uid in ranking:
            bucket = origin.setdefault(uid, [])
            if label not in bucket:
                bucket.append(label)

    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]
    return _hydrate(store, ordered, origin)


def _hydrate(store, ordered: list[tuple[str, float]], origin: dict[str, list[str]]) -> list[Result]:
    """Recharge le contenu des uid retenus, en deux requêtes au plus."""
    if not ordered:
        return []
    uids = [uid for uid, _ in ordered]
    placeholders = ",".join("?" * len(uids))

    rows: dict[str, Result] = {}
    for row in store.execute(
        f"SELECT uid, ts, content, kind, actor, session, trust "
        f"FROM episodes WHERE uid IN ({placeholders})",
        uids,
    ).fetchall():
        rows[row["uid"]] = Result(
            uid=row["uid"], scope="episode", text=row["content"], score=0.0, ts=row["ts"],
            trust=row["trust"],
            meta={"kind": row["kind"], "actor": row["actor"], "session": row["session"]},
        )
    for row in store.execute(
        f"SELECT uid, updated, subject, predicate, object, confidence, trust "
        f"FROM facts WHERE uid IN ({placeholders})",
        uids,
    ).fetchall():
        rows[row["uid"]] = Result(
            uid=row["uid"], scope="fact",
            text=f"{row['subject']} · {row['predicate']} · {row['object']}",
            score=0.0, ts=row["updated"], trust=row["trust"],
            meta={"confidence": row["confidence"], "subject": row["subject"],
                  "predicate": row["predicate"], "object": row["object"]},
        )

    out: list[Result] = []
    for uid, score in ordered:
        result = rows.get(uid)
        if result is None:
            continue  # supprimé entre la recherche et l'hydratation
        result.score = score
        result.sources = origin.get(uid, [])
        out.append(result)
    return out
