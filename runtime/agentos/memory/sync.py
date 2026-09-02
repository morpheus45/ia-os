"""Synchronisation de la mémoire avec Supabase.

Le protocole est volontairement simple : on pousse ce qui n'a pas encore
été envoyé, on tire ce qui a changé depuis le dernier curseur. Il n'y a
pas de transaction distribuée, et il n'en faut pas — la mémoire est
faite d'écritures indépendantes, jamais d'un état global à maintenir
cohérent en deux endroits.

Deux règles gouvernent la fusion. La première départage deux écritures
concurrentes par l'horloge de Lamport, puis par identifiant de nœud à
égalité : deux machines hors ligne aboutissent au même résultat, quel
que soit l'ordre dans lequel elles se reconnectent. La seconde plafonne
la confiance de tout ce qui arrive d'ailleurs. Une machine ne peut pas
vérifier qu'une affirmation marquée « operator » sur un autre nœud vient
bien de l'humain ; sans ce plafond, compromettre une seule machine de la
flotte suffirait à injecter des consignes réputées fiables partout.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import net
from . import crypto
from .store import TRUST_LEVELS

log = logging.getLogger("agentos.sync")

#: Colonnes chiffrées quand `remote.encrypt` est actif.
ENCRYPTED_FIELDS = {"episodes": ("content",), "facts": ("object",)}


@dataclass
class SyncReport:
    pushed: int = 0
    pulled: int = 0
    conflicts: int = 0
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        base = f"{self.pushed} poussé(s), {self.pulled} tiré(s)"
        if self.conflicts:
            base += f", {self.conflicts} conflit(s) tranché(s)"
        if self.errors:
            base += f" — {len(self.errors)} erreur(s) : {self.errors[0]}"
        return base


class SupabaseBackend:
    """Accès à la mémoire distante par l'API REST de Supabase (PostgREST)."""

    def __init__(self, url: str, api_key: str, *, timeout: float = 30.0) -> None:
        if not url:
            raise ValueError("remote.url est vide")
        if not api_key:
            raise ValueError(
                "AGENTOS_SUPABASE_KEY est vide — utiliser la clé « service_role », "
                "jamais la clé « anon » à laquelle RLS refuse tout accès"
            )
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Profile": "public",
        }
        self.timeout = timeout

    def upsert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        net.request_json(
            f"{self.base}/{table}?on_conflict=uid",
            payload=rows,
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=self.timeout,
        )

    def select(self, table: str, *, since: int, exclude_node: str, limit: int) -> list[dict]:
        """Tire les lignes écrites ailleurs, au-delà du curseur de Lamport."""
        query = (f"{self.base}/{table}"
                 f"?lamport=gt.{since}"
                 f"&node=neq.{exclude_node}"
                 f"&order=lamport.asc&limit={limit}")
        body = net.request_json(query, method="GET", headers=self.headers, timeout=self.timeout)
        return body if isinstance(body, list) else []

    def register_node(self, node: str, label: str, version: str) -> None:
        self.upsert_nodes([{
            "node": node, "label": label, "version": version,
            "last_seen": _now_iso(),
        }])

    def upsert_nodes(self, rows: list[dict[str, Any]]) -> None:
        net.request_json(
            f"{self.base}/agentos_nodes?on_conflict=node",
            payload=rows,
            headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=self.timeout,
        )

    def log(self, node: str, direction: str, scope: str, rows: int, error: str = "") -> None:
        try:
            net.request_json(
                f"{self.base}/agentos_sync_log",
                payload=[{"node": node, "direction": direction, "scope": scope,
                          "rows": rows, "error": error[:500]}],
                headers={**self.headers, "Prefer": "return=minimal"},
                timeout=self.timeout, attempts=1,
            )
        except net.HttpError:
            pass  # le journal distant ne doit jamais faire échouer une synchro


