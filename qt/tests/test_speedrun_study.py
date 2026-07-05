# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Guards for the Speedrun guided-session study surface.

The one behaviour we lock down here is the RECAP invariant: a wrong recap
answer must NEVER take the miss-reason path (the only path that calls
``record_miss_reason`` / ``activate_cards_for_miss`` and unsuspends cards).
Recap re-tests the *same material* practised in Phase 1, so it may reveal the
explanation and grade the question (native answer path -> revlog), but it must
not unlock/activate any new cards. That decision lives in the pure
``offers_activation`` gate, so we assert it directly without standing up a Qt
dialog.
"""

from aqt.speedrun.study import (
    MODE_HELDOUT,
    MODE_PRACTICE,
    MODE_RECAP,
    MODE_STANDALONE,
    offers_activation,
)


def test_recap_wrong_answer_never_activates_cards():
    # A missed recap question must not offer the miss-reason chooser, so
    # activation / card unlocking can never be triggered during recap.
    assert offers_activation(MODE_RECAP, is_correct=False) is False


def test_heldout_wrong_answer_never_activates_cards():
    # Held-out / paraphrase are pure evaluation surfaces: a wrong answer is
    # graded into revlog but must never offer the miss chooser (no activation),
    # so answering the eval split can't leak into the served-pool models.
    assert offers_activation(MODE_HELDOUT, is_correct=False) is False


def test_practice_and_standalone_still_activate_on_miss():
    # The Practice phase and standalone study keep the gating loop: a wrong
    # answer offers the miss-reason chooser (which can activate linked cards).
    assert offers_activation(MODE_PRACTICE, is_correct=False) is True
    assert offers_activation(MODE_STANDALONE, is_correct=False) is True


def test_correct_answer_never_activates_in_any_mode():
    # A correct answer never routes through the miss/activation path.
    for mode in (MODE_RECAP, MODE_PRACTICE, MODE_STANDALONE, MODE_HELDOUT):
        assert offers_activation(mode, is_correct=True) is False
