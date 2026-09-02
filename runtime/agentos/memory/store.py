"""Stockage local : SQLite en WAL, schéma versionné, horloge de Lamport.

Un seul fichier porte toute la mémoire de la machine — le journal
épisodique, les faits sémantiques, les vecteurs et la file de synchro.
Une base unique rend le snapshot et la restauration atomiques ; c'est ce
qui compte le plus sur une machine qui peut être coupée sans préavis.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .ids import ulid

SCHEMA_VERSION = 2

#: Chaque migration est appliquée dans une transaction, dans l'ordre.
MIGRATIONS: list[str] = [
    # --- v1 -------------------------------------------------------------
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- Journal de ce qui s'est passé. Append-only : on ne réécrit pas
    -- l'histoire, on la révoque (voir facts.revoked) ou on la compacte.
    CREATE TABLE episodes (
        id      INTEGER PRIMARY KEY,
        uid     TEXT    NOT NULL UNIQUE,
        ts      REAL    NOT NULL,
        kind    TEXT    NOT NULL,
        actor   TEXT    NOT NULL,
        session TEXT,
        content TEXT    NOT NULL,
        meta    TEXT    NOT NULL DEFAULT '{}',
        lamport INTEGER NOT NULL DEFAULT 0,
        node    TEXT    NOT NULL DEFAULT '',
        synced  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX episodes_ts      ON episodes(ts DESC);
    CREATE INDEX episodes_session ON episodes(session, ts DESC);
    CREATE INDEX episodes_kind    ON episodes(kind, ts DESC);
    CREATE INDEX episodes_outbox  ON episodes(synced) WHERE synced = 0;

    -- Faits durables extraits des épisodes. Le triplet est unique : réaffirmer
    -- un fait connu renforce sa confiance au lieu de créer un doublon.
    CREATE TABLE facts (
        id         INTEGER PRIMARY KEY,
        uid        TEXT    NOT NULL UNIQUE,
        subject    TEXT    NOT NULL,
        predicate  TEXT    NOT NULL,
        object     TEXT    NOT NULL,
        confidence REAL    NOT NULL DEFAULT 0.5,
        source_uid TEXT,
        created    REAL    NOT NULL,
        updated    REAL    NOT NULL,
        revoked    INTEGER NOT NULL DEFAULT 0,
        lamport    INTEGER NOT NULL DEFAULT 0,
        node       TEXT    NOT NULL DEFAULT '',
        synced     INTEGER NOT NULL DEFAULT 0,
        UNIQUE(subject, predicate, object)
    );
    CREATE INDEX facts_subject ON facts(subject) WHERE revoked = 0;
    CREATE INDEX facts_updated ON facts(updated DESC);
    CREATE INDEX facts_outbox  ON facts(synced) WHERE synced = 0;

    -- Vecteurs à part : les scans de `episodes` ne doivent pas traîner
    -- des blobs de plusieurs kilo-octets par ligne.
    CREATE TABLE vectors (
        uid     TEXT PRIMARY KEY,
        scope   TEXT NOT NULL,
        dim     INTEGER NOT NULL,
        scale   REAL NOT NULL,
        data    BLOB NOT NULL,
        updated REAL NOT NULL
    );
    CREATE INDEX vectors_scope ON vectors(scope, updated DESC);

    -- Recherche plein texte. `remove_diacritics 2` pour que « mémoire »
    -- et « memoire » se trouvent l'un l'autre.
    CREATE VIRTUAL TABLE episodes_fts USING fts5(
        content,
        content='episodes',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER episodes_ai AFTER INSERT ON episodes BEGIN
        INSERT INTO episodes_fts(rowid, content) VALUES (new.id, new.content);
    END;
    CREATE TRIGGER episodes_ad AFTER DELETE ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    END;
    CREATE TRIGGER episodes_au AFTER UPDATE OF content ON episodes BEGIN
        INSERT INTO episodes_fts(episodes_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
        INSERT INTO episodes_fts(rowid, content) VALUES (new.id, new.content);
    END;

    CREATE VIRTUAL TABLE facts_fts USING fts5(
        subject, predicate, object,
        content='facts',
        content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts(rowid, subject, predicate, object)
        VALUES (new.id, new.subject, new.predicate, new.object);
    END;
    CREATE TRIGGER facts_ad AFTER DELETE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, subject, predicate, object)
        VALUES ('delete', old.id, old.subject, old.predicate, old.object);
    END;
    CREATE TRIGGER facts_au AFTER UPDATE OF subject, predicate, object ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, subject, predicate, object)
        VALUES ('delete', old.id, old.subject, old.predicate, old.object);
        INSERT INTO facts_fts(rowid, subject, predicate, object)
        VALUES (new.id, new.subject, new.predicate, new.object);
    END;

    -- Ce que la synchro distante a déjà vu, pour ne pas retélécharger.
    CREATE TABLE sync_state (
        scope     TEXT PRIMARY KEY,
        cursor    TEXT NOT NULL DEFAULT '',
        last_run  REAL NOT NULL DEFAULT 0,
        last_ok   REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT ''
    );

    -- File de l'ordonnanceur.
    CREATE TABLE jobs (
        id         INTEGER PRIMARY KEY,
        uid        TEXT    NOT NULL UNIQUE,
        name       TEXT    NOT NULL,
        payload    TEXT    NOT NULL DEFAULT '{}',
        state      TEXT    NOT NULL DEFAULT 'pending',
        run_after  REAL    NOT NULL,
        attempts   INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        created    REAL    NOT NULL,
        started    REAL,
        finished   REAL,
        result     TEXT,
        error      TEXT,
        origin     TEXT NOT NULL DEFAULT 'manual'
    );
    CREATE INDEX jobs_ready ON jobs(state, run_after);
    CREATE INDEX jobs_created ON jobs(created DESC);

    -- Déclencheurs récurrents (cron) et leur dernier tir.
    CREATE TABLE schedules (
        id       INTEGER PRIMARY KEY,
        name     TEXT    NOT NULL UNIQUE,
        cron     TEXT    NOT NULL,
        job      TEXT    NOT NULL,
        payload  TEXT    NOT NULL DEFAULT '{}',
        enabled  INTEGER NOT NULL DEFAULT 1,
        last_fire REAL   NOT NULL DEFAULT 0,
        next_fire REAL   NOT NULL DEFAULT 0
    );
    CREATE INDEX schedules_next ON schedules(enabled, next_fire);
    """,
    # --- v2 : provenance et confiance -----------------------------------
    # Un souvenir est du contexte, jamais une consigne. Sans cette colonne,
    # une note écrite par l'agent — ou reçue d'une autre machine par la
    # synchro — arrive dans le prompt avec exactement le poids d'un fait
    # établi par l'opérateur, ce qui suffit à faire d'une mémoire empoisonnée
    # un canal d'instruction durable.
    """
    ALTER TABLE episodes ADD COLUMN trust TEXT NOT NULL DEFAULT 'agent';
    ALTER TABLE facts    ADD COLUMN trust TEXT NOT NULL DEFAULT 'agent';
    CREATE INDEX episodes_trust ON episodes(trust);
    CREATE INDEX facts_trust    ON facts(trust);
    """,
]

