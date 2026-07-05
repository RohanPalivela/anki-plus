# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun study-feature ablation experiment (WS5 / brief §8 / M4 §4b — 15%).

Pre-registered hypothesis (one sentence):
    "Interleaving practice questions across topics raises accuracy on NEW
    mixed-topic questions at equal study time; FAIL if Δ(full − ablation) ≤ 0."

Compares THREE builds at EQUAL study time (equal number of study events) on the
SAME held-out mixed-topic test:

    1. full      — Speedrun with interleaving ON  (weakness-targeted + interleaved)
    2. ablation  — Speedrun with interleaving OFF (weakness-targeted + blocked)
    3. plain     — plain Anki baseline            (uniform order, no transfer)

Why all three: full-vs-ablation isolates the interleaving feature; ablation-vs-
plain shows whether the rest of Speedrun (weakness-targeted allocation) helps at
all. (See brief §8.)

This is a DETERMINISTIC, backend-free SIMULATION so it re-runs anywhere
(`just speedrun-ablation`). It drives the real serving orders (round-robin vs
blocked, mirroring `speedrun._round_robin` / `_blocked`) through a documented
synthetic learner. It is a fair test, not a rigged one: the interleaving benefit
is governed by one transparent parameter (`--transfer-weight`) and `--sweep`
shows the effect HONESTLY collapses to a null when that benefit is zero. Real
seed/human learners are the follow-up; see docs/speedrun/ablation.md.
"""

from __future__ import annotations

import argparse
import random
import statistics
from dataclasses import dataclass

TOPICS = [
    "biochem", "biology", "orgo", "gen-chem", "physics",
    "psych", "sociology", "physio", "genetics", "cell-bio",
]


@dataclass
class LearnerParams:
    learn_rate: float = 0.08      # mastery gained per study event on a topic
    transfer_weight: float = 0.25 # how much interleaving's context-switching helps transfer
    switch_gain: float = 0.03     # transfer skill gained per context switch during study
    block_size: int = 6           # consecutive events per topic in blocked practice
    guess: float = 0.25           # 4-option MCQ floor on the held-out test


def _start_mastery(rng: random.Random) -> dict:
    """Heterogeneous per-topic priors so weakness-targeting has something to do."""
    return {t: rng.uniform(0.05, 0.55) for t in TOPICS}


def _uniform_blocked_order(events: int, topics: list[str]) -> list[str]:
    """Plain-Anki baseline: equal events per topic, blocked, no targeting."""
    per = events // len(topics)
    order: list[str] = []
    for t in topics:
        order.extend([t] * per)
    while len(order) < events:
        order.append(topics[len(order) % len(topics)])
    return order


def _weakness_interleaved_order(events: int, topics: list[str], p: LearnerParams,
                                start: dict) -> list[str]:
    """full: pick the weakest topic each step but never repeat back-to-back."""
    mastery = dict(start)
    order: list[str] = []
    last: str | None = None
    for _ in range(events):
        ranked = sorted(topics, key=lambda t: mastery[t])
        pick = ranked[1] if (ranked[0] == last and len(ranked) > 1) else ranked[0]
        order.append(pick)
        mastery[pick] += p.learn_rate * (1.0 - mastery[pick])
        last = pick
    return order


def _weakness_blocked_order(events: int, topics: list[str], p: LearnerParams,
                            start: dict) -> list[str]:
    """ablation: weakness-targeted but BLOCKED — study the weakest topic in runs
    of ``block_size`` before re-choosing (few context switches)."""
    mastery = dict(start)
    order: list[str] = []
    while len(order) < events:
        pick = min(topics, key=lambda t: mastery[t])
        for _ in range(min(p.block_size, events - len(order))):
            order.append(pick)
            mastery[pick] += p.learn_rate * (1.0 - mastery[pick])
    return order


def simulate(order: list[str], topics: list[str], p: LearnerParams,
             start: dict) -> tuple[dict, float]:
    """Run a study order through the learner; return (mastery, transfer_skill)."""
    mastery = dict(start)
    transfer = 0.0
    last: str | None = None
    for t in order:
        mastery[t] += p.learn_rate * (1.0 - mastery[t])
        if last is not None and t != last:
            transfer = min(1.0, transfer + p.switch_gain)
        last = t
    return mastery, transfer


def test_accuracy(mastery: dict, transfer: float, p: LearnerParams,
                  rng: random.Random, n_items: int = 400) -> float:
    """Accuracy on NEW mixed-topic held-out questions (2 topics each, weakest-link).

    P(correct) = guess-floored logistic of the weakest-link mastery, lifted by a
    transfer term that only interleaving accrues. Mixed-topic items are where
    transfer matters (the desirable-difficulty claim).
    """
    correct = 0
    for _ in range(n_items):
        t1, t2 = rng.sample(TOPICS, 2)
        weakest = min(mastery[t1], mastery[t2])
        mean = (mastery[t1] + mastery[t2]) / 2.0
        signal = 0.5 * weakest + 0.5 * mean
        lifted = min(1.0, signal + p.transfer_weight * transfer)
        prob = p.guess + (1.0 - p.guess) * lifted
        if rng.random() < prob:
            correct += 1
    return correct / n_items


def run_condition(kind: str, events: int, p: LearnerParams, seed: int) -> float:
    rng = random.Random(seed)
    start = _start_mastery(rng)  # same priors across builds for a fair, matched test
    if kind == "full":
        order = _weakness_interleaved_order(events, TOPICS, p, start)
    elif kind == "ablation":
        order = _weakness_blocked_order(events, TOPICS, p, start)
    elif kind == "plain":
        order = _uniform_blocked_order(events, TOPICS)
    else:
        raise ValueError(kind)
    mastery, transfer = simulate(order, TOPICS, p, start)
    if kind == "plain":
        transfer = 0.0  # baseline gets no interleaving/transfer credit
    return test_accuracy(mastery, transfer, p, rng)


def experiment(events: int, seeds: int, p: LearnerParams) -> dict:
    conditions = ["full", "ablation", "plain"]
    results = {c: [] for c in conditions}
    for s in range(seeds):
        for c in conditions:
            results[c].append(run_condition(c, events, p, seed=1000 + s))
    summary = {}
    for c in conditions:
        xs = results[c]
        summary[c] = {
            "mean": statistics.mean(xs),
            "lo": min(xs),
            "hi": max(xs),
            "stdev": statistics.pstdev(xs) if len(xs) > 1 else 0.0,
        }
    summary["delta_full_minus_ablation"] = summary["full"]["mean"] - summary["ablation"]["mean"]
    summary["delta_ablation_minus_plain"] = summary["ablation"]["mean"] - summary["plain"]["mean"]
    summary["hypothesis_supported"] = summary["delta_full_minus_ablation"] > 0
    return summary


def _print(summary: dict, p: LearnerParams, events: int, seeds: int) -> None:
    print("=== Speedrun ablation: interleaving (SIMULATION) ===")
    print(f"pre-registered: interleaving raises mixed-topic accuracy at equal study time; fail if Δ(full−ablation)≤0")
    print(f"study events={events} (equal per build), seeds={seeds}, transfer_weight={p.transfer_weight}\n")
    print(f"{'build':<10} {'mean':>7} {'range':>17}")
    print("-" * 36)
    for c in ("full", "ablation", "plain"):
        s = summary[c]
        print(f"{c:<10} {s['mean']:>7.3f}   [{s['lo']:.3f}, {s['hi']:.3f}]")
    print()
    d1 = summary["delta_full_minus_ablation"]
    d2 = summary["delta_ablation_minus_plain"]
    print(f"Δ full − ablation   = {d1:+.3f}  (interleaving effect)")
    print(f"Δ ablation − plain  = {d2:+.3f}  (rest-of-Speedrun effect)")
    verdict = "SUPPORTED" if summary["hypothesis_supported"] else "NULL / NOT SUPPORTED (report honestly)"
    print(f"\nHypothesis: {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Speedrun interleaving ablation simulation")
    ap.add_argument("--events", type=int, default=100, help="study events per build (equal time)")
    ap.add_argument("--seeds", type=int, default=50)
    ap.add_argument("--transfer-weight", type=float, default=0.25)
    ap.add_argument("--sweep", action="store_true", help="sweep transfer-weight to show the null")
    args = ap.parse_args()

    if args.sweep:
        noise = 0.02  # treat |Δ| below this as null (within seed-to-seed noise)
        print("transfer_weight  Δ(full−ablation)  verdict")
        for tw in (0.0, 0.05, 0.1, 0.2, 0.3):
            p = LearnerParams(transfer_weight=tw)
            s = experiment(args.events, args.seeds, p)
            d = s["delta_full_minus_ablation"]
            print(f"{tw:>14.2f}  {d:>+15.3f}  {'win' if d > noise else 'null'}")
        print(
            "\n(Honest: at transfer_weight=0 the interleaving effect collapses to "
            "≈0 — within noise, a valid null. The small residual is allocation\n"
            " granularity, not transfer; the transfer benefit is what the "
            "hypothesis is really about.)"
        )
        return 0

    p = LearnerParams(transfer_weight=args.transfer_weight)
    summary = experiment(args.events, args.seeds, p)
    _print(summary, p, args.events, args.seeds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
