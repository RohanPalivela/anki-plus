# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun (MCAT fork) validation metrics (WS1 — held-out validation harness).

This module is the **pure, backend-free heart** of the validation harness. It
holds every calibration / classification metric and the two model formulas the
harness needs, kept free of any dependency on the compiled Rust backend (and of
numpy / scipy / sklearn / matplotlib) so it:

* imports and runs with **only the Python standard library** — the unit tests in
  ``pylib/tests/test_speedrun_validation.py`` exercise it without a built
  ``anki`` backend, and the ``--demo`` mode of ``tools/speedrun/validation.py``
  produces artifacts on any machine; and
* stays trivially reviewable and reusable by any surface.

What lives here:

* **Calibration metrics** for the Memory model — :func:`brier_score`,
  :func:`log_loss`, a binned :func:`reliability_table` (predicted probability vs
  empirical accuracy + counts) with :func:`expected_calibration_error`, wrapped
  by :func:`calibration_metrics`.
* **Recalibration** — :class:`PlattCalibrator` (logistic/Platt scaling) and
  :class:`IsotonicCalibrator` (pool-adjacent-violators isotonic regression),
  fit on a train split and applied to the held-out split so the harness can
  report calibration *before vs after*.
* **Classification metrics** for the Performance model — :func:`accuracy` and a
  stdlib :func:`roc_auc` (Mann-Whitney U with average-rank tie handling), so no
  sklearn is needed.
* **Model formulas mirrored from Rust** — :func:`fsrs_retrievability` (the FSRS
  power forgetting curve, matching ``rslib``'s ``current_retrievability`` used by
  ``mastery.rs``) and the 2PL-IRT :func:`predict_performance`
  (matching ``performance.rs``: guessing floor, weakest-link mastery blend,
  logit offset). Keeping the constants here in lockstep with the Rust source
  lets the harness reproduce the served model's predictions in Python.
* **Splitting** — :func:`split_pairs` (deterministic random *or* time-ordered
  train/test split with a fixed seed).

A "pair" throughout is ``(predicted_probability, observed_outcome)`` where the
prediction is in ``[0, 1]`` and the outcome is ``0`` (miss/incorrect) or ``1``
(recall/correct).

NOTE on optional plotting: :func:`save_reliability_diagram` renders a PNG with
matplotlib **iff it is importable**, and otherwise degrades to a no-op returning
``False`` — matplotlib is deliberately NOT a hard dependency of this module or
of Anki. The JSON/CSV outputs are always produced by the harness regardless.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Optional

# A single evaluated observation: (predicted probability in [0,1], outcome 0/1).
Pair = tuple[float, int]

# --- Constants mirrored from the Rust models (keep in lockstep) ---------------

#: FSRS-5 default decay (see ``fsrs`` crate ``FSRS5_DEFAULT_DECAY``); the power
#: forgetting curve exponent. Negative: retrievability falls as time passes.
FSRS5_DEFAULT_DECAY = -0.5

#: 4-option MCQ guessing floor (``performance.rs::GUESSING_FLOOR_C``).
GUESSING_FLOOR_C = 0.25
#: Weight given to the weakest topic's mastery (``WEAKEST_LINK_WEIGHT``).
WEAKEST_LINK_WEIGHT = 0.5
#: Mastery→logit scale (``MASTERY_LOGIT_SCALE``): mastery 1.0 → +2 logits.
MASTERY_LOGIT_SCALE = 2.0
#: Neutral mastery for a topic with no FSRS data (``NEUTRAL_MASTERY``).
NEUTRAL_MASTERY = 0.5

# Numerical guards.
_EPS = 1e-12
_LOGLOSS_EPS = 1e-15


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


# --- Model formulas (mirrored from rslib) ------------------------------------


