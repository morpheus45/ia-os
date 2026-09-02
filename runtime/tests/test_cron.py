"""Analyse cron et calcul du prochain déclenchement."""

from __future__ import annotations

import unittest
from datetime import datetime

from agentos.scheduler.cron import Cron, CronError, validate


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


class ParsingTest(unittest.TestCase):
    def test_wildcards_and_steps(self):
        cron = Cron("*/15 * * * *")
        self.assertEqual(sorted(cron.minutes), [0, 15, 30, 45])
        self.assertEqual(len(cron.hours), 24)

    def test_ranges_lists_and_combinations(self):
        self.assertEqual(sorted(Cron("1,3,5 * * * *").minutes), [1, 3, 5])
        self.assertEqual(sorted(Cron("0 9-17 * * *").hours), list(range(9, 18)))
        self.assertEqual(sorted(Cron("0 0-23/6 * * *").hours), [0, 6, 12, 18])

    def test_bare_step_runs_to_end_of_field(self):
        """« 5/15 » veut dire « à partir de 5, tous les quarts d'heure »."""
        self.assertEqual(sorted(Cron("5/15 * * * *").minutes), [5, 20, 35, 50])

    def test_named_months_and_days(self):
        self.assertEqual(sorted(Cron("0 0 * jan,dec *").months), [1, 12])
        self.assertEqual(sorted(Cron("0 0 * * mon-fri").weekdays), [1, 2, 3, 4, 5])

    def test_sunday_accepts_both_zero_and_seven(self):
        self.assertEqual(Cron("0 0 * * 7").weekdays, {0})
        self.assertEqual(Cron("0 0 * * 0").weekdays, {0})

    def test_shortcuts(self):
        self.assertEqual(Cron("@daily").next_after(at("2026-03-01 12:00")), at("2026-03-02 00:00"))
        self.assertEqual(Cron("@hourly").next_after(at("2026-03-01 12:30")), at("2026-03-01 13:00"))

    def test_rejects_malformed_expressions(self):
        for bad in ["* * * *", "* * * * * *", "60 * * * *", "* 24 * * *",
                    "5-2 * * * *", "*/0 * * * *", "abc * * * *", "", "0 0 0 * *"]:
            with self.assertRaises(CronError, msg=f"« {bad} » aurait dû être refusée"):
                Cron(bad)

    def test_error_names_the_offending_field(self):
        with self.assertRaises(CronError) as caught:
            Cron("0 99 * * *")
        self.assertIn("heure", str(caught.exception))

    def test_validate_returns_normalised_expression(self):
        self.assertEqual(validate("  */5 * * * *  "), "*/5 * * * *")


class NextFireTest(unittest.TestCase):
    def test_next_minute_and_next_day(self):
        self.assertEqual(Cron("0 3 * * *").next_after(at("2026-03-01 02:59")), at("2026-03-01 03:00"))
        self.assertEqual(Cron("0 3 * * *").next_after(at("2026-03-01 03:00")), at("2026-03-02 03:00"))

    def test_is_strictly_after_the_reference(self):
        """Sans cette stricte inégalité, une tâche se redéclencherait en
        boucle dans la minute où elle vient de tourner."""
        moment = at("2026-03-01 03:00")
        self.assertGreater(Cron("0 3 * * *").next_after(moment), moment)

    def test_weekday_only(self):
        # 2026-03-02 est un lundi.
        self.assertEqual(Cron("0 0 * * mon").next_after(at("2026-03-01 12:00")), at("2026-03-02 00:00"))

    def test_day_of_month_or_weekday_when_both_restricted(self):
        """Sémantique cron historique : les deux champs restreints se
        combinent par OU, pas par ET."""
        cron = Cron("0 0 15 * mon")
        # 2026-03-02 (lundi) arrive avant le 15 du mois.
        self.assertEqual(cron.next_after(at("2026-03-01 12:00")), at("2026-03-02 00:00"))
        # Le 15 mars 2026 est un dimanche : il déclenche quand même.
        self.assertEqual(cron.next_after(at("2026-03-09 12:00")), at("2026-03-15 00:00"))

    def test_day_of_month_and_weekday_when_only_one_restricted(self):
        cron = Cron("0 0 15 * *")
        self.assertEqual(cron.next_after(at("2026-03-01 00:00")), at("2026-03-15 00:00"))

    def test_leap_day_skips_three_years(self):
        found = Cron("0 0 29 2 *").next_after(at("2026-03-01 00:00"))
        self.assertEqual(found, at("2028-02-29 00:00"))

    def test_month_restriction_jumps_forward(self):
        self.assertEqual(Cron("0 0 1 1 *").next_after(at("2026-06-15 12:00")), at("2027-01-01 00:00"))

    def test_impossible_expression_returns_none(self):
        self.assertIsNone(Cron("0 0 30 2 *").next_after(at("2026-01-01 00:00")))

    def test_hour_rollover_within_a_day(self):
        cron = Cron("30 9,17 * * *")
        self.assertEqual(cron.next_after(at("2026-03-01 09:30")), at("2026-03-01 17:30"))
        self.assertEqual(cron.next_after(at("2026-03-01 17:30")), at("2026-03-02 09:30"))

    def test_end_of_year_rollover(self):
        self.assertEqual(Cron("0 0 * * *").next_after(at("2026-12-31 12:00")), at("2027-01-01 00:00"))

    def test_matches_agrees_with_next_after(self):
        """Toute date produite doit satisfaire l'expression : les deux
        chemins de code ne doivent pas diverger."""
        for expression in ["*/15 * * * *", "0 3 * * *", "0 0 15 * mon",
                           "30 9,17 * * mon-fri", "0 0 1 jan,jul *", "@weekly"]:
            cron = Cron(expression)
            moment = at("2026-01-01 00:00")
            for _ in range(40):
                moment = cron.next_after(moment)
                self.assertIsNotNone(moment, expression)
                self.assertTrue(cron.matches(moment), f"{expression} → {moment}")

    def test_resolution_is_fast_for_rare_expressions(self):
        import time
        started = time.monotonic()
        Cron("7 3 29 2 *").next_after(at("2026-03-01 00:00"))
        self.assertLess(time.monotonic() - started, 0.05)


if __name__ == "__main__":
    unittest.main()
