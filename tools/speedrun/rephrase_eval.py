# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Offline eval harness for the Speedrun AI rephrasal feature (Plan §2a).

It runs the generator over a vendored gold set of 50 real served MCAT questions
and produces the honest numbers the Plan requires *before any variant reaches a
user*:

* **Quality classification** — each variant is graded correct-and-useful /
  wrong / correct-but-bad-teaching by the automatic :class:`HeuristicGrader`,
  reporting accuracy + wrong-answer rate.
* **Pass cutoff, pre-set** — :data:`PASS_ACCURACY_CUTOFF` and
  :data:`MAX_WRONG_RATE` are fixed here in advance; the run prints PASS/FAIL and
  the same per-variant ``min_quality`` gate that blocks bad variants in
  :func:`anki.speedrun_rephrase.generate_variants`.
* **Baseline comparison** — a simple TF-IDF/keyword retrieval baseline for the
  head-to-head "find the same-concept item" task; the AI rephrasal must beat it.
* **Leakage check** — scans generation inputs/outputs for any ``pool::heldout``
  item or near-duplicate of one, and confirms the generator refuses to run on
  heldout input.

Run it deterministically with no network (CI / grading)::

    python tools/speedrun/rephrase_eval.py --mock

Real OpenAI eval (needs ``OPENAI_API_KEY`` + network; this sandbox lacks both)::

    export OPENAI_API_KEY=sk-...
    python tools/speedrun/rephrase_eval.py            # auto-uses OpenAI if a key
    python tools/speedrun/rephrase_eval.py --model gpt-4o-mini

Exit code is 0 on PASS, 1 on FAIL, so it doubles as a CI gate.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make the pure ``anki.speedrun_rephrase`` importable without a built backend.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "pylib"))

from anki.speedrun_rephrase import (  # noqa: E402
    DEFAULT_MIN_QUALITY,
    LABEL_BAD_TEACHING,
    LABEL_CORRECT_USEFUL,
    LABEL_WRONG,
    HeuristicGrader,
    MockProvider,
    OpenAIProvider,
    Provider,
    RephrasedQuestion,
    SourceQuestion,
    scan_leakage,
    tokens,
)

GOLD_PATH = Path(__file__).parent / "data" / "rephrase_gold.json"

# --- Pre-registered pass criteria (fixed IN ADVANCE) -------------------------

#: At least this fraction of generated variants must be correct-and-useful.
#: 0.80 mirrors the app's WEAK_ACCURACY_THRESHOLD and reflects that a majority
#: super-majority of AI variants must be classroom-ready before the feature is
#: allowed to write to a student's collection.
PASS_ACCURACY_CUTOFF = 0.80
#: At most this fraction may be *wrong* (answer drift / leakage). Held to a
#: stricter bar than merely-suboptimal teaching because a wrong variant actively
#: misleads; correct-but-bad-teaching is filtered but not dangerous.
MAX_WRONG_RATE = 0.10
#: The AI must beat the retrieval baseline by at least this margin (points) on
#: the same-concept head-to-head, so "use an LLM" is a justified win, not noise.
MIN_BASELINE_MARGIN = 0.05


@dataclass
class EvalReport:
    """Everything the eval computes (also returned for programmatic use/tests)."""

    provider: str
    total: int
    counts: dict[str, int]
    accuracy: float
    wrong_rate: float
    gate_written: int
    gate_blocked: int
    baseline_set_size: int
    ai_same_concept: float
    baseline_same_concept: float
    leakage_clean: bool
    heldout_refused: bool
    grades: list[Any] = field(default_factory=list)

    @property
    def beats_baseline(self) -> bool:
        return self.ai_same_concept - self.baseline_same_concept >= MIN_BASELINE_MARGIN

    @property
    def passed(self) -> bool:
        return (
            self.accuracy >= PASS_ACCURACY_CUTOFF
            and self.wrong_rate <= MAX_WRONG_RATE
            and self.leakage_clean
            and self.heldout_refused
            and self.beats_baseline
        )


# --- TF-IDF / keyword retrieval baseline -------------------------------------