#: Niveaux de confiance, du plus sûr au plus suspect.
#:  - operator : affirmé par l'humain via la console ou la CLI ;
#:  - agent    : observation ou déduction de l'agent lui-même ;
#:  - external : issu d'un contenu récupéré (web, fichier, sortie d'outil)
#:               ou reçu d'un autre nœud par la synchro.
TRUST_LEVELS = ("operator", "agent", "external")
TRUSTED = "operator"


@dataclass(slots=True)
class Episode:
    uid: str
    ts: float
    kind: str
    actor: str
    content: str
    session: str | None = None
    meta: dict[str, Any] | None = None


class Store:
    """Accès à la base locale, sûr entre threads.

    SQLite en WAL autorise un écrivain et plusieurs lecteurs simultanés,
    mais un objet `Connection` ne traverse pas les threads. On garde donc
    une connexion par thread, et un verrou de processus sérialise les
    écritures pour transformer les `SQLITE_BUSY` en attente ordonnée
    plutôt qu'en erreur remontée à l'appelant.
    """

    def __init__(self, path: str | Path, *, node: str = "") -> None:
        self.path = Path(path)
        self.node = node
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # -- connexions ------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL plutôt que FULL : sur coupure brutale on peut perdre la
            # dernière transaction, jamais l'intégrité de la base. Le gain en
            # écritures est d'un ordre de grandeur sur disque mécanique.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA temp_store=MEMORY")
            # 64 Mo de cache par connexion : confortable sur 16 Go, et borné
            # pour ne pas concurrencer un modèle local chargé en RAM.
            conn.execute("PRAGMA cache_size=-65536")
            conn.execute("PRAGMA mmap_size=268435456")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- migrations ------------------------------------------------------

    def _migrate(self) -> None:
        conn = self.conn
        with self._write_lock:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > len(MIGRATIONS):
                raise RuntimeError(
                    f"base en version {current}, ce binaire n'en connaît que "
                    f"{len(MIGRATIONS)} — mise à jour du runtime nécessaire"
                )
            for index in range(current, len(MIGRATIONS)):
                # `executescript` valide toute transaction ouverte avant de
                # démarrer : le BEGIN doit donc vivre dans le script lui-même,
                # sinon la migration s'applique hors transaction et une erreur
                # à mi-parcours laisse un schéma à moitié créé.
                script = (
                    "BEGIN;\n"
                    f"{MIGRATIONS[index]}\n"
                    f"PRAGMA user_version={index + 1};\n"
                    "COMMIT;"
                )
                try:
                    conn.executescript(script)
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

    # -- horloge de Lamport ---------------------------------------------

    def tick(self) -> int:
        """Incrémente et renvoie l'horloge logique.

        Elle départage deux écritures concurrentes venues de machines
        différentes, ce que l'horloge murale ne peut pas faire de façon
        fiable entre nœuds désynchronisés.
        """
        with self._write_lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key='lamport'").fetchone()
            value = int(row["value"]) + 1 if row else 1
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('lamport', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(value),),
            )
            return value

    def observe_lamport(self, remote: int) -> int:
        """Aligne l'horloge sur une valeur distante reçue."""
        with self._write_lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key='lamport'").fetchone()
            value = max(int(row["value"]) if row else 0, remote) + 1
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('lamport', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(value),),
            )
            return value

    # -- épisodes --------------------------------------------------------

    def remember(
        self,
        content: str,
        *,
        kind: str = "note",
        actor: str = "agent",
        session: str | None = None,
        meta: dict[str, Any] | None = None,
        ts: float | None = None,
        uid: str | None = None,
        trust: str = "agent",
    ) -> str:
        """Consigne un épisode et renvoie son identifiant."""
        if trust not in TRUST_LEVELS:
            raise ValueError(f"niveau de confiance inconnu : {trust!r}")
        with self._write_lock:
            episode_uid = uid or ulid()
            self.conn.execute(
                "INSERT INTO episodes(uid, ts, kind, actor, session, content, meta, lamport, node, trust) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    episode_uid,
                    ts if ts is not None else time.time(),
                    kind,
                    actor,
                    session,
                    content,
                    json.dumps(meta or {}, ensure_ascii=False),
                    self.tick(),
                    self.node,
                    trust,
                ),
            )
            return episode_uid

    def recent(self, limit: int = 50, *, session: str | None = None, kind: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        if session:
            clauses.append("session = ?")
            params.append(session)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"SELECT * FROM episodes {where} ORDER BY ts DESC LIMIT ?", params
        ).fetchall()

    def episode(self, uid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM episodes WHERE uid = ?", (uid,)).fetchone()

    # -- faits -----------------------------------------------------------

    def assert_fact(
        self,
        subject: str,
        predicate: str,
        object_: str,
        *,
        confidence: float = 0.6,
        source_uid: str | None = None,
        trust: str = "agent",
    ) -> str:
        """Affirme un fait. Réaffirmer un fait connu renforce sa confiance.

        La montée est asymptotique vers 1.0 : chaque confirmation comble la
        moitié du chemin restant, donc rien ne devient jamais certain par
        simple répétition.
        """
        now = time.time()
        with self._write_lock:
            row = self.conn.execute(
                "SELECT uid, confidence, trust FROM facts WHERE subject=? AND predicate=? AND object=?",
                (subject, predicate, object_),
            ).fetchone()
            if row is not None:
                boosted = row["confidence"] + (1.0 - row["confidence"]) * 0.5
                # La confiance d'un fait ne se dégrade pas : qu'un contenu
                # externe réaffirme ce que l'opérateur a établi ne doit pas
                # faire redescendre le fait au rang de donnée suspecte.
                rank = {level: index for index, level in enumerate(TRUST_LEVELS)}
                best = min(row["trust"], trust, key=lambda level: rank.get(level, 99))
                self.conn.execute(
                    "UPDATE facts SET confidence=?, updated=?, revoked=0, lamport=?, node=?, "
                    "trust=?, synced=0 WHERE uid=?",
                    (boosted, now, self.tick(), self.node, best, row["uid"]),
                )
                return row["uid"]

            if trust not in TRUST_LEVELS:
                raise ValueError(f"niveau de confiance inconnu : {trust!r}")
            fact_uid = ulid()
            self.conn.execute(
                "INSERT INTO facts(uid, subject, predicate, object, confidence, source_uid, "
                "created, updated, lamport, node, trust) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fact_uid, subject, predicate, object_,
                    max(0.0, min(1.0, confidence)), source_uid,
                    now, now, self.tick(), self.node, trust,
                ),
            )
            return fact_uid

    def revoke_fact(self, uid: str) -> bool:
        """Retire un fait sans l'effacer : l'historique reste auditable."""
        with self._write_lock:
            cursor = self.conn.execute(
                "UPDATE facts SET revoked=1, updated=?, lamport=?, node=?, synced=0 WHERE uid=?",
                (time.time(), self.tick(), self.node, uid),
            )
            return cursor.rowcount > 0

    def facts_about(self, subject: str, *, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM facts WHERE subject=? AND revoked=0 "
            "ORDER BY confidence DESC, updated DESC LIMIT ?",
            (subject, limit),
        ).fetchall()

    # -- accès générique -------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def write(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn.execute(sql, tuple(params))

    def writemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._write_lock:
            self.conn.executemany(sql, [tuple(r) for r in rows])

    def transaction(self):
        """Contexte de transaction explicite, sérialisé entre threads."""
        return _Transaction(self)

    # -- entretien -------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        counts = {}
        for table in ("episodes", "facts", "vectors", "jobs", "schedules"):
            counts[table] = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        page_size = self.conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = self.conn.execute("PRAGMA page_count").fetchone()[0]
        counts["bytes"] = page_size * page_count
        counts["pending_sync"] = self.conn.execute(
            "SELECT (SELECT count(*) FROM episodes WHERE synced=0) + "
            "(SELECT count(*) FROM facts WHERE synced=0)"
        ).fetchone()[0]
        return counts

    def checkpoint(self) -> None:
        """Replie le WAL dans la base principale et rend l'espace au disque."""
        with self._write_lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class _Transaction:
    def __init__(self, store: Store) -> None:
        self._store = store

    def __enter__(self) -> sqlite3.Connection:
        self._store._write_lock.acquire()
        self._store.conn.execute("BEGIN IMMEDIATE")
        return self._store.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._store.conn.execute("COMMIT")
            else:
                self._store.conn.execute("ROLLBACK")
        finally:
            self._store._write_lock.release()
        return False
