# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from anki.consts import QUEUE_TYPE_SUSPENDED
from anki.decks import DeckId
from anki.speedrun import MissReason
from tests.shared import getEmptyCol


def _add_note(col, tags):
    note = col.new_note(col.models.by_name("Basic"))
    note["Front"] = "front"
    note["Back"] = "back"
    note.tags = tags
    col.add_note(note, DeckId(1))
    return note


def test_activate_cards_for_miss_rpc():
    col = getEmptyCol()
    question = _add_note(col, ["topic::biochem"])
    flashcard = _add_note(col, ["topic::biochem"])
    cids = [c.id for c in flashcard.cards()]
    col.sched.suspend_cards(cids)
    assert all(c.queue == QUEUE_TYPE_SUSPENDED for c in flashcard.cards())

    # A qualifying reason unsuspends the linked card(s).
    resp = col.speedrun.activate_cards_for_miss(question.id, MissReason.KNOWLEDGE_GAP)
    assert sorted(resp.activated_card_ids) == sorted(cids)
    assert all(c.queue != QUEUE_TYPE_SUSPENDED for c in flashcard.cards())

    # A non-qualifying reason is a no-op and leaves the cards suspended.
    col.sched.suspend_cards(cids)
    resp = col.speedrun.activate_cards_for_miss(
        question.id, MissReason.MISUNDERSTANDING
    )
    assert list(resp.activated_card_ids) == []
    assert all(c.queue == QUEUE_TYPE_SUSPENDED for c in flashcard.cards())


def test_record_miss_reason_sets_latest_tag_and_activates():
    col = getEmptyCol()
    question = _add_note(col, ["topic::physio"])
    flashcard = _add_note(col, ["topic::physio"])
    cids = [c.id for c in flashcard.cards()]
    col.sched.suspend_cards(cids)

    resp = col.speedrun.record_miss_reason(question.id, MissReason.KNOWLEDGE_GAP)
    assert sorted(resp.activated_card_ids) == sorted(cids)
    question.load()
    assert "miss::knowledge-gap" in question.tags

    # A later, different reason replaces the prior miss tag (D-11: latest only).
    col.speedrun.record_miss_reason(question.id, MissReason.CARELESS)
    question.load()
    assert "miss::knowledge-gap" not in question.tags
    assert "miss::careless" in question.tags


def test_coverage_sweep_and_memory_score_rpcs():
    col = getEmptyCol()
    for topic in ("a", "b", "c"):
        note = _add_note(col, [f"topic::{topic}"])
        col.sched.suspend_cards([c.id for c in note.cards()])

    swept = col.speedrun.run_coverage_sweep(1)
    assert len(swept.activated_card_ids) >= 1

    # With essentially no graded data, the Memory model must abstain (D-6).
    score = col.speedrun.get_memory_score()
    assert score.abstained