class Synchroniser:
    """Pousse et tire la mémoire entre la base locale et Supabase."""

    def __init__(self, store, config, *, backend: SupabaseBackend | None = None) -> None:
        self.store = store
        self.config = config
        self.node = config.remote.node_id
        self.batch = max(1, config.remote.batch)

        self.key: bytes | None = None
        if config.remote.encrypt:
            if not crypto.available():
                raise crypto.CryptoUnavailable(
                    "remote.encrypt = true mais « cryptography » est absent — "
                    "la synchro refuse de partir en clair"
                )
            self.key = crypto.derive_key(config.remote_key)

        ceiling = config.remote.trust_ceiling
        if ceiling not in TRUST_LEVELS:
            raise ValueError(f"remote.trust_ceiling inconnu : {ceiling!r}")
        self._ceiling_rank = TRUST_LEVELS.index(ceiling)

        self.backend = backend or SupabaseBackend(config.remote.url, config.remote_api_key)

    # -- confidentialité --------------------------------------------------

    def _seal(self, value: str) -> str:
        return crypto.encrypt(value, self.key) if self.key else value

    def _open(self, value: str, encrypted: bool) -> str:
        if not encrypted:
            return value
        if self.key is None:
            raise ValueError("ligne chiffrée reçue mais aucune clé configurée")
        return crypto.decrypt(value, self.key)

    def _cap_trust(self, remote_trust: str) -> str:
        """Rabaisse la confiance d'une donnée venue d'ailleurs.

        Un niveau inconnu — nœud plus récent, ou compromis — est traité comme
        le plus défavorable plutôt que rejeté : refuser la ligne ferait
        échouer tout le lot, et une valeur inattendue n'est pas une raison de
        cesser de se synchroniser.
        """
        worst = len(TRUST_LEVELS) - 1
        rank = TRUST_LEVELS.index(remote_trust) if remote_trust in TRUST_LEVELS else worst
        return TRUST_LEVELS[min(worst, max(rank, self._ceiling_rank))]

    # -- envoi ------------------------------------------------------------

    def push(self) -> SyncReport:
        report = SyncReport()
        for scope in ("episodes", "facts"):
            try:
                report.pushed += self._push_scope(scope)
            except (net.HttpError, ValueError, crypto.CryptoUnavailable) as exc:
                report.errors.append(f"push {scope}: {exc}")
                self.backend.log(self.node, "push", scope, 0, str(exc))
        return report

    def _push_scope(self, scope: str) -> int:
        rows = self.store.execute(
            f"SELECT * FROM {scope} WHERE synced = 0 ORDER BY lamport LIMIT ?", (self.batch,)
        ).fetchall()
        if not rows:
            return 0

        payload = [self._encode(scope, row) for row in rows]
        self.backend.upsert(f"agentos_{scope}", payload)
        self.store.writemany(
            f"UPDATE {scope} SET synced = 1 WHERE uid = ?", [(row["uid"],) for row in rows]
        )
        self.backend.log(self.node, "push", scope, len(rows))
        log.info("synchro : %d %s poussés", len(rows), scope)
        return len(rows)

    def _encode(self, scope: str, row) -> dict[str, Any]:
        encrypt_fields = ENCRYPTED_FIELDS[scope] if self.key else ()
        if scope == "episodes":
            return {
                "uid": row["uid"], "node": self.node, "ts": _iso(row["ts"]),
                "kind": row["kind"], "actor": row["actor"], "session": row["session"],
                "content": self._seal(row["content"]) if "content" in encrypt_fields else row["content"],
                "meta": json.loads(row["meta"] or "{}"),
                "lamport": row["lamport"], "trust": row["trust"],
                "encrypted": bool(encrypt_fields),
            }
        return {
            "uid": row["uid"], "node": self.node,
            "subject": row["subject"], "predicate": row["predicate"],
            "object": self._seal(row["object"]) if "object" in encrypt_fields else row["object"],
            "confidence": row["confidence"], "source_uid": row["source_uid"],
            "created": _iso(row["created"]), "updated": _iso(row["updated"]),
            "revoked": bool(row["revoked"]), "lamport": row["lamport"],
            "trust": row["trust"], "encrypted": bool(encrypt_fields),
        }

    # -- réception --------------------------------------------------------

    def pull(self) -> SyncReport:
        report = SyncReport()
        for scope in ("episodes", "facts"):
            try:
                pulled, conflicts = self._pull_scope(scope)
                report.pulled += pulled
                report.conflicts += conflicts
            except (net.HttpError, ValueError) as exc:
                report.errors.append(f"pull {scope}: {exc}")
                self.backend.log(self.node, "pull", scope, 0, str(exc))
        return report

    def _pull_scope(self, scope: str) -> tuple[int, int]:
        cursor = self._cursor(scope)
        rows = self.backend.select(
            f"agentos_{scope}", since=cursor, exclude_node=self.node, limit=self.batch)
        if not rows:
            return 0, 0

        applied = conflicts = 0
        highest = cursor
        for remote in rows:
            highest = max(highest, int(remote.get("lamport") or 0))
            try:
                outcome = self._merge(scope, remote)
            except (ValueError, KeyError) as exc:
                log.warning("ligne %s ignorée : %s", remote.get("uid"), exc)
                continue
            applied += int(outcome != "ignoré")
            conflicts += int(outcome == "remplacé")

        self.store.write(
            "INSERT INTO sync_state(scope, cursor, last_run, last_ok) VALUES (?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET cursor=excluded.cursor, "
            "last_run=excluded.last_run, last_ok=excluded.last_ok, last_error=''",
            (scope, str(highest), time.time(), time.time()),
        )
        # L'horloge locale doit dépasser tout ce qu'on a vu, sinon les
        # prochaines écritures d'ici perdraient systématiquement l'arbitrage.
        self.store.observe_lamport(highest)
        self.backend.log(self.node, "pull", scope, applied)
        log.info("synchro : %d %s appliqués (%d conflits)", applied, scope, conflicts)
        return applied, conflicts

    def _cursor(self, scope: str) -> int:
        row = self.store.execute(
            "SELECT cursor FROM sync_state WHERE scope = ?", (scope,)).fetchone()
        try:
            return int(row["cursor"]) if row and row["cursor"] else 0
        except (TypeError, ValueError):
            return 0

    def _merge(self, scope: str, remote: dict[str, Any]) -> str:
        """Applique une ligne distante. Renvoie 'inséré', 'remplacé' ou 'ignoré'."""
        uid = remote["uid"]
        remote_lamport = int(remote.get("lamport") or 0)
        remote_node = remote.get("node") or ""
        trust = self._cap_trust(remote.get("trust") or "agent")

        local = self.store.execute(
            f"SELECT lamport, node FROM {scope} WHERE uid = ?", (uid,)).fetchone()
        if local is not None:
            local_key = (local["lamport"], local["node"])
            remote_key = (remote_lamport, remote_node)
            if local_key >= remote_key:
                # À égalité d'horloge, l'identifiant de nœud tranche. Le
                # critère importe peu ; ce qui compte est qu'il soit total et
                # identique partout, sinon deux machines divergeraient.
                return "ignoré"

        if scope == "episodes":
            content = self._open(remote["content"], bool(remote.get("encrypted")))
            self.store.write(
                "INSERT INTO episodes(uid, ts, kind, actor, session, content, meta, "
                "lamport, node, trust, synced) VALUES (?,?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(uid) DO UPDATE SET content=excluded.content, meta=excluded.meta, "
                "lamport=excluded.lamport, node=excluded.node, trust=excluded.trust, synced=1",
                (uid, _epoch(remote["ts"]), remote["kind"], remote["actor"],
                 remote.get("session"), content,
                 json.dumps(remote.get("meta") or {}, ensure_ascii=False),
                 remote_lamport, remote_node, trust),
            )
        else:
            object_ = self._open(remote["object"], bool(remote.get("encrypted")))
            self.store.write(
                "INSERT INTO facts(uid, subject, predicate, object, confidence, source_uid, "
                "created, updated, revoked, lamport, node, trust, synced) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(uid) DO UPDATE SET object=excluded.object, "
                "confidence=excluded.confidence, updated=excluded.updated, "
                "revoked=excluded.revoked, lamport=excluded.lamport, node=excluded.node, "
                "trust=excluded.trust, synced=1",
                (uid, remote["subject"], remote["predicate"], object_,
                 remote.get("confidence", 0.5), remote.get("source_uid"),
                 _epoch(remote["created"]), _epoch(remote["updated"]),
                 int(bool(remote.get("revoked"))), remote_lamport, remote_node, trust),
            )
        return "remplacé" if local is not None else "inséré"

    # -- cycle complet ----------------------------------------------------

    def run(self) -> SyncReport:
        """Un cycle complet. Pousser d'abord : en cas de coupure au milieu,
        mieux vaut avoir mis à l'abri ce qui n'existe que sur cette machine."""
        started = time.monotonic()
        try:
            self.backend.register_node(self.node, self.config.agent.name, _version())
        except (net.HttpError, ValueError) as exc:
            report = SyncReport(errors=[f"enregistrement du nœud : {exc}"])
            report.duration_s = time.monotonic() - started
            return report

        report = self.push()
        pulled = self.pull()
        report.pulled += pulled.pulled
        report.conflicts += pulled.conflicts
        report.errors.extend(pulled.errors)
        report.duration_s = time.monotonic() - started
        return report

    def status(self) -> dict[str, Any]:
        rows = {row["scope"]: dict(row) for row in
                self.store.execute("SELECT * FROM sync_state").fetchall()}
        pending = self.store.execute(
            "SELECT (SELECT count(*) FROM episodes WHERE synced=0) + "
            "(SELECT count(*) FROM facts WHERE synced=0)").fetchone()[0]
        return {
            "actif": self.config.remote.enabled,
            "noeud": self.node,
            "chiffre": self.key is not None,
            "plafond_confiance": self.config.remote.trust_ceiling,
            "en_attente": pending,
            "curseurs": {scope: row.get("cursor") for scope, row in rows.items()},
            "derniere_erreur": next(
                (row["last_error"] for row in rows.values() if row.get("last_error")), ""),
        }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _epoch(value: Any) -> float:
    """Convertit un horodatage PostgREST en secondes epoch."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return time.time()


def _version() -> str:
    from .. import __version__

    return __version__


def build(store, config) -> Synchroniser | None:
    """Construit le synchroniseur, ou None si la synchro est désactivée."""
    if not config.remote.enabled:
        return None
    return Synchroniser(store, config)
