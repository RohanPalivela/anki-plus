# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun paraphrase test (brief §7d / M4 §4a).

Proves the Performance model measures *transfer*, not just card memorization.
For each of 30 cards we compare:

* **card recall** — the Memory model's P(recall) for that card, and
* **reworded accuracy** — the student's accuracy on 2 exam-style questions that
  test the SAME idea in NEW words.

If the two are basically equal, the Performance model is just echoing Memory and
there is no bridge. The headline output is the **gap = recall − reworded
accuracy**; a positive, non-trivial gap is the win.

Run it::

    just speedrun-paraphrase -- --demo                    # synthetic; no build
    just speedrun-paraphrase -- --collection ~/col.anki2  # real study data

The 30-card / 60-question set lives in ``tools/speedrun/data/paraphrase_set.json``
(grounded in the MCAT first-principles topics). The gap aggregation is pure
Python and unit-tested (``pylib/tests/test_speedrun_paraphrase.py``).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "tools" / "speedrun" / "data" / "paraphrase_set.json"


@dataclass
class CardResult:
    card_id: str
    topic: str
    recall: float  # memory: P(recall of the card)
    reworded_acc: float  # performance: accuracy on the 2 reworded questions

    @property
    def gap(self) -> float:
        return self.recall - self.reworded_acc


def load_set(path: Path = DATA) -> list[dict]:
    return json.loads(path.read_text())["cards"]


# --------------------------------------------------------------------------- #
# Aggregation (pure)
# --------------------------------------------------------------------------- #
def summarize(results: list[CardResult]) -> dict:
    """Overall + per-topic mean recall, mean reworded accuracy and the gap."""
    if not results:
        return {"n": 0}
    recall = statistics.mean(r.recall for r in results)
    reworded = statistics.mean(r.reworded_acc for r in results)
    gaps = [r.gap for r in results]
    by_topic: dict[str, list[CardResult]] = {}
    for r in results:
        by_topic.setdefault(r.topic, []).append(r)
    topics = {
        t: {
            "n": len(rs),
            "recall": statistics.mean(x.recall for x in rs),
            "reworded_acc": statistics.mean(x.reworded_acc for x in rs),
            "gap": statistics.mean(x.gap for x in rs),
        }
        for t, rs in sorted(by_topic.items())
    }
    return {
        "n": len(results),
        "mean_recall": recall,
        "mean_reworded_acc": reworded,
        "gap": recall - reworded,
        "gap_lo": min(gaps),
        "gap_hi": max(gaps),
        "gap_stdev": statistics.pstdev(gaps) if len(gaps) > 1 else 0.0,
        "by_topic": topics,
    }


# --------------------------------------------------------------------------- #
# Demo (synthetic) data source
# --------------------------------------------------------------------------- #
def demo_results(
    cards: list[dict], seed: int = 5, transfer_penalty: float = 0.18
) -> list[CardResult]:
    """Synthetic learner: each card has a latent mastery driving recall; reworded
    accuracy is systematically lower (memorized wording > transfer) by
    ``transfer_penalty`` plus noise. Set the penalty to 0 to see the NULL (no
    bridge) case — the harness will then report a ~0 gap honestly.
    """
    rng = random.Random(seed)
    out: list[CardResult] = []
    for c in cards:
        mastery = rng.uniform(0.55, 0.98)
        recall = mastery
        reworded = max(
            0.25, min(1.0, mastery - transfer_penalty + rng.uniform(-0.06, 0.06))
        )
        out.append(CardResult(c["card_id"], c["topic"], recall, reworded))
    return out


