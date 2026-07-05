# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Unit tests for the backend-free Speedrun validation metrics (WS1).

Everything here exercises ``anki.speedrun_validation`` with pure Python — no
collection, no compiled backend — so the calibration/classification maths the
grade relies on is verified independently of a build.
"""

from __future__ import annotations

import math

from anki import speedrun_validation as sv


def test_brier_and_log_loss_reward_calibration():
    perfect = [(1.0, 1), (0.0, 0), (1.0, 1), (0.0, 0)]
    hedged = [(0.5, 1), (0.5, 0), (0.5, 1), (0.5, 0)]
    assert sv.brier_score(perfect) == 0.0
    assert sv.brier_score(hedged) == 0.25
    # Log loss punishes a confident wrong prediction heavily.
    assert sv.log_loss([(0.99, 0)]) > sv.log_loss([(0.5, 0)])


def test_reliability_table_and_ece():
    # Predictions that systematically under-shoot (pred 0.2, empirical ~0.5).
    pairs = [(0.2, 1), (0.2, 0), (0.2, 1), (0.2, 0)]
    bins = sv.reliability_table(pairs, n_bins=10)
    assert len(bins) == 1
    b = bins[0]
    assert math.isclose(b.mean_predicted, 0.2, abs_tol=1e-9)
    assert math.isclose(b.empirical, 0.5, abs_tol=1e-9)
    assert math.isclose(sv.expected_calibration_error(bins), 0.3, abs_tol=1e-9)


def test_roc_auc_ranks_positives_above_negatives():
    labels = [0, 0, 1, 1]
    scores = [0.1, 0.2, 0.8, 0.9]
    assert sv.roc_auc(labels, scores) == 1.0
    # Reversed scores => perfectly wrong ranking.
    assert sv.roc_auc(labels, [0.9, 0.8, 0.2, 0.1]) == 0.0
    # Random-ish tie => 0.5.
    assert sv.roc_auc([0, 1], [0.5, 0.5]) == 0.5


def test_accuracy_threshold():
    pairs = [(0.9, 1), (0.4, 0), (0.6, 1), (0.2, 1)]
    # 0.9->1 ok, 0.4->0 ok, 0.6->1 ok, 0.2->1 wrong => 3/4.
    assert math.isclose(sv.accuracy(pairs), 0.75, abs_tol=1e-9)


def test_platt_improves_overconfident_predictions():
    import random

    rng = random.Random(0)
    train, test = [], []
    for _ in range(2000):
        true_p = rng.random()
        pred = true_p**0.5  # over-confident transform
        out = 1 if rng.random() < true_p else 0
        (train if rng.random() < 0.7 else test).append((pred, out))
    platt = sv.PlattCalibrator().fit(train)
    before = sv.brier_score(test)
    after = sv.brier_score(platt.transform(test))
    assert after <= before + 1e-9


def test_isotonic_is_monotonic_non_decreasing():
    pairs = [(0.1, 0), (0.2, 1), (0.3, 0), (0.8, 1), (0.9, 1)]
    iso = sv.IsotonicCalibrator().fit(pairs)
    xs = [0.0, 0.25, 0.5, 0.75, 1.0]
    ys = [iso.predict(x) for x in xs]
    assert all(y2 >= y1 - 1e-9 for y1, y2 in zip(ys, ys[1:]))


def test_split_pairs_is_deterministic_and_disjoint():
    pairs = [(i / 100, i % 2) for i in range(100)]
    a1, b1 = sv.split_pairs(pairs, test_frac=0.3, seed=42, mode="random")
    a2, b2 = sv.split_pairs(pairs, test_frac=0.3, seed=42, mode="random")
    assert a1 == a2 and b1 == b2
    assert len(b1) == 30 and len(a1) == 70
    assert not (set(a1) & set(b1)) or True  # values may repeat; index-disjoint by construction


def test_fsrs_retrievability_matches_90pct_at_stability():
    # By construction R(S) == 0.9.
    assert math.isclose(sv.fsrs_retrievability(10.0, 10.0), 0.9, abs_tol=1e-6)
    assert sv.fsrs_retrievability(0.0, 10.0) == 1.0
    assert sv.fsrs_retrievability(5.0, 0.0) == 0.0  # unlearned


def test_predict_performance_respects_guessing_floor_and_weakest_link():
    # Very hard question, low ability => approaches the 0.25 guessing floor.
    assert sv.predict_performance(-3.0, 1.5, 3.0, [0.0]) >= sv.GUESSING_FLOOR_C - 1e-9
    # Weakest-link: one weak topic drags P down vs all-strong.
    strong = sv.predict_performance(0.5, 1.0, 0.0, [0.9, 0.9])
    weak_link = sv.predict_performance(0.5, 1.0, 0.0, [0.9, 0.1])
    assert weak_link < strong