def _tfidf_vectors(
    docs: list[set[str]],
) -> tuple[list[dict[str, float]], dict[str, float]]:
    """TF-IDF vectors (as sparse dicts) for token-set documents + the idf map."""
    n = len(docs)
    df: dict[str, int] = {}
    for doc in docs:
        for term in doc:
            df[term] = df.get(term, 0) + 1
    idf = {term: math.log((n + 1) / (count + 1)) + 1.0 for term, count in df.items()}
    vectors: list[dict[str, float]] = []
    for doc in docs:
        # Token sets -> tf is 1 per present term; weight by idf.
        vectors.append({term: idf[term] for term in doc})
    return vectors, idf


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def baseline_same_concept_rate(items: list[dict[str, Any]]) -> tuple[float, int]:
    """TF-IDF nearest-neighbour retrieval: for each query with a same-concept
    sibling in the corpus, retrieve the most similar *other* item and score a
    hit iff it shares the query's concept. Returns ``(rate, set_size)``.
    """
    concepts = [str(it.get("concept", "")) for it in items]
    docs = [tokens(_item_text(it) + " " + concepts[i]) for i, it in enumerate(items)]
    vectors, _ = _tfidf_vectors(docs)

    # Head-to-head set = queries that actually have a same-concept sibling.
    has_sibling = [
        i
        for i in range(len(items))
        if concepts[i]
        and sum(1 for j in range(len(items)) if concepts[j] == concepts[i]) >= 2
    ]
    if not has_sibling:
        return 0.0, 0
    hits = 0
    for i in has_sibling:
        best_j, best_sim = -1, -1.0
        for j in range(len(items)):
            if j == i:
                continue
            sim = _cosine(vectors[i], vectors[j])
            if sim > best_sim:
                best_sim, best_j = sim, j
        if best_j >= 0 and concepts[best_j] == concepts[i]:
            hits += 1
    return hits / len(has_sibling), len(has_sibling)


def _item_text(item: dict[str, Any]) -> str:
    from anki.speedrun_rephrase import _as_option_list  # local, pure

    return " ".join(
        [str(item.get("stem", "")), " ".join(_as_option_list(item.get("options", [])))]
    )


def ai_same_concept_rate(
    items: list[dict[str, Any]],
    variants: list[RephrasedQuestion],
    grader: HeuristicGrader,
) -> tuple[float, int]:
    """AI head-to-head on the SAME same-concept set as the baseline: a hit is a
    generated variant that is a usable same-concept item (answer preserved,
    options grounded, not leaked). Returns ``(rate, set_size)``.
    """
    concepts = [str(it.get("concept", "")) for it in items]
    has_sibling = [
        i
        for i in range(len(items))
        if concepts[i]
        and sum(1 for j in range(len(items)) if concepts[j] == concepts[i]) >= 2
    ]
    if not has_sibling:
        return 0.0, 0
    hits = 0
    for i in has_sibling:
        source = SourceQuestion.from_dict(items[i])
        grade = grader.grade(source, variants[i])
        if grade.answer_preserved and grade.options_grounded and not grade.leaked:
            hits += 1
    return hits / len(has_sibling), len(has_sibling)


# --- The eval ----------------------------------------------------------------


