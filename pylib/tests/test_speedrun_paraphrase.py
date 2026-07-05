# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Unit tests for the Speedrun paraphrase-test gap aggregation (WS6).

Loads the standalone ``tools/speedrun/paraphrase_test.py`` by file path (it is a
tool, not a package) and checks the pure gap maths + the data set integrity —
no collection or backend required.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _ROOT / "tools" / "speedrun" / "paraphrase_test.py"
_DATA = _ROOT / "tools" / "speedrun" / "data" / "paraphrase_set.json"

_spec = importlib.util.spec_from_file_location("paraphrase_test", _TOOL)
pt = importlib.util.module_from_spec(_spec)
sys.modules["paraphrase_test"] = pt  # dataclass annotations resolve via this
_spec.loader.exec_module(pt)


def test_dataset_has_30_cards_and_valid_indices():
    cards = json.loads(_DATA.read_text())["cards"]
    assert len(cards) == 30
    total_q = 0
    for c in cards:
        assert len(c["reworded"]) == 2
        for q in c["reworded"]:
            assert 0 <= q["correct"] < len(q["options"])
            total_q += 1
    assert total_q == 60


def test_summarize_gap_is_recall_minus_reworded():
    results = [
        pt.CardResult("a", "biochem", recall=0.9, reworded_acc=0.6),
        pt.CardResult("b", "biochem", recall=0.8, reworded_acc=0.7),
    ]
    s = pt.summarize(results)
    assert abs(s["mean_recall"] - 0.85) < 1e-9
    assert abs(s["mean_reworded_acc"] - 0.65) < 1e-9
    assert abs(s["gap"] - 0.20) < 1e-9
    assert s["by_topic"]["biochem"]["n"] == 2


def test_zero_penalty_demo_gives_negligible_gap():
    cards = json.loads(_DATA.read_text())["cards"]
    results = pt.demo_results(cards, seed=1, transfer_penalty=0.0)
    s = pt.summarize(results)
    # With no transfer penalty the gap should be ~0 (only symmetric noise) — the
    # honest "no bridge" null.
    assert abs(s["gap"]) < 0.05


def test_positive_penalty_demo_gives_positive_gap():
    cards = json.loads(_DATA.read_text())["cards"]
    s = pt.summarize(pt.demo_results(cards, seed=1, transfer_penalty=0.2))
    assert s["gap"] > 0.1


def test_empty_results():
    assert pt.summarize([]) == {"n": 0}