def fsrs_retrievability(
    elapsed_days: float, stability: float, decay: float = FSRS5_DEFAULT_DECAY
) -> float:
    """FSRS power forgetting curve: P(recall) after ``elapsed_days`` at ``stability``.

    Mirrors the ``fsrs`` crate's ``current_retrievability`` used by the Memory
    model (``rslib/src/speedrun/mastery.rs``):

    ``R(t) = (1 + FACTOR · t / S) ^ decay``  with  ``FACTOR = 0.9^(1/decay) − 1``

    By construction ``R(S) = 0.9`` (stability is the interval for 90% retention).
    ``stability`` must be positive; a non-positive stability returns ``0.0``
    (an unlearned card). The result is clamped to ``[0, 1]``.
    """
    if stability <= 0.0 or elapsed_days < 0.0:
        return 0.0
    factor = 0.9 ** (1.0 / decay) - 1.0
    r = (1.0 + factor * elapsed_days / stability) ** decay
    return _clamp(r, 0.0, 1.0)


def sigmoid(z: float) -> float:
    """Numerically stable logistic σ(z) = 1 / (1 + e^-z)."""
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def logit(p: float) -> float:
    """Inverse logistic; input clamped away from 0/1 to stay finite."""
    p = _clamp(p, _EPS, 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def p_correct_2pl(theta: float, a: float, b: float) -> float:
    """2PL probability of a correct answer with the guessing floor ``c``.

    Mirrors ``performance.rs::p_correct_2pl``.
    """
    return GUESSING_FLOOR_C + (1.0 - GUESSING_FLOOR_C) * sigmoid(a * (theta - b))


def mastery_signal(masteries: Sequence[float]) -> float:
    """Blend topic masteries: half from the weakest topic, half from the mean.

    Mirrors ``performance.rs::mastery_signal``.
    """
    if not masteries:
        return NEUTRAL_MASTERY
    mean = sum(masteries) / len(masteries)
    minimum = min(masteries)
    return WEAKEST_LINK_WEIGHT * minimum + (1.0 - WEAKEST_LINK_WEIGHT) * mean


def mastery_logit(signal: float) -> float:
    """Mastery signal (0..1) → additive ability logit offset (``mastery_logit``)."""
    return MASTERY_LOGIT_SCALE * (2.0 * signal - 1.0)


def predict_performance(
    theta: float, a: float, b: float, masteries: Sequence[float]
) -> float:
    """P(correct) blending fitted ability with the question's topic masteries.

    Mirrors ``performance.rs::predict_performance`` — this is the exact formula
    the served Performance RPC uses, reproduced so the harness can score its
    predictions on held-out questions in Python.
    """
    effective_theta = theta + mastery_logit(mastery_signal(masteries))
    return p_correct_2pl(effective_theta, a, b)


# --- Calibration metrics -----------------------------------------------------


def _as_pairs(pairs: Sequence[Pair]) -> list[Pair]:
    out: list[Pair] = []
    for pred, obs in pairs:
        out.append((float(pred), 1 if obs else 0))
    return out


def brier_score(pairs: Sequence[Pair]) -> float:
    """Mean squared error between predicted probability and outcome (lower=better).

    ``BS = (1/N) Σ (pᵢ − oᵢ)²`` in ``[0, 1]``; 0 is perfect, 0.25 is the
    always-predict-0.5 baseline. Returns ``0.0`` for an empty set.
    """
    data = _as_pairs(pairs)
    if not data:
        return 0.0
    return sum((p - o) ** 2 for p, o in data) / len(data)


def log_loss(pairs: Sequence[Pair], eps: float = _LOGLOSS_EPS) -> float:
    """Mean negative log-likelihood (cross-entropy) of the outcomes (lower=better).

    ``LL = −(1/N) Σ [oᵢ·ln pᵢ + (1−oᵢ)·ln(1−pᵢ)]``. Predictions are clamped to
    ``[eps, 1−eps]`` so a confident wrong prediction is heavily — but finitely —
    penalised. Returns ``0.0`` for an empty set.
    """
    data = _as_pairs(pairs)
    if not data:
        return 0.0
    total = 0.0
    for p, o in data:
        p = _clamp(p, eps, 1.0 - eps)
        total += math.log(p) if o == 1 else math.log(1.0 - p)
    return -total / len(data)


@dataclass
class ReliabilityBin:
    """One bin of a reliability table.

    ``lower``/``upper`` are the predicted-probability bin edges; ``count`` is how
    many predictions fell in it; ``mean_predicted`` is their average predicted
    probability; ``empirical`` is the observed fraction correct/recalled (the
    calibration target — a perfectly calibrated bin has ``empirical ==
    mean_predicted``).
    """

    lower: float
    upper: float
    count: int
    mean_predicted: float
    empirical: float

    @property
    def gap(self) -> float:
        """Signed calibration gap (empirical − predicted) for this bin."""
        return self.empirical - self.mean_predicted

    def to_dict(self) -> dict:
        d = asdict(self)
        d["gap"] = self.gap
        return d


def reliability_table(pairs: Sequence[Pair], n_bins: int = 10) -> list[ReliabilityBin]:
    """Bin predictions into ``n_bins`` equal-width buckets over ``[0, 1]``.

    Empty bins are omitted. The top bin is right-closed so a prediction of
    exactly ``1.0`` is counted. This is the data behind the reliability diagram.
    """
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    data = _as_pairs(pairs)
    edges = [i / n_bins for i in range(n_bins + 1)]
    sums_pred = [0.0] * n_bins
    sums_obs = [0.0] * n_bins
    counts = [0] * n_bins
    for p, o in data:
        p = _clamp(p, 0.0, 1.0)
        idx = min(int(p * n_bins), n_bins - 1)
        sums_pred[idx] += p
        sums_obs[idx] += o
        counts[idx] += 1
    bins: list[ReliabilityBin] = []
    for i in range(n_bins):
        if counts[i] == 0:
            continue
        bins.append(
            ReliabilityBin(
                lower=edges[i],
                upper=edges[i + 1],
                count=counts[i],
                mean_predicted=sums_pred[i] / counts[i],
                empirical=sums_obs[i] / counts[i],
            )
        )
    return bins


def expected_calibration_error(bins: Sequence[ReliabilityBin]) -> float:
    """Count-weighted mean absolute gap between predicted and empirical (ECE).

    ``ECE = Σ (nᵢ/N) · |empiricalᵢ − predictedᵢ|`` over non-empty bins; 0 is
    perfectly calibrated. Returns ``0.0`` when there is no data.
    """
    total = sum(b.count for b in bins)
    if total == 0:
        return 0.0
    return sum(b.count * abs(b.gap) for b in bins) / total


@dataclass
class CalibrationMetrics:
    """The full calibration read-out for one set of predictions."""

    n: int
    brier: float
    log_loss: float
    ece: float
    reliability: list[ReliabilityBin] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "ece": self.ece,
            "reliability": [b.to_dict() for b in self.reliability],
        }


