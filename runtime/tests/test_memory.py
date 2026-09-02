"""Mémoire locale : stockage, vecteurs, recherche hybride, entretien."""

from __future__ import annotations

import math
import random
import threading
import time
import unittest

from agentos.memory import Memory
from agentos.memory.embed import HashingEmbedder
from agentos.memory.ids import timestamp_ms, ulid
from agentos.memory.search import fts_query, reciprocal_rank_fusion
from agentos.memory.store import SCHEMA_VERSION, Store
from agentos.memory.vectors import VectorIndex, dequantize, normalize, quantize

from .fakes import temp_config


class UlidTest(unittest.TestCase):
    def test_monotonic_unique_and_timestamped(self):
        values = [ulid() for _ in range(5000)]
        self.assertEqual(values, sorted(values), "l'ordre lexicographique doit suivre l'écriture")
        self.assertEqual(len(set(values)), len(values))
        self.assertLess(abs(timestamp_ms(values[0]) / 1000 - time.time()), 5)

    def test_rejects_malformed(self):
        with self.assertRaises(ValueError):
            timestamp_ms("trop-court")


class StoreTest(unittest.TestCase):
    def setUp(self):
        cfg, self.root = temp_config()
        self.store = Store(cfg.memory.database, node="banc")

    def test_migration_is_idempotent(self):
        version = self.store.conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)
        again = Store(self.store.path, node="banc")   # rouvrir ne rejoue rien
        self.assertEqual(again.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_fulltext_ignores_diacritics(self):
        uid = self.store.remember("La mémoire est branchée sur Supabase")
        self.store.remember("Rien à voir")
        rows = self.store.execute(
            "SELECT e.uid FROM episodes_fts f JOIN episodes e ON e.id = f.rowid "
            "WHERE episodes_fts MATCH ?", ("memoire",)).fetchall()
        self.assertEqual([r["uid"] for r in rows], [uid])

    def test_reasserting_a_fact_deduplicates_and_reinforces(self):
        first = self.store.assert_fact("machine", "ram_go", "16", confidence=0.5)
        self.assertEqual(self.store.assert_fact("machine", "ram_go", "16"), first)
        confidence = self.store.execute(
            "SELECT confidence FROM facts WHERE uid = ?", (first,)).fetchone()[0]
        self.assertAlmostEqual(confidence, 0.75, places=6)

    def test_confidence_never_reaches_certainty(self):
        uid = self.store.assert_fact("a", "b", "c", confidence=0.5)
        for _ in range(50):
            self.store.assert_fact("a", "b", "c")
        confidence = self.store.execute(
            "SELECT confidence FROM facts WHERE uid = ?", (uid,)).fetchone()[0]
        self.assertLess(confidence, 1.0)

    def test_revoked_facts_disappear_but_are_kept(self):
        uid = self.store.assert_fact("machine", "ram_go", "16")
        self.assertTrue(self.store.revoke_fact(uid))
        self.assertEqual(self.store.facts_about("machine"), [])
        self.assertEqual(self.store.execute("SELECT count(*) FROM facts").fetchone()[0], 1)

    def test_rollback_leaves_no_trace(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO episodes(uid,ts,kind,actor,content) "
                             "VALUES('X',1,'k','a','c')")
                raise RuntimeError("boom")
        self.assertIsNone(self.store.episode("X"))

    def test_delete_keeps_fulltext_index_consistent(self):
        uid = self.store.remember("à supprimer")
        self.store.remember("à garder")
        self.store.write("DELETE FROM episodes WHERE uid = ?", (uid,))
        self.assertEqual(self.store.execute("SELECT count(*) FROM episodes_fts").fetchone()[0], 1)

    def test_concurrent_writers(self):
        errors: list[Exception] = []

        def worker(index: int) -> None:
            try:
                for step in range(50):
                    self.store.remember(f"thread {index} ligne {step}", kind="stress")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(
            self.store.execute("SELECT count(*) FROM episodes WHERE kind='stress'").fetchone()[0],
            400,
        )

    def test_lamport_clock_advances_and_absorbs_remote(self):
        first = self.store.tick()
        self.assertEqual(self.store.tick(), first + 1)
        self.assertEqual(self.store.observe_lamport(9999), 10000)


class VectorTest(unittest.TestCase):
    def setUp(self):
        random.seed(7)
        self.dim = 128

    def _cosine(self, a, b):
        ua, ub = normalize(a), normalize(b)
        return sum(x * y for x, y in zip(ua, ub))

    def test_quantization_preserves_direction(self):
        worst = 0.0
        for _ in range(200):
            vector = [random.gauss(0, 1) for _ in range(self.dim)]
            scale, data = quantize(vector)
            self.assertEqual(len(data), self.dim)
            worst = max(worst, abs(self._cosine(vector, dequantize(scale, data)) - 1.0))
        self.assertLess(worst, 5e-3, f"erreur de cosinus trop grande : {worst}")

    def test_zero_vector_is_survivable(self):
        scale, data = quantize([0.0] * self.dim)
        self.assertEqual(data, bytes(self.dim))
        self.assertEqual(normalize([0.0, 0.0]), [0.0, 0.0])

    def test_nearest_neighbour_is_found(self):
        index = VectorIndex(dim=self.dim, capacity=1000)
        for i in range(300):
            scale, data = quantize([random.gauss(0, 1) for _ in range(self.dim)])
            index.add(f"u{i}", "episode", scale, data, float(i))
        base = [random.gauss(0, 1) for _ in range(self.dim)]
        scale, data = quantize([x + random.gauss(0, 0.05) for x in base])
        index.add("cible", "fact", scale, data, 999.0)

        hits = index.search(base, limit=5)
        self.assertEqual(hits[0].uid, "cible")
        self.assertGreater(hits[0].score, 0.9)
        self.assertNotEqual(index.search(base, limit=5, scope="episode")[0].uid, "cible")
        restricted = index.search(base, limit=3, restrict={"u1", "u2"})
        self.assertTrue(all(hit.uid in {"u1", "u2"} for hit in restricted))

    def test_removal_keeps_positions_coherent(self):
        index = VectorIndex(dim=self.dim, capacity=100)
        for i in range(20):
            scale, data = quantize([random.gauss(0, 1) for _ in range(self.dim)])
            index.add(f"u{i}", "episode", scale, data, float(i))
        self.assertTrue(index.remove("u0"))
        self.assertFalse(index.remove("u0"))
        self.assertEqual(len(index), 19)
        for uid in list(index._position):
            self.assertTrue(index.remove(uid))
        self.assertEqual(len(index), 0)
        self.assertEqual(index.search([1.0] * self.dim), [])

    def test_eviction_keeps_the_most_recent(self):
        index = VectorIndex(dim=self.dim, capacity=10)
        for i in range(25):
            scale, data = quantize([random.gauss(0, 1) for _ in range(self.dim)])
            index.add(f"v{i}", "episode", scale, data, float(i))
        self.assertEqual(len(index), 10)
        self.assertGreaterEqual(min(int(uid[1:]) for uid in index._position), 15)

    def test_dimension_mismatch_is_refused(self):
        index = VectorIndex(dim=self.dim)
        with self.assertRaises(ValueError):
            index.add("bad", "episode", 1.0, bytes(7), 0.0)
        with self.assertRaises(ValueError):
            index.search([1.0, 2.0])


class SearchTest(unittest.TestCase):
    def setUp(self):
        cfg, self.root = temp_config()
        self.memory = Memory(cfg, embedder=HashingEmbedder(cfg.memory.vector_dim))
        self.cfg = cfg
        for content, kind in [
            ("Le serveur de sauvegarde tourne sur le port 8443", "observation"),
            ("La machine a 16 Go de RAM et un disque de 500 Go", "observation"),
            ("Incident du 14 mars : le disque a saturé", "incident"),
            ("La météo à Orléans était pluvieuse", "note"),
        ]:
            self.memory.record(content, kind=kind)
        self.memory.learn("machine", "ram_go", "16", confidence=0.9)
        self.memory.learn("sauvegarde", "port", "8443", confidence=0.8)

    def tearDown(self):
        self.memory.close()

    def test_finds_rare_token_through_fulltext(self):
        results = self.memory.search("8443", limit=3)
        self.assertTrue(any("8443" in r.text for r in results))

    def test_ranks_the_incident_first(self):
        top = self.memory.search("saturation du disque", limit=1)[0]
        self.assertIn("Incident", top.text)

    def test_scope_filter(self):
        results = self.memory.search("machine ram", limit=3, scope="fact")
        self.assertTrue(results)
        self.assertTrue(all(r.scope == "fact" for r in results))

    def test_hostile_queries_are_absorbed(self):
        """Une requête libre contient des opérateurs FTS5 ; elle ne doit ni
        lever ni changer de sens."""
        for hostile in ['"', "a AND OR b", "NEAR(x", "*", 'foo" OR "1"="1', "((()))", "", "   "]:
            self.memory.search(hostile, limit=2)
        self.assertEqual(fts_query('foo" OR "1"="1'), '"foo" OR "OR" OR "1" OR "1"')
        self.assertEqual(fts_query(""), "")

    def test_rank_fusion_prefers_consensus(self):
        scores = reciprocal_rank_fusion([["a", "b", "c"], ["b", "c", "a"], ["b", "a", "c"]], k=60)
        self.assertEqual(max(scores, key=scores.get), "b")

    def test_context_block_respects_budget(self):
        """Le budget borne les souvenirs rappelés ; l'encadrement qui les
        présente s'y ajoute et reste de taille fixe."""
        recalled = [line for line in self.memory.context(
            "combien de RAM ?", limit=6, max_chars=120).splitlines()
            if line.startswith("- ")]
        self.assertTrue(recalled)
        self.assertLessEqual(sum(len(line) + 1 for line in recalled), 120)

    def test_context_is_empty_when_nothing_matches(self):
        """Sans plancher de similarité, la voie vectorielle rendrait ses plus
        proches voisins quelle que soit la question : le contexte se
        remplirait de hors-sujet, et un souvenir planté remonterait sur
        n'importe quelle requête."""
        self.assertEqual(self.memory.context("zzzzz introuvable qqqqq"), "")

    def test_relevance_floor_is_what_suppresses_stray_recall(self):
        self.assertEqual(self.memory.search("zzzzz introuvable qqqqq", limit=5), [])
        self.cfg.memory.min_similarity = -1.0      # plancher désactivé
        self.assertTrue(self.memory.search("zzzzz introuvable qqqqq", limit=5),
                        "sans plancher, la recherche vectorielle rend toujours des voisins")

    def test_lexical_fallback_does_not_bridge_synonyms(self):
        """Le repli par hachage rapproche des formes voisines, pas des
        synonymes : « mémoire vive » ne trouve pas « ram_go ». C'est la
        raison d'être du modèle d'embedding local, pas un défaut caché."""
        self.assertEqual(self.memory.search("mémoire vive", limit=3, scope="fact"), [])
        self.assertTrue(self.memory.search("machine ram_go", limit=3, scope="fact"))

    def test_forget_removes_from_every_index(self):
        uid = self.memory.record("à oublier absolument", kind="note")
        self.assertTrue(self.memory.forget(uid))
        self.assertFalse(any(r.uid == uid for r in self.memory.search("oublier", limit=5)))
        self.assertEqual(
            self.memory.store.execute(
                "SELECT count(*) FROM vectors WHERE uid = ?", (uid,)).fetchone()[0], 0)

    def test_index_survives_restart(self):
        before = len(self.memory.index)
        self.memory.store.close()
        again = Memory(self.cfg, embedder=HashingEmbedder(self.cfg.memory.vector_dim))
        self.assertEqual(len(again.index), before)
        self.assertIn("Incident", again.search("saturation du disque", limit=1)[0].text)

    def test_reindex_rebuilds_everything(self):
        self.assertEqual(self.memory.reindex(), 4 + 2)
        self.assertEqual(len(self.memory.index), 6)

    def test_compaction_purges_episodes_but_keeps_facts(self):
        self.cfg.memory.episodic_retention_days = 0
        time.sleep(0.01)
        self.assertEqual(self.memory.compact(), 4)
        stats = self.memory.store.stats()
        self.assertEqual(stats["episodes"], 0)
        self.assertEqual(stats["facts"], 2)
        self.assertEqual(stats["vectors"], 2, "les vecteurs des épisodes purgés partent aussi")
        self.assertTrue(self.memory.search("machine ram", limit=3))


class EmbedderTest(unittest.TestCase):
    def test_hashing_is_stable_across_processes(self):
        """L'index serait illisible au redémarrage si la projection dépendait
        de la graine de `hash()`."""
        one = HashingEmbedder(64).embed_one("mémoire distante")
        two = HashingEmbedder(64).embed_one("mémoire distante")
        self.assertEqual(one, two)
        self.assertEqual(len(one), 64)

    def test_similar_forms_are_closer_than_unrelated_text(self):
        embedder = HashingEmbedder(512)
        base = normalize(embedder.embed_one("installation du disque"))
        near = normalize(embedder.embed_one("installer le disque"))
        far = normalize(embedder.embed_one("météo pluvieuse à Orléans"))
        self.assertGreater(sum(x * y for x, y in zip(base, near)),
                           sum(x * y for x, y in zip(base, far)))


if __name__ == "__main__":
    unittest.main()
