"""File de travaux, reprises, plannings et ouvriers."""

from __future__ import annotations

import threading
import time
import unittest

from agentos.memory.store import Store
from agentos.scheduler import DEAD, DONE, PENDING, RUNNING, Scheduler
from agentos.scheduler.cron import CronError
from agentos.scheduler.queue import JobQueue

from .fakes import temp_config


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = temp_config()
        self.store = Store(self.cfg.memory.database, node="banc")
        self.queue = JobQueue(self.store, max_attempts=3, backoff_base=0.01, backoff_cap=0.05)

    def test_enqueue_then_claim_in_order(self):
        first = self.queue.enqueue("a", {"x": 1})
        second = self.queue.enqueue("b")
        self.assertEqual(self.queue.claim().uid, first)
        self.assertEqual(self.queue.claim().uid, second)
        self.assertIsNone(self.queue.claim())

    def test_payload_round_trip(self):
        self.queue.enqueue("a", {"texte": "accentué", "n": 3})
        self.assertEqual(self.queue.claim().payload, {"texte": "accentué", "n": 3})

    def test_delayed_job_is_not_claimable_yet(self):
        self.queue.enqueue("plus_tard", delay_s=30)
        self.assertIsNone(self.queue.claim())

    def test_claim_is_atomic_under_contention(self):
        """Deux ouvriers ne doivent jamais repartir avec le même travail."""
        for index in range(200):
            self.queue.enqueue(f"job{index}")
        claimed: list[str] = []
        lock = threading.Lock()

        def worker():
            while True:
                job = self.queue.claim()
                if job is None:
                    return
                with lock:
                    claimed.append(job.uid)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(claimed), 200)
        self.assertEqual(len(set(claimed)), 200, "un travail a été pris deux fois")

    def test_failure_retries_then_dies(self):
        uid = self.queue.enqueue("a")
        for expected in (True, True):          # tentatives 1 et 2 sur 3
            self.queue.claim()
            self.assertEqual(self.queue.fail(uid, "boum"), expected)
            time.sleep(0.06)                   # laisse passer le backoff
        self.queue.claim()
        self.assertFalse(self.queue.fail(uid, "boum"), "la 3e doit être la dernière")
        self.assertEqual(self.queue.get(uid)["state"], DEAD)

    def test_failure_without_retry_dies_immediately(self):
        uid = self.queue.enqueue("a")
        self.queue.claim()
        self.assertFalse(self.queue.fail(uid, "définitif", retry=False))
        self.assertEqual(self.queue.get(uid)["state"], DEAD)

    def test_backoff_grows_and_is_capped(self):
        delays = [self.queue.backoff(n) for n in range(1, 12)]
        self.assertLess(delays[0], delays[4])
        self.assertTrue(all(d <= self.queue.backoff_cap for d in delays))

    def test_completion_records_result(self):
        uid = self.queue.enqueue("a")
        self.queue.claim()
        self.queue.complete(uid, {"ok": True})
        row = self.queue.get(uid)
        self.assertEqual(row["state"], DONE)
        self.assertIn("ok", row["result"])

    def test_stale_running_jobs_are_requeued(self):
        """Un travail interrompu par une coupure n'a plus personne pour le
        terminer : il doit revenir en file, pas rester bloqué."""
        uid = self.queue.enqueue("a")
        self.queue.claim()
        self.store.write("UPDATE jobs SET started = ? WHERE uid = ?", (time.time() - 5000, uid))
        self.assertEqual(self.queue.reap_stale(timeout_s=60), 1)
        self.assertEqual(self.queue.get(uid)["state"], PENDING)

    def test_stale_job_with_no_attempts_left_dies(self):
        uid = self.queue.enqueue("a", max_attempts=1)
        self.queue.claim()
        self.store.write("UPDATE jobs SET started = ? WHERE uid = ?", (time.time() - 5000, uid))
        self.queue.reap_stale(timeout_s=60)
        self.assertEqual(self.queue.get(uid)["state"], DEAD)

    def test_purge_keeps_dead_jobs_for_diagnosis(self):
        done = self.queue.enqueue("ok")
        self.queue.claim()
        self.queue.complete(done)
        dead = self.queue.enqueue("ko")
        self.queue.claim()
        self.queue.fail(dead, "boum", retry=False)
        old = time.time() - 40 * 86400
        self.store.write("UPDATE jobs SET finished = ?", (old,))
        self.assertEqual(self.queue.purge(older_than_days=30), 1)
        self.assertIsNotNone(self.queue.get(dead))


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.cfg, _ = temp_config()
        self.cfg.scheduler.tick_s = 0.05
        self.cfg.scheduler.concurrency = 3
        self.cfg.scheduler.max_attempts = 2
        self.cfg.scheduler.backoff_base_s = 0.01
        self.store = Store(self.cfg.memory.database, node="banc")
        self.sched = Scheduler(self.store, self.cfg)

    def tearDown(self):
        self.sched.stop(timeout=2)

    def test_runs_a_job_through_its_handler(self):
        seen = []
        self.sched.register("compter", lambda job, deadline: seen.append(job.payload["n"]) or "fini")
        uid = self.sched.queue.enqueue("compter", {"n": 7})
        self.assertTrue(self.sched.run_one())
        self.assertEqual(seen, [7])
        self.assertEqual(self.sched.queue.get(uid)["state"], DONE)

    def test_handler_exception_becomes_a_retry(self):
        def explode(job, deadline):
            raise ValueError("cassé")

        self.sched.register("explose", explode)
        uid = self.sched.queue.enqueue("explose")
        self.sched.run_one()
        row = self.sched.queue.get(uid)
        self.assertEqual(row["state"], PENDING)
        self.assertIn("cassé", row["error"])

    def test_unknown_handler_dies_without_retry(self):
        uid = self.sched.queue.enqueue("inexistant")
        self.sched.run_one()
        self.assertEqual(self.sched.queue.get(uid)["state"], DEAD)

    def test_handler_receives_a_deadline(self):
        captured = []
        self.sched.register("t", lambda job, deadline: captured.append(deadline))
        self.sched.queue.enqueue("t")
        self.sched.run_one()
        self.assertGreater(captured[0], time.time())

    def test_invalid_cron_is_refused_at_creation(self):
        self.sched.register("x", lambda job, deadline: None)
        with self.assertRaises(CronError):
            self.sched.add_schedule("mauvais", "99 * * * *", "x")
        self.assertEqual(self.sched.list_schedules(), [])

    def test_due_schedule_enqueues_a_job(self):
        self.sched.register("sauvegarde", lambda job, deadline: "ok")
        self.sched.add_schedule("nuit", "0 3 * * *", "sauvegarde", {"cible": "/var"})
        # On force l'échéance dans le passé, comme après une nuit d'arrêt.
        self.store.write("UPDATE schedules SET next_fire = ?", (time.time() - 10,))
        self.assertEqual(self.sched.fire_due(), 1)

        job = self.sched.queue.claim()
        self.assertEqual(job.name, "sauvegarde")
        self.assertEqual(job.payload["cible"], "/var")
        self.assertEqual(job.payload["_schedule"], "nuit")
        self.assertTrue(job.origin.startswith("planning:"))

        # L'échéance suivante repart dans le futur : pas de rejeu en boucle.
        self.assertGreater(self.sched.get_schedule("nuit")["next_fire"], time.time())
        self.assertEqual(self.sched.fire_due(), 0)

    def test_disabled_schedule_does_not_fire(self):
        self.sched.register("x", lambda job, deadline: None)
        self.sched.add_schedule("off", "* * * * *", "x", enabled=False)
        self.store.write("UPDATE schedules SET next_fire = ?", (time.time() - 10,))
        self.assertEqual(self.sched.fire_due(), 0)

    def test_schedule_corrupted_in_database_is_disabled_not_fatal(self):
        self.sched.register("x", lambda job, deadline: None)
        self.sched.add_schedule("ok", "* * * * *", "x")
        self.store.write("UPDATE schedules SET cron = ?, next_fire = ?",
                         ("expression cassée", time.time() - 10))
        self.assertEqual(self.sched.fire_due(), 0)
        self.assertEqual(self.sched.get_schedule("ok")["enabled"], 0)

    def test_replacing_a_schedule_keeps_one_row(self):
        self.sched.register("x", lambda job, deadline: None)
        self.sched.add_schedule("n", "0 3 * * *", "x")
        self.sched.add_schedule("n", "0 4 * * *", "x")
        self.assertEqual(len(self.sched.list_schedules()), 1)
        self.assertEqual(self.sched.get_schedule("n")["cron"], "0 4 * * *")

    def test_workers_drain_the_queue_concurrently(self):
        done = []
        lock = threading.Lock()

        def slow(job, deadline):
            time.sleep(0.02)
            with lock:
                done.append(job.uid)
            return "ok"

        self.sched.register("lent", slow)
        for _ in range(24):
            self.sched.queue.enqueue("lent")
        self.sched.start()
        deadline = time.time() + 10
        while len(done) < 24 and time.time() < deadline:
            time.sleep(0.02)
        self.sched.stop(timeout=3)
        self.assertEqual(len(done), 24)
        self.assertEqual(self.sched.queue.counts().get(DONE), 24)

    def test_status_reports_next_schedule(self):
        self.sched.register("x", lambda job, deadline: None)
        self.sched.add_schedule("nuit", "0 3 * * *", "x")
        status = self.sched.status()
        self.assertEqual(status["prochain"]["nom"], "nuit")
        self.assertIn("x", status["gestionnaires"])
        self.assertFalse(status["en_marche"])


if __name__ == "__main__":
    unittest.main()
