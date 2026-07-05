# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pure-Python helpers for the Speedrun one-command benchmark (WS2 / §10).

Everything here is intentionally **backend-free**: no ``anki`` import, no
third-party dependency (the anki dev env does not ship numpy, so percentiles are
computed with the stdlib to honour "no new deps"). That keeps the percentile
maths and the PASS/FAIL table trivially unit-testable without a compiled Rust
backend — see ``tools/bench/test_bench_lib.py``, which runs under plain
``python`` on a fresh checkout.

The actual measuring lives in ``tools/bench/bench.py``; this module only turns a
list of per-iteration latency samples (in milliseconds) into the p50 / p95 /
worst table the Plan's §10 performance budgets are graded against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def percentile(samples: list[float], q: float) -> float:
    """Return the ``q``-th percentile (``q`` in ``[0, 100]``) of ``samples``.

    Uses linear interpolation between the two closest ranks — the same method as
    ``numpy.percentile(..., method="linear")`` and ``statistics.quantiles``'
    inclusive variant — so results match what a numpy-based harness would print,
    but with zero dependencies. Raises ``ValueError`` on empty input (an empty
    latency series is a bug in the caller, not a 0ms result).
    """
    if not samples:
        raise ValueError("percentile() requires at least one sample")
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"percentile q must be in [0, 100], got {q}")
    ordered = sorted(samples)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (q / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[low])
    frac = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * frac)


@dataclass(frozen=True)
class Budget:
    """A §10 performance budget for one measured action.

    ``p95_ms`` is the hard cap the Plan sets on the p95 latency. ``interactive``
    marks actions that are on the tap-to-feedback path, so their *worst* sample
    is additionally checked against the "no UI freeze > 100ms" rule.
    """

    key: str
    label: str
    p95_ms: float
    interactive: bool = False


#: The four benchmarked actions and their §10 budgets (Anki_Plan.md line 51):
#: button ack p95 < 50ms; next card p95 < 100ms; dashboard load p95 < 1s;
#: dashboard refresh p95 < 500ms; plus the cross-cutting "no UI freeze > 100ms".
FREEZE_MS = 100.0
BUDGETS: dict[str, Budget] = {
    "button_ack": Budget("button_ack", "Button press ack (answerCard)", 50.0, True),
    "next_card": Budget("next_card", "Next card appears (queue)", 100.0, True),
    "dashboard_load": Budget("dashboard_load", "Dashboard first load (cold)", 1000.0),
    "dashboard_refresh": Budget("dashboard_refresh", "Dashboard refresh (warm)", 500.0),
}


@dataclass
class Stat:
    """Summary statistics for one action's latency samples (all in ms)."""

    key: str
    label: str
    samples_ms: list[float] = field(default_factory=list)
    #: Extra human-readable context (e.g. "abstained" for a score RPC).
    note: str = ""

    @property
    def n(self) -> int:
        return len(self.samples_ms)

    @property
    def p50(self) -> float:
        return percentile(self.samples_ms, 50)

    @property
    def p95(self) -> float:
        return percentile(self.samples_ms, 95)

    @property
    def worst(self) -> float:
        return max(self.samples_ms)

    @property
    def mean(self) -> float:
        return sum(self.samples_ms) / len(self.samples_ms)


def summarize(key: str, label: str, samples_ms: list[float], note: str = "") -> Stat:
    return Stat(key=key, label=label, samples_ms=list(samples_ms), note=note)


@dataclass
class RowResult:
    """One rendered table row plus its PASS/FAIL verdicts."""

    stat: Stat
    budget: Budget | None
    p95_pass: bool
    freeze_pass: bool
    #: True when the action was not measured (no latency samples) — e.g. the deck
    #: had no due cards to answer. Such a row is neither PASS nor FAIL; it is
    #: reported as SKIP and excluded from the OVERALL verdict rather than
    #: crashing the whole benchmark on an empty series.
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.p95_pass and self.freeze_pass


