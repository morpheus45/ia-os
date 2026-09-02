"""Synchronisation distante : envoi, réception, conflits, confiance."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from agentos.memory import crypto
from agentos.memory.store import Store
from agentos.memory.sync import Synchroniser, _epoch

from .fakes import temp_config


class FakeBackend:
    """Mémoire distante en RAM, avec la même surface que Supabase."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict]] = {
            "agentos_episodes": {}, "agentos_facts": {}}
        self.nodes: dict[str, dict] = {}
        self.log_lines: list[tuple] = []

    def upsert(self, table, rows):
        for row in rows:
            self.tables[table][row["uid"]] = dict(row)

    def select(self, table, *, since, exclude_node, limit):
        rows = [row for row in self.tables[table].values()
                if int(row.get("lamport") or 0) > since and row.get("node") != exclude_node]
        return sorted(rows, key=lambda r: r["lamport"])[:limit]

    def register_node(self, node, label, version):
        self.nodes[node] = {"label": label, "version": version}

    def upsert_nodes(self, rows):
        for row in rows:
            self.nodes[row["node"]] = row

    def log(self, node, direction, scope, rows, error=""):
        self.log_lines.append((node, direction, scope, rows, error))

    # -- fabrication de lignes distantes ---------------------------------

    def plant_episode(self, uid, content, *, node, lamport, trust="agent"):
        self.tables["agentos_episodes"][uid] = {
            "uid": uid, "node": node, "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "note", "actor": "agent", "session": None, "content": content,
            "meta": {}, "lamport": lamport, "trust": trust, "encrypted": False,
        }

    def plant_fact(self, uid, subject, predicate, object_, *, node, lamport, trust="agent"):
        now = datetime.now(timezone.utc).isoformat()
        self.tables["agentos_facts"][uid] = {
            "uid": uid, "node": node, "subject": subject, "predicate": predicate,
            "object": object_, "confidence": 0.8, "source_uid": None,
            "created": now, "updated": now, "revoked": False,
            "lamport": lamport, "trust": trust, "encrypted": False,
        }


def make(node="machine-a", **remote):
    cfg, _ = temp_config()
    cfg.remote.enabled = True
    cfg.remote.node_id = node
    cfg.remote.encrypt = False
    cfg.remote.url = "https://exemple.supabase.co"
    cfg.remote_api_key = "clef-de-service"
    for key, value in remote.items():
        setattr(cfg.remote, key, value)
    store = Store(cfg.memory.database, node=node)
    backend = FakeBackend()
    return cfg, store, backend, Synchroniser(store, cfg, backend=backend)


class PushTest(unittest.TestCase):
    def test_pushes_then_marks_as_synced(self):
        cfg, store, backend, sync = make()
        store.remember("premier souvenir", trust="operator")
        store.assert_fact("machine", "ram_go", "16")

        report = sync.push()
        self.assertEqual(report.pushed, 2)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(len(backend.tables["agentos_episodes"]), 1)
        self.assertEqual(
            store.execute("SELECT count(*) FROM episodes WHERE synced=0").fetchone()[0], 0)

        self.assertEqual(sync.push().pushed, 0, "rien à repousser une fois marqué")

    def test_pushed_row_carries_node_and_trust(self):
        cfg, store, backend, sync = make(node="machine-a")
        store.remember("consigne", trust="operator")
        sync.push()
        row = next(iter(backend.tables["agentos_episodes"].values()))
        self.assertEqual(row["node"], "machine-a")
        self.assertEqual(row["trust"], "operator")
        self.assertFalse(row["encrypted"])

    def test_updated_fact_is_pushed_again(self):
        cfg, store, backend, sync = make()
        store.assert_fact("machine", "ram_go", "16")
        sync.push()
        store.assert_fact("machine", "ram_go", "16")     # réaffirmation → synced=0
        self.assertEqual(sync.push().pushed, 1)