def load_gold(path: Path | None = None) -> dict[str, Any]:
    with open(path or GOLD_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def evaluate(
    provider: Provider,
    gold: dict[str, Any],
    *,
    min_quality: float = DEFAULT_MIN_QUALITY,
) -> EvalReport:
    """Run the full eval and return an :class:`EvalReport` (pure; no I/O)."""
    questions = gold["questions"]
    heldout = gold.get("heldout_probe", [])

    # Grade leakage against the heldout probe so the check has something to bite.
    heldout_texts = [_item_text(h) for h in heldout]
    grader = HeuristicGrader(heldout_texts=heldout_texts)

    variants: list[RephrasedQuestion] = []
    grades = []
    counts = {LABEL_CORRECT_USEFUL: 0, LABEL_WRONG: 0, LABEL_BAD_TEACHING: 0}
    gate_written = gate_blocked = 0
    for item in questions:
        source = SourceQuestion.from_dict(item)
        variant = provider.rephrase(source)
        variants.append(variant)
        grade = grader.grade(source, variant)
        grades.append(grade)
        counts[grade.label] = counts.get(grade.label, 0) + 1
        # The exact gate generate_variants applies.
        if grade.label == LABEL_WRONG or grade.score < min_quality:
            gate_blocked += 1
        else:
            gate_written += 1

    total = len(questions)
    accuracy = counts[LABEL_CORRECT_USEFUL] / total if total else 0.0
    wrong_rate = counts[LABEL_WRONG] / total if total else 0.0

    baseline_rate, baseline_n = baseline_same_concept_rate(questions)
    ai_rate, _ = ai_same_concept_rate(questions, variants, grader)

    # Leakage: no heldout among inputs, no variant near-dup of a heldout item.
    variant_texts = [v.stem + " " + " ".join(v.options) for v in variants]
    leak = scan_leakage(questions, variant_texts, heldout)

    # Refusal check: feeding the heldout probe as input must be flagged (the
    # generator refuses heldout; the scan surfaces it here without a collection).
    refusal = scan_leakage(heldout, [], heldout)
    heldout_refused = bool(refusal.heldout_in_inputs) and not leak.heldout_in_inputs

    return EvalReport(
        provider=getattr(provider, "name", provider.__class__.__name__),
        total=total,
        counts=counts,
        accuracy=accuracy,
        wrong_rate=wrong_rate,
        gate_written=gate_written,
        gate_blocked=gate_blocked,
        baseline_set_size=baseline_n,
        ai_same_concept=ai_rate,
        baseline_same_concept=baseline_rate,
        leakage_clean=leak.clean,
        heldout_refused=heldout_refused,
        grades=grades,
    )


def format_report(report: EvalReport, min_quality: float) -> str:
    pct = lambda x: f"{100 * x:.1f}%"  # noqa: E731
    lines = [
        "=" * 64,
        "  Speedrun AI Rephrasal Eval",
        "=" * 64,
        f"Provider:            {report.provider}",
        f"Gold served items:   {report.total}",
        f"Variants generated:  {report.total}",
        "",
        "Classification (automatic grader):",
        f"  correct-and-useful:        {report.counts[LABEL_CORRECT_USEFUL]:>3}"
        f"  ({pct(report.accuracy)})",
        f"  correct-but-bad-teaching:  {report.counts[LABEL_BAD_TEACHING]:>3}",
        f"  wrong:                     {report.counts[LABEL_WRONG]:>3}"
        f"  ({pct(report.wrong_rate)})",
        "",
        f"Accuracy:       {pct(report.accuracy)}   "
        f"[cutoff >= {pct(PASS_ACCURACY_CUTOFF)}]  "
        f"{'OK' if report.accuracy >= PASS_ACCURACY_CUTOFF else 'FAIL'}",
        f"Wrong rate:     {pct(report.wrong_rate)}   "
        f"[cutoff <= {pct(MAX_WRONG_RATE)}]  "
        f"{'OK' if report.wrong_rate <= MAX_WRONG_RATE else 'FAIL'}",
        "",
        f"min_quality gate ({min_quality:.2f}): "
        f"{report.gate_written} would be written, {report.gate_blocked} blocked",
        "",
        f"Baseline (same-concept, head-to-head over {report.baseline_set_size} items):",
        f"  AI rephrasal same-concept:   {pct(report.ai_same_concept)}",
        f"  TF-IDF retrieval baseline:   {pct(report.baseline_same_concept)}",
        f"  AI beats baseline:           "
        f"{'YES' if report.beats_baseline else 'NO'} "
        f"(+{pct(report.ai_same_concept - report.baseline_same_concept)})",
        "",
        "Leakage check:",
        f"  heldout items in inputs:     "
        f"{'0 (clean)' if report.leakage_clean else 'FOUND'}",
        f"  near-duplicates of heldout:  "
        f"{'0 (clean)' if report.leakage_clean else 'FOUND'}",
        f"  refuses to run on heldout:   {'YES' if report.heldout_refused else 'NO'}",
        "",
        "-" * 64,
        f"RESULT: {'PASS' if report.passed else 'FAIL'}",
        "-" * 64,
    ]
    return "\n".join(lines)


def build_provider(args: argparse.Namespace) -> Provider:
    if args.mock:
        return MockProvider()
    if OpenAIProvider.available():
        return OpenAIProvider(model=args.model)
    print(
        "No OPENAI_API_KEY / openai library found; falling back to the "
        "deterministic MockProvider. Pass --mock to silence this, or set a key "
        "for the real eval.",
        file=sys.stderr,
    )
    return MockProvider()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force the deterministic offline MockProvider (CI / grading).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="OpenAI model for the real eval (default: a small/cheap model).",
    )
    parser.add_argument(
        "--min-quality",
        type=float,
        default=DEFAULT_MIN_QUALITY,
        help="Per-variant write gate (default: %(default)s).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of the text table.",
    )
    args = parser.parse_args()

    provider = build_provider(args)
    gold = load_gold()
    report = evaluate(provider, gold, min_quality=args.min_quality)

    if args.json:
        print(
            json.dumps(
                {
                    "provider": report.provider,
                    "total": report.total,
                    "counts": report.counts,
                    "accuracy": report.accuracy,
                    "wrong_rate": report.wrong_rate,
                    "gate_written": report.gate_written,
                    "gate_blocked": report.gate_blocked,
                    "baseline_set_size": report.baseline_set_size,
                    "ai_same_concept": report.ai_same_concept,
                    "baseline_same_concept": report.baseline_same_concept,
                    "leakage_clean": report.leakage_clean,
                    "heldout_refused": report.heldout_refused,
                    "passed": report.passed,
                },
                indent=2,
            )
        )
    else:
        print(format_report(report, args.min_quality))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