def evaluate(stat: Stat, budget: Budget | None) -> RowResult:
    """Grade one action's stats against its budget (if any)."""
    if budget is None:
        return RowResult(stat=stat, budget=None, p95_pass=True, freeze_pass=True)
    if not stat.samples_ms:
        # No samples collected: report as skipped, not a 0ms pass or a crash.
        return RowResult(
            stat=stat, budget=budget, p95_pass=False, freeze_pass=True, skipped=True
        )
    p95_pass = stat.p95 <= budget.p95_ms
    # The freeze rule only applies to interactive (tap-to-feedback) actions.
    freeze_pass = (not budget.interactive) or stat.worst <= FREEZE_MS
    return RowResult(
        stat=stat, budget=budget, p95_pass=p95_pass, freeze_pass=freeze_pass
    )


def _fmt_ms(value: float) -> str:
    return f"{value:.1f}"


def format_table(stats: list[Stat], budgets: dict[str, Budget] = BUDGETS) -> str:
    """Render the p50 / p95 / worst table with a PASS/FAIL hint per row.

    Each row prints the measured p50 / p95 / worst next to the §10 p95 budget
    and a PASS/FAIL verdict (p95 within budget, and — for interactive actions —
    no worst-case sample above the 100ms freeze line).
    """
    rows = [evaluate(s, budgets.get(s.key)) for s in stats]

    headers = ["Action", "n", "p50 ms", "p95 ms", "worst ms", "§10 budget", "result"]
    table: list[list[str]] = []
    for row in rows:
        stat = row.stat
        budget = row.budget
        if budget is None:
            budget_cell = "—"
            result_cell = "n/a"
        elif row.skipped:
            budget_cell = f"p95<{_fmt_ms(budget.p95_ms)}"
            result_cell = "SKIP (no samples)"
        else:
            budget_cell = f"p95<{_fmt_ms(budget.p95_ms)}"
            result_cell = "PASS" if row.passed else "FAIL"
        label = stat.label + (f"  [{stat.note}]" if stat.note else "")
        # Empty series (skipped) have no percentiles to print.
        if stat.samples_ms:
            p50_cell, p95_cell, worst_cell = (
                _fmt_ms(stat.p50),
                _fmt_ms(stat.p95),
                _fmt_ms(stat.worst),
            )
        else:
            p50_cell = p95_cell = worst_cell = "—"
        table.append(
            [
                label,
                str(stat.n),
                p50_cell,
                p95_cell,
                worst_cell,
                budget_cell,
                result_cell,
            ]
        )

    widths = [len(h) for h in headers]
    for line in table:
        for i, cell in enumerate(line):
            widths[i] = max(widths[i], len(cell))

    def render(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    out = [render(headers), render(["-" * w for w in widths])]
    out.extend(render(line) for line in table)

    # Skipped (unmeasured) actions are excluded from OVERALL — it reflects only
    # the actions we actually measured.
    graded = [r for r in rows if r.budget is not None and not r.skipped]
    all_pass = all(r.passed for r in graded)
    out.append("")
    out.append(f"OVERALL: {'PASS' if all_pass else 'FAIL'}")
    skipped = [r.stat.label for r in rows if r.skipped]
    if skipped:
        out.append(f"SKIPPED (no samples): {', '.join(skipped)}")
    # Call out any freeze violations explicitly (worst > 100ms on an
    # interactive action) — these are the "no UI freeze > 100ms" rule.
    freezes = [
        r.stat.label
        for r in rows
        if r.budget is not None and r.budget.interactive and not r.freeze_pass
    ]
    if freezes:
        out.append(f"UI FREEZE (>{_fmt_ms(FREEZE_MS)}ms worst): {', '.join(freezes)}")
    return "\n".join(out)


def all_passed(stats: list[Stat], budgets: dict[str, Budget] = BUDGETS) -> bool:
    """True iff every action with a budget met its p95 and freeze verdicts."""
    rows = [evaluate(s, budgets.get(s.key)) for s in stats]
    return all(r.passed for r in rows if r.budget is not None and not r.skipped)