class PullTest(unittest.TestCase):
    def test_pulls_and_applies_remote_rows(self):
        cfg, store, backend, sync = make(node="machine-a")
        backend.plant_episode("E1", "écrit sur la machine B", node="machine-b", lamport=5)
        report = sync.pull()
        self.assertEqual(report.pulled, 1)
        self.assertEqual(store.episode("E1")["content"], "écrit sur la machine B")

    def test_does_not_pull_back_its_own_rows(self):
        cfg, store, backend, sync = make(node="machine-a")
        store.remember("le mien")
        sync.push()
        self.assertEqual(sync.pull().pulled, 0)

    def test_cursor_advances_so_rows_are_not_replayed(self):
        cfg, store, backend, sync = make()
        backend.plant_episode("E1", "un", node="machine-b", lamport=3)
        self.assertEqual(sync.pull().pulled, 1)
        self.assertEqual(sync.pull().pulled, 0)
        backend.plant_episode("E2", "deux", node="machine-b", lamport=9)
        self.assertEqual(sync.pull().pulled, 1)

    def test_local_clock_absorbs_remote_values(self):
        """Sans cela, les prochaines écritures locales perdraient
        systématiquement l'arbitrage contre les lignes déjà reçues."""
        cfg, store, backend, sync = make()
        backend.plant_episode("E1", "loin devant", node="machine-b", lamport=10_000)
        sync.pull()
        self.assertGreater(store.tick(), 10_000)

    def test_malformed_remote_row_is_skipped_not_fatal(self):
        cfg, store, backend, sync = make()
        backend.tables["agentos_episodes"]["CASSE"] = {
            "uid": "CASSE", "node": "machine-b", "lamport": 4}   # colonnes manquantes
        backend.plant_episode("E1", "valide", node="machine-b", lamport=5)
        report = sync.pull()
        self.assertEqual(report.pulled, 1)
        self.assertIsNotNone(store.episode("E1"))


class TrustTest(unittest.TestCase):
    def test_remote_operator_claim_is_capped(self):
        """Compromettre une seule machine ne doit pas suffire à injecter des
        consignes réputées fiables dans toute la flotte."""
        cfg, store, backend, sync = make(trust_ceiling="external")
        backend.plant_episode("E1", "consigne officielle", node="machine-b",
                              lamport=5, trust="operator")
        sync.pull()
        self.assertEqual(store.episode("E1")["trust"], "external")

    def test_ceiling_never_upgrades_trust(self):
        cfg, store, backend, sync = make(trust_ceiling="agent")
        backend.plant_episode("E1", "note externe", node="machine-b",
                              lamport=5, trust="external")
        sync.pull()
        self.assertEqual(store.episode("E1")["trust"], "external",
                         "le plafond rabaisse, il ne promeut jamais")

    def test_unknown_remote_trust_is_treated_as_worst(self):
        cfg, store, backend, sync = make(trust_ceiling="agent")
        backend.plant_episode("E1", "x", node="machine-b", lamport=5, trust="root")
        sync.pull()
        self.assertEqual(store.episode("E1")["trust"], "external")

    def test_invalid_ceiling_is_refused_at_construction(self):
        cfg, store, backend, _ = make()
        cfg.remote.trust_ceiling = "administrateur"
        with self.assertRaises(ValueError):
            Synchroniser(store, cfg, backend=backend)


class ConflictTest(unittest.TestCase):
    def test_higher_lamport_wins(self):
        cfg, store, backend, sync = make(node="machine-a")
        store.remember("version locale", uid="E1")
        store.write("UPDATE episodes SET lamport = 5, node = 'machine-a' WHERE uid = 'E1'")
        backend.plant_episode("E1", "version distante", node="machine-b", lamport=9)
        report = sync.pull()
        self.assertEqual(report.conflicts, 1)
        self.assertEqual(store.episode("E1")["content"], "version distante")

    def test_lower_lamport_is_ignored(self):
        cfg, store, backend, sync = make(node="machine-a")
        store.remember("version locale", uid="E1")
        store.write("UPDATE episodes SET lamport = 20, node = 'machine-a' WHERE uid = 'E1'")
        backend.plant_episode("E1", "version distante", node="machine-b", lamport=9)
        sync.pull()
        self.assertEqual(store.episode("E1")["content"], "version locale")

    def test_tie_is_broken_deterministically_by_node(self):
        """Deux machines hors ligne doivent aboutir au même état, quel que
        soit l'ordre dans lequel elles se reconnectent."""
        results = []
        for local_node, remote_node in (("machine-a", "machine-b"), ("machine-b", "machine-a")):
            cfg, store, backend, sync = make(node=local_node)
            store.remember(f"écrit par {local_node}", uid="E1")
            store.write("UPDATE episodes SET lamport = 7, node = ? WHERE uid = 'E1'",
                        (local_node,))
            backend.plant_episode("E1", f"écrit par {remote_node}",
                                  node=remote_node, lamport=7)
            sync.pull()
            results.append(store.episode("E1")["content"])
        self.assertEqual(results[0], results[1],
                         "les deux machines doivent converger vers la même version")
        self.assertEqual(results[0], "écrit par machine-b")


