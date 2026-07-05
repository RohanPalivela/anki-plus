# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun one-command benchmark (WS2 / §7h / §10).

Loads the shared 50,000-card deck and measures, over many iterations, the four
actions the Plan sets hard latency budgets on, printing **p50 / p95 / worst**
for each next to its §10 budget with a PASS/FAIL verdict:

* button press acknowledged        (``sched.answerCard``)     p95 < 50ms
* next card appears after grading   (``sched.getCard``)        p95 < 100ms
* dashboard first load (cold)       (three score RPCs)         p95 < 1s
* dashboard refresh (warm)          (three score RPCs)         p95 < 500ms

The percentile maths + PASS/FAIL table live in the backend-free
``bench_lib`` module (unit-tested without a build). This driver only does the
timing, so "one hand-picked number" is impossible — every action reports its
whole distribution.

Run it (builds pylib first) with::

    just bench                                   # generates a 50k deck if needed
    just bench -- --deck /path/to/collection.anki2 --iterations 2000

See ``docs/speedrun/benchmark.md``.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from collections.abc import Callable

import bench_lib  # type: ignore[import-not-found]
from _bootstrap import ensure_anki_importable  # type: ignore[import-not-found]

ensure_anki_importable()

from anki.cards import Card  # noqa: E402
from anki.collection import Collection  # noqa: E402


def _time_ms(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000.0


def _bench_button_and_next(col: Collection, iterations: int) -> list[bench_lib.Stat]:
    """Measure answerCard (button ack) and getCard (next card) latencies."""
    button_samples: list[float] = []
    next_samples: list[float] = []
    for _ in range(iterations):
        # Time fetching the next card...
        card_box: list[Card | None] = []
        next_samples.append(_time_ms(lambda: card_box.append(col.sched.getCard())))
        card = card_box[0]
        if card is None:
            break
        # ...then acknowledging a grade on it (Good).
        button_samples.append(
            _time_ms(lambda: col.sched.answerCard(card, 3, from_queue=False))
        )
    return [
        bench_lib.summarize(
            "button_ack", "Button press ack (answerCard)", button_samples
        ),
        bench_lib.summarize("next_card", "Next card appears (queue)", next_samples),
    ]


def _bench_dashboard(col: Collection, iterations: int) -> list[bench_lib.Stat]:
    """Measure cold (first) and warm (refresh) dashboard score-RPC latency."""

    def load_all() -> None:
        col.speedrun.get_memory_score()
        col.speedrun.get_performance_score()
        col.speedrun.get_readiness_score()

    cold = _time_ms(load_all)
    warm_samples = [_time_ms(load_all) for _ in range(iterations)]
    mem = col.speedrun.get_memory_score()
    note = "abstained" if getattr(mem, "abstained", False) else ""
    return [
        bench_lib.summarize(
            "dashboard_load", "Dashboard first load (cold)", [cold], note
        ),
        bench_lib.summarize(
            "dashboard_refresh", "Dashboard refresh (warm)", warm_samples, note
        ),
    ]


def run(deck: str, iterations: int, dashboard_iterations: int) -> bool:
    col = Collection(deck)
    try:
        card_count = col.card_count()
        print(f"Collection: {deck}  ({card_count} cards)\n")
        if card_count < 40_000:
            print(
                "WARNING: fewer than 40k cards — §10 budgets are defined on a 50k "
                "deck. Generate one with tools/bench/gen_deck.py for a valid run.\n"
            )
        stats = _bench_button_and_next(col, iterations)
        stats += _bench_dashboard(col, dashboard_iterations)
        print(bench_lib.format_table(stats))
        return bench_lib.all_passed(stats)
    finally:
        col.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Speedrun p50/p95/worst benchmark")
    ap.add_argument(
        "--deck",
        help="path to a .anki2 collection; if omitted, a 50k deck is generated",
    )
    ap.add_argument("--iterations", type=int, default=2_000)
    ap.add_argument("--dashboard-iterations", type=int, default=200)
    ap.add_argument("--count", type=int, default=50_000, help="cards to generate")
    args = ap.parse_args()

    deck = args.deck
    tmp_created = False
    if not deck:
        import gen_deck  # type: ignore[import-not-found]

        deck = os.path.join(tempfile.mkdtemp(prefix="speedrun-bench-"), "col.anki2")
        print(f"No --deck given; generating a {args.count}-card deck at {deck}\n")
        gen_deck.generate(deck, count=args.count)
        tmp_created = True

    passed = run(deck, args.iterations, args.dashboard_iterations)
    if tmp_created:
        print(f"\n(generated deck left at {deck} for re-runs; delete when done)")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