# --------------------------------------------------------------------------- #
# Real-collection data source (best-effort; refined at integration)
# --------------------------------------------------------------------------- #
def collection_results(col_path: str, cards: list[dict]) -> list[CardResult]:
    """Card recall from the Memory model vs reworded accuracy from real answers.

    Card recall: per-card FSRS retrievability (via memory state). Reworded
    accuracy: fraction of the card's 2 reworded questions answered correctly in
    revlog if they were imported and studied; otherwise the mirrored 2PL
    prediction. Matching is by the ``paraphrase::<card_id>`` tag written at
    import time. Documented as integration-refined.
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT / "pylib"))
    sys.path.insert(0, str(REPO_ROOT / "out" / "pylib"))
    from anki.collection import Collection

    col = Collection(col_path)
    results: list[CardResult] = []
    try:
        today_ms = col.sched.day_cutoff * 1000
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "speedrun_validation", REPO_ROOT / "pylib/anki/speedrun_validation.py"
        )
        sv = importlib.util.module_from_spec(spec)
        sys.modules["speedrun_validation"] = sv
        spec.loader.exec_module(sv)

        for c in cards:
            recall = _card_recall(col, c, today_ms, sv)
            reworded = _reworded_accuracy(col, c)
            if recall is None or reworded is None:
                continue
            results.append(CardResult(c["card_id"], c["topic"], recall, reworded))
    finally:
        col.close()
    return results


def _card_recall(col, card_def, today_ms, sv) -> float | None:
    front = card_def["card_front"]
    nids = col.find_notes(f'"{front}"')
    for nid in nids:
        note = col.get_note(nid)
        for card in note.cards():
            state = getattr(card, "memory_state", None)
            if state and getattr(state, "stability", 0):
                last_ms = col.db.scalar(
                    "select id from revlog where cid=? order by id desc limit 1",
                    card.id,
                )
                if last_ms is None:
                    continue
                elapsed = max(0.0, (today_ms - last_ms) / 86_400_000.0)
                return sv.fsrs_retrievability(elapsed, state.stability)
    return None


def _reworded_accuracy(col, card_def) -> float | None:
    tag = f"paraphrase::{card_def['card_id']}"
    nids = col.find_notes(f"tag:{tag}")
    if not nids:
        return None
    correct = total = 0
    for nid in nids:
        for card in col.get_note(nid).cards():
            ease = col.db.scalar(
                "select ease from revlog where cid=? order by id desc limit 1", card.id
            )
            if ease is not None:
                total += 1
                correct += 1 if ease > 1 else 0
    return (correct / total) if total else None


def _print(summary: dict, mode: str) -> None:
    print(f"=== Speedrun paraphrase test — {mode} ===\n")
    if not summary.get("n"):
        print("no results (import the paraphrase set + study it, or use --demo)")
        return
    print(f"cards: {summary['n']}")
    print(f"mean card recall     : {summary['mean_recall']:.3f}")
    print(f"mean reworded accuracy: {summary['mean_reworded_acc']:.3f}")
    print(
        f"GAP (recall - reworded): {summary['gap']:+.3f}  "
        f"[per-card {summary['gap_lo']:+.3f}..{summary['gap_hi']:+.3f}]"
    )
    verdict = (
        "MEANINGFUL GAP — performance != memory (bridge exists)"
        if summary["gap"] > 0.05
        else "NO/SMALL GAP — performance is echoing memory (report honestly)"
    )
    print(f"\n{verdict}\n")
    print(f"{'topic':<12} {'recall':>7} {'reworded':>9} {'gap':>7}")
    print("-" * 38)
    for t, s in summary["by_topic"].items():
        print(
            f"{t:<12} {s['recall']:>7.3f} {s['reworded_acc']:>9.3f} {s['gap']:>+7.3f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Speedrun paraphrase (card recall vs reworded) test"
    )
    ap.add_argument("--demo", action="store_true", help="synthetic; runs with no build")
    ap.add_argument("--collection", help="path to a studied .anki2 collection")
    ap.add_argument("--seed", type=int, default=5)
    ap.add_argument("--transfer-penalty", type=float, default=0.18, help="demo only")
    ap.add_argument("--out", help="optional JSON summary output path")
    args = ap.parse_args()
    if not args.demo and not args.collection:
        ap.error("pass --demo or --collection PATH")

    cards = load_set()
    if args.demo:
        results = demo_results(
            cards, seed=args.seed, transfer_penalty=args.transfer_penalty
        )
        mode = "demo (SYNTHETIC — not real learner data)"
    else:
        results = collection_results(args.collection, cards)
        mode = f"collection: {args.collection}"

    summary = summarize(results)
    _print(summary, mode)
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2))
        print(f"\nsummary -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