@unittest.skipUnless(crypto.available(), "cryptography inutilisable dans cet environnement")
class CryptoTest(unittest.TestCase):
    def test_round_trip(self):
        key = crypto.derive_key("phrase secrète de l'opérateur")
        sealed = crypto.encrypt("mémoire confidentielle", key)
        self.assertNotIn("confidentielle", sealed)
        self.assertEqual(crypto.decrypt(sealed, key), "mémoire confidentielle")

    def test_wrong_key_fails_loudly(self):
        sealed = crypto.encrypt("secret", crypto.derive_key("bonne"))
        with self.assertRaises(Exception):
            crypto.decrypt(sealed, crypto.derive_key("mauvaise"))

    def test_tampering_is_detected(self):
        import base64
        key = crypto.derive_key("clé")
        raw = bytearray(base64.b64decode(crypto.encrypt("intact", key)))
        raw[-1] ^= 0xFF
        with self.assertRaises(Exception):
            crypto.decrypt(base64.b64encode(bytes(raw)).decode(), key)

    def test_foreign_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            crypto.decrypt("bm90IG91cnM=", crypto.derive_key("clé"))

    def test_empty_passphrase_is_refused(self):
        with self.assertRaises(ValueError):
            crypto.derive_key("")

    def test_encrypted_round_trip_through_sync(self):
        cfg, store, backend, _ = make()
        cfg.remote.encrypt = True
        cfg.remote_key = "phrase de test"
        sync = Synchroniser(store, cfg, backend=backend)

        store.remember("contenu à protéger")
        sync.push()
        stored = next(iter(backend.tables["agentos_episodes"].values()))
        self.assertTrue(stored["encrypted"])
        self.assertNotIn("protéger", stored["content"])

        # Une autre machine, même clé : elle doit pouvoir relire.
        cfg_b, store_b, _, _ = make(node="machine-b")
        cfg_b.remote.encrypt = True
        cfg_b.remote_key = "phrase de test"
        other = Synchroniser(store_b, cfg_b, backend=backend)
        other.pull()
        self.assertEqual(store_b.episode(stored["uid"])["content"], "contenu à protéger")


class CryptoProbeTest(unittest.TestCase):
    def test_probe_exercises_a_real_round_trip(self):
        """Une installation aux liaisons natives incomplètes laisse
        « import cryptography » réussir puis échoue au premier chiffrement.
        La sonde doit donc chiffrer pour de bon."""
        self.assertIsInstance(crypto.available(), bool)
        if crypto.available():
            key = crypto.derive_key("x")
            self.assertEqual(crypto.decrypt(crypto.encrypt("y", key), key), "y")

    def test_sync_refuses_to_start_rather_than_send_plaintext(self):
        cfg, store, backend, _ = make()
        cfg.remote.encrypt = True
        cfg.remote_key = "phrase"
        original = crypto.available
        crypto.available = lambda: False
        try:
            with self.assertRaises(crypto.CryptoUnavailable):
                Synchroniser(store, cfg, backend=backend)
        finally:
            crypto.available = original


class StatusTest(unittest.TestCase):
    def test_reports_pending_and_settings(self):
        cfg, store, backend, sync = make()
        store.remember("en attente")
        status = sync.status()
        self.assertEqual(status["en_attente"], 1)
        self.assertEqual(status["noeud"], "machine-a")
        self.assertFalse(status["chiffre"])
        self.assertEqual(status["plafond_confiance"], "external")

    def test_full_cycle_registers_the_node(self):
        cfg, store, backend, sync = make()
        store.remember("x")
        report = sync.run()
        self.assertTrue(report.ok, report.errors)
        self.assertIn("machine-a", backend.nodes)
        self.assertIn("poussé", report.summary())


class TimestampTest(unittest.TestCase):
    def test_parses_postgrest_shapes(self):
        self.assertAlmostEqual(_epoch("1970-01-01T00:00:10+00:00"), 10.0)
        self.assertAlmostEqual(_epoch("1970-01-01T00:00:10Z"), 10.0)
        self.assertAlmostEqual(_epoch(1234.5), 1234.5)
        self.assertGreater(_epoch("pas une date"), 0)


if __name__ == "__main__":
    unittest.main()