def calibration_metrics(
    pairs: Sequence[Pair], n_bins: int = 10
) -> CalibrationMetrics:
    """Brier score, log loss, ECE and the reliability table for ``pairs``."""
    data = _as_pairs(pairs)
    bins = reliability_table(data, n_bins)
    return CalibrationMetrics(
        n=len(data),
        brier=brier_score(data),
        log_loss=log_loss(data),
        ece=expected_calibration_error(bins),
        reliability=bins,
    )


# --- Classification metrics --------------------------------------------------


def accuracy(pairs: Sequence[Pair], threshold: float = 0.5) -> float:
    """Fraction of predictions on the correct side of ``threshold``.

    A prediction ``p >= threshold`` is a predicted positive. Returns ``0.0`` for
    an empty set.
    """
    data = _as_pairs(pairs)
    if not data:
        return 0.0
    hits = sum(1 for p, o in data if (p >= threshold) == (o == 1))
    return hits / len(data)


def roc_auc(labels: Sequence[int], scores: Sequence[float]) -> float:
    """Area under the ROC curve via the Mann-Whitney U statistic (stdlib only).

    Uses average ranks so ties between a positive and a negative contribute
    exactly 0.5, matching the standard AUC definition. Returns ``0.5`` (chance)
    when either class is empty, since AUC is undefined there — callers should
    check ``n`` and the class balance separately.
    """
    if len(labels) != len(scores):
        raise ValueError("labels and scores must be the same length")
    labs = [1 if x else 0 for x in labels]
    n = len(labs)
    n_pos = sum(labs)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5

    # Average ranks (1-based) over scores sorted ascending.
    order = sorted(range(n), key=lambda i: scores[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1

    sum_ranks_pos = sum(ranks[i] for i in range(n) if labs[i] == 1)
    return (sum_ranks_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


# --- Recalibration -----------------------------------------------------------


class PlattCalibrator:
    """Platt (logistic) scaling: fit ``p' = σ(A·z + B)`` where ``z = logit(p)``.

    A parametric recalibration that fixes a systematic over/under-confidence with
    two parameters. Fit by minimising cross-entropy against Platt's smoothed
    targets (so it does not over-fit small samples), by gradient descent on
    standardised ``z`` for good conditioning — no numpy/scipy needed.
    """

    def __init__(self) -> None:
        self.a = 1.0
        self.b = 0.0
        self._mean = 0.0
        self._std = 1.0
        self.fitted = False

    def fit(
        self, pairs: Sequence[Pair], *, iterations: int = 4000, lr: float = 0.3
    ) -> "PlattCalibrator":
        data = _as_pairs(pairs)
        if not data:
            return self
        zs = [logit(p) for p, _ in data]
        ys = [o for _, o in data]
        n = len(data)
        n_pos = sum(ys)
        n_neg = n - n_pos
        # Platt's smoothed targets guard against over-fitting on small samples.
        t_pos = (n_pos + 1.0) / (n_pos + 2.0)
        t_neg = 1.0 / (n_neg + 2.0)
        targets = [t_pos if y == 1 else t_neg for y in ys]

        # Standardise z so a single learning rate conditions well.
        self._mean = sum(zs) / n
        var = sum((z - self._mean) ** 2 for z in zs) / n
        self._std = math.sqrt(var) if var > _EPS else 1.0
        zst = [(z - self._mean) / self._std for z in zs]

        a, b = 0.0, 0.0
        for _ in range(iterations):
            ga = 0.0
            gb = 0.0
            for z, t in zip(zst, targets):
                pred = sigmoid(a * z + b)
                err = pred - t
                ga += err * z
                gb += err
            a -= lr * ga / n
            b -= lr * gb / n
        self.a, self.b = a, b
        self.fitted = True
        return self

    def predict(self, p: float) -> float:
        if not self.fitted:
            return _clamp(p, 0.0, 1.0)
        z = (logit(p) - self._mean) / self._std
        return sigmoid(self.a * z + self.b)

    def transform(self, pairs: Sequence[Pair]) -> list[Pair]:
        return [(self.predict(p), o) for p, o in _as_pairs(pairs)]

    def to_dict(self) -> dict:
        return {
            "method": "platt",
            "a": self.a,
            "b": self.b,
            "z_mean": self._mean,
            "z_std": self._std,
        }


class IsotonicCalibrator:
    """Isotonic regression via pool-adjacent-violators (PAV), then interpolation.

    A non-parametric recalibration that learns any monotonic (non-decreasing)
    mapping from predicted to empirical probability — more flexible than Platt
    when miscalibration is not a simple logistic shift. Prediction linearly
    interpolates between fitted points and clamps outside the training range.
    """

    def __init__(self) -> None:
        self._x: list[float] = []
        self._y: list[float] = []
        self.fitted = False

    @staticmethod
    def _pav(ys: Sequence[float]) -> list[float]:
        """Pool-adjacent-violators; returns a non-decreasing fit aligned to ys."""
        values: list[float] = []
        weights: list[float] = []
        counts: list[int] = []
        for y in ys:
            values.append(float(y))
            weights.append(1.0)
            counts.append(1)
            while len(values) > 1 and values[-2] > values[-1]:
                w = weights[-2] + weights[-1]
                v = (values[-2] * weights[-2] + values[-1] * weights[-1]) / w
                c = counts[-2] + counts[-1]
                values[-2:] = [v]
                weights[-2:] = [w]
                counts[-2:] = [c]
        fitted: list[float] = []
        for v, c in zip(values, counts):
            fitted.extend([v] * c)
        return fitted

    def fit(self, pairs: Sequence[Pair]) -> "IsotonicCalibrator":
        data = sorted(_as_pairs(pairs), key=lambda pr: pr[0])
        if not data:
            return self
        xs = [p for p, _ in data]
        ys = [o for _, o in data]
        fitted = self._pav(ys)
        # Collapse duplicate x, keeping the (already pooled) fitted value.
        cx: list[float] = []
        cy: list[float] = []
        for x, v in zip(xs, fitted):
            if cx and abs(x - cx[-1]) <= _EPS:
                cy[-1] = v
            else:
                cx.append(x)
                cy.append(v)
        self._x, self._y = cx, cy
        self.fitted = True
        return self

    def predict(self, p: float) -> float:
        if not self.fitted or not self._x:
            return _clamp(p, 0.0, 1.0)
        if p <= self._x[0]:
            return self._y[0]
        if p >= self._x[-1]:
            return self._y[-1]
        # Binary search for the bracketing interval, then linear-interpolate.
        lo, hi = 0, len(self._x) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self._x[mid] <= p:
                lo = mid
            else:
                hi = mid
        x0, x1 = self._x[lo], self._x[hi]
        y0, y1 = self._y[lo], self._y[hi]
        if x1 - x0 <= _EPS:
            return y1
        return y0 + (y1 - y0) * (p - x0) / (x1 - x0)

    def transform(self, pairs: Sequence[Pair]) -> list[Pair]:
        return [(self.predict(p), o) for p, o in _as_pairs(pairs)]

    def to_dict(self) -> dict:
        return {"method": "isotonic", "points": list(zip(self._x, self._y))}


# --- Train/test split --------------------------------------------------------


def split_pairs(
    pairs: Sequence[Pair],
    *,
    test_frac: float = 0.3,
    seed: int = 0,
    mode: str = "random",
    order: Optional[Sequence[float]] = None,
) -> tuple[list[Pair], list[Pair]]:
    """Deterministic train/test split of ``pairs``.

    ``mode="random"`` shuffles with a fixed ``seed`` (reproducible).
    ``mode="time"`` sorts by ``order`` (e.g. review timestamps; falls back to the
    input order) and puts the earliest ``1−test_frac`` in train and the latest
    ``test_frac`` in test — the honest "predict the future from the past" split.
    """
    data = _as_pairs(pairs)
    n = len(data)
    if n == 0:
        return [], []
    if not 0.0 < test_frac < 1.0:
        raise ValueError("test_frac must be in (0, 1)")
    n_test = max(1, int(round(n * test_frac)))
    n_test = min(n_test, n - 1) if n > 1 else 0

    if mode == "time":
        keys = list(order) if order is not None else list(range(n))
        if len(keys) != n:
            raise ValueError("order must match pairs length")
        idx = sorted(range(n), key=lambda i: keys[i])
        train_idx = idx[: n - n_test]
        test_idx = idx[n - n_test :]
    elif mode == "random":
        idx = list(range(n))
        random.Random(seed).shuffle(idx)
        test_idx = idx[:n_test]
        train_idx = idx[n_test:]
    else:
        raise ValueError(f"unknown split mode: {mode!r}")

    return [data[i] for i in train_idx], [data[i] for i in test_idx]


# --- Optional reliability-diagram plot (guarded) -----------------------------


def save_reliability_diagram(
    before: Sequence[ReliabilityBin],
    path: str,
    *,
    after: Optional[Sequence[ReliabilityBin]] = None,
    title: str = "Memory model reliability",
    after_label: str = "recalibrated",
) -> bool:
    """Render a reliability diagram PNG to ``path`` — iff matplotlib is available.

    Returns ``True`` when the file was written, ``False`` when matplotlib could
    not be imported (a deliberate soft dependency: the harness always emits
    JSON/CSV; the PNG is a bonus). Never raises on a missing import.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless: no display needed
        import matplotlib.pyplot as plt
    except Exception:
        return False

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], "--", color="gray", label="perfect calibration")

    def _series(bins: Sequence[ReliabilityBin]):
        xs = [b.mean_predicted for b in bins]
        ys = [b.empirical for b in bins]
        return xs, ys

    bx, by = _series(before)
    ax.plot(bx, by, "o-", label="as predicted")
    if after:
        ax_, ay = _series(after)
        ax.plot(ax_, ay, "s-", label=after_label)

    ax.set_xlabel("mean predicted probability")
    ax.set_ylabel("empirical accuracy")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title(title)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True
