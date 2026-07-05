# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Generate a large synthetic Anki collection for the Speedrun benchmark (WS2).

The §10 performance budgets are graded on a **50,000-card** deck, so the
benchmark needs a deterministic, reproducible deck of that size. This builds one
with the real Anki Python API (native notes/cards, so the scheduler, FSRS and
the Speedrun score RPCs all see genuine data), not a hand-rolled SQLite blob.

Cards are ``Basic`` notes spread across the MCAT blueprint topics via ``topic::``
tags (so per-topic mastery + coverage have something to chew on). A configurable
fraction is left **activated** (unsuspended, i.e. due) — the rest start
suspended, mirroring Speedrun's "off by default" model — and the activated ones
are given a spread of review history so the memory model is exercised.

Usage (must run under the built interpreter — see ``docs/speedrun/benchmark.md``):

    python tools/bench/gen_deck.py --out /tmp/speedrun_bench.anki2 --count 50000

Determinism: a fixed ``--seed`` makes the topic assignment, activation choice and
review history reproducible run to run.
"""

from __future__ import annotations

import argparse
import random
import time

from _bootstrap import ensure_anki_importable

ensure_anki_importable()

from anki.collection import Collection  # noqa: E402
from anki.decks import DeckId  # noqa: E402

# The MCAT blueprint topics (bare names; the Speedrun engine keys on ``topic::``).
# Kept in sync with pylib/anki/data/speedrun_concepts.json at a coarse level; the
# exact set does not matter for a latency benchmark, only the spread + volume.
TOPICS = [
    "biochem",
    "biology",
    "orgo",
    "gen-chem",
    "physics",
    "psych",
    "sociology",
    "physio",
    "genetics",
    "cell-bio",
    "molecular",
    "anatomy",
    "stats",
    "research-methods",
]

DEFAULT_COUNT = 50_000
DEFAULT_ACTIVATED_FRACTION = 0.30
BATCH = 2_000


def generate(
    out_path: str,
    count: int = DEFAULT_COUNT,
    activated_fraction: float = DEFAULT_ACTIVATED_FRACTION,
    seed: int = 1234,
) -> None:
    rng = random.Random(seed)
    col = Collection(out_path)
    try:
        basic = col.models.by_name("Basic")
        if basic is None:
            raise SystemExit("Basic note type missing from a fresh collection?")
        deck_id = DeckId(col.decks.id("Speedrun::Bench"))

        started = time.time()
        suspend_ids: list[int] = []
        review_targets: list[int] = []

        for base in range(0, count, BATCH):
            n = min(BATCH, count - base)
            for i in range(n):
                idx = base + i
                topic = TOPICS[idx % len(TOPICS)]
                note = col.new_note(basic)
                note["Front"] = f"Bench Q{idx} ({topic})"
                note["Back"] = f"Bench A{idx}"
                note.tags = [f"topic::{topic}"]
                col.add_note(note, deck_id)
                card_ids = [c.id for c in note.cards()]
                if rng.random() < activated_fraction:
                    review_targets.extend(card_ids)
                else:
                    suspend_ids.extend(card_ids)
            # Commit each batch so memory stays flat on 50k cards.
            col.save()
            print(f"  generated {min(base + n, count)}/{count} cards", flush=True)

        # Suspend the "off by default" majority in one atomic op.
        if suspend_ids:
            col.sched.suspend_cards(suspend_ids)

        # Give the activated cards a spread of review history so the memory model
        # (FSRS retrievability) and dashboard RPCs have real state to aggregate.
        _seed_reviews(col, review_targets, rng)

        col.save()
        elapsed = time.time() - started
        print(
            f"Done: {count} cards ({len(review_targets)} activated) "
            f"in {elapsed:.1f}s -> {out_path}"
        )
    finally:
        col.close()


def _seed_reviews(col: Collection, card_ids: list[int], rng: random.Random) -> None:
    """Answer a sample of activated cards so they carry FSRS state + revlog rows."""
    # Reviewing 50k cards would dominate generation time; a representative
    # sample is enough to exercise the memory/performance aggregations.
    sample = card_ids if len(card_ids) <= 5_000 else rng.sample(card_ids, 5_000)
    answered = 0
    for cid in sample:
        card = col.get_card(cid)
        # Bias toward "Good" so mastery is non-degenerate but not all-perfect.
        ease = 3 if rng.random() < 0.75 else 1
        try:
            col.sched.answerCard(card, ease, from_queue=False)
            answered += 1
        except Exception:  # noqa: BLE001 - best-effort seeding for a benchmark
            continue
        if answered % 1_000 == 0:
            col.save()
    print(f"  seeded review history on {answered} cards")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a Speedrun benchmark deck")
    ap.add_argument("--out", required=True, help="path to write the .anki2 collection")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument(
        "--activated-fraction", type=float, default=DEFAULT_ACTIVATED_FRACTION
    )
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()
    generate(args.out, args.count, args.activated_fraction, args.seed)


if __name__ == "__main__":
    main()
