# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Backend-free unit tests for ``bench_lib`` (percentile maths + table).

These deliberately need **no compiled anki backend and no third-party deps**, so
they run on a fresh checkout (where ``out/`` is absent) with just::

    python tools/bench/test_bench_lib.py

They lock down the percentile interpolation (so the p50/p95/worst numbers the
grader reads are trustworthy) and the PASS/FAIL verdicts against the §10
budgets, including the "no UI freeze > 100ms" rule.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bench_lib import (  # noqa: E402
    BUDGETS,
    Budget,
    Stat,
    all_passed,
    format_table,
    percentile,
    summarize,
)


class PercentileTests(unittest.TestCase):
    def test_single_sample(self) -> None:
        self.assertEqual(percentile([42.0], 50), 42.0)
        self.assertEqual(percentile([42.0], 95), 42.0)

    def test_median_odd_and_even(self) -> None:
        self.assertEqual(percentile([1, 2, 3], 50), 2.0)
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)

    def test_endpoints(self) -> None:
        data = [5, 1, 4, 2, 3]
        self.assertEqual(percentile(data, 0), 1.0)
        self.assertEqual(percentile(data, 100), 5.0)

    def test_linear_interpolation_matches_numpy(self) -> None:
        # numpy.percentile(range(1..10), 95) == 9.55 with the linear method.
        data = list(range(1, 11))
        self.assertAlmostEqual(percentile(data, 95), 9.55, places=6)
        self.assertAlmostEqual(percentile(data, 25), 3.25, places=6)

    def test_unsorted_input_is_ordered(self) -> None:
        self.assertEqual(percentile([9, 1, 5], 50), 5.0)

    def test_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            percentile([], 50)
        with self.assertRaises(ValueError):
            percentile([1.0], 101)


class StatTests(unittest.TestCase):
    def test_summary_fields(self) -> None:
        stat = summarize("button_ack", "Button", [10, 20, 30, 40])
        self.assertEqual(stat.n, 4)
        self.assertEqual(stat.worst, 40)
        self.assertEqual(stat.mean, 25)
        self.assertEqual(stat.p50, 25.0)


class TableTests(unittest.TestCase):
    def test_pass_when_within_budget(self) -> None:
        stats = [
            summarize("button_ack", BUDGETS["button_ack"].label, [10, 12, 20]),
            summarize("next_card", BUDGETS["next_card"].label, [30, 40, 55]),
            summarize(
                "dashboard_load", BUDGETS["dashboard_load"].label, [300, 500, 800]
            ),
            summarize(
                "dashboard_refresh",
                BUDGETS["dashboard_refresh"].label,
                [100, 200, 300],
            ),
        ]
        self.assertTrue(all_passed(stats))
        table = format_table(stats)
        self.assertIn("OVERALL: PASS", table)
        self.assertIn("p50 ms", table)

    def test_fail_when_p95_exceeds_budget(self) -> None:
        # p95 well above the 50ms button-ack cap.
        stats = [summarize("button_ack", "Button", [10, 10, 999])]
        self.assertFalse(all_passed(stats))
        self.assertIn("OVERALL: FAIL", format_table(stats))

    def test_interactive_freeze_rule(self) -> None:
        # p95 within 100ms budget for next_card, but a single 150ms worst sample
        # violates the "no UI freeze > 100ms" rule -> FAIL + explicit callout.
        samples = [10.0] * 99 + [150.0]
        stats = [summarize("next_card", BUDGETS["next_card"].label, samples)]
        row_passed = all_passed(stats)
        self.assertFalse(row_passed)
        table = format_table(stats)
        self.assertIn("UI FREEZE", table)

    def test_non_interactive_high_worst_is_not_a_freeze(self) -> None:
        # Dashboard load can legitimately spike past 100ms (budget is 1s); a high
        # worst there must NOT be treated as a UI freeze.
        stats = [
            summarize(
                "dashboard_load", BUDGETS["dashboard_load"].label, [200, 300, 900]
            )
        ]
        self.assertTrue(all_passed(stats))
        self.assertNotIn("UI FREEZE", format_table(stats))

    def test_unbudgeted_row_is_informational(self) -> None:
        stats = [Stat(key="misc", label="Misc", samples_ms=[1, 2, 3])]
        # No budget -> does not affect overall pass.
        self.assertTrue(all_passed(stats))
        self.assertIn("n/a", format_table(stats))

    def test_custom_budget_map(self) -> None:
        budgets = {"x": Budget("x", "X", 5.0, interactive=True)}
        stats = [summarize("x", "X", [1, 2, 3])]
        self.assertTrue(all_passed(stats, budgets))
        self.assertFalse(all_passed([summarize("x", "X", [1, 2, 99])], budgets))


if __name__ == "__main__":
    unittest.main()
