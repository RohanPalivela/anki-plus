# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from anki.consts import QUEUE_TYPE_SUSPENDED
from anki.decks import DeckId
from anki.speedrun import (
    BANK_AI_GENERATED_TAG,
    BANK_TAG,
    DEFAULT_BANK_PATH,
    QUESTION_FIELDS,
    QUESTION_NOTETYPE_NAME,
    QUESTIONS_DECK_NAME,
    MissReason,
    load_question_bank,
)
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


# Data-model provisioning + question-first loop (desktop M1)
##############################################################################


def test_setup_mcat_provisions_and_is_idempotent():
    col = getEmptyCol()
    summary = col.speedrun.setup_mcat(load_demo_data=True)

    # Notetype with the frozen field order.
    notetype = col.models.by_name(QUESTION_NOTETYPE_NAME)
    assert notetype is not None
    assert [f["name"] for f in notetype["flds"]] == list(QUESTION_FIELDS)

    # Decks + blueprint + value ordering toggle.
    assert col.decks.id_for_name(QUESTIONS_DECK_NAME) is not None
    blueprint = col.get_config("speedrunBlueprint")
    assert blueprint and len(blueprint["topics"]) == summary.blueprint_topics
    assert col.get_config("speedrunOrdering") is True

    # Synthetic demo data is clearly tagged and served-only.
    assert summary.demo_loaded and not summary.demo_already_present
    assert summary.questions_created >= 12
    served = col.speedrun.served_question_note_ids()
    assert len(served) == summary.questions_created
    assert all("pool::served" in col.get_note(nid).tags for nid in served)

    # Re-running is a no-op for content (idempotent, D-13).
    note_count = col.db.scalar("select count() from notes")
    summary2 = col.speedrun.setup_mcat(load_demo_data=True)
    assert summary2.demo_already_present
    assert col.db.scalar("select count() from notes") == note_count


def test_question_first_loop_miss_activates_linked_cards():
    """Drives the exact pylib sequence the Qt study dialog uses."""
    col = getEmptyCol()
    col.speedrun.setup_mcat(load_demo_data=True)
    served = col.speedrun.served_question_note_ids()
    assert served

    note = col.get_note(served[0])
    topic = next(t for t in note.tags if t.startswith("topic::"))
    suspended_targets = col.find_cards(f"tag:{topic} is:suspended")
    assert suspended_targets, "expected suspended gating flashcards for the topic"

    # 1) Answer the served question incorrectly via the native path -> revlog.
    revlog_before = col.db.scalar("select count() from revlog")
    card = note.cards()[0]
    card.start_timer()
    col.sched.answerCard(card, 1)  # Again == incorrect
    assert col.db.scalar("select count() from revlog") == revlog_before + 1

    # 2) Classify the miss as a knowledge gap -> gated activation unsuspends
    #    the linked cards and records the latest miss tag.
    resp = col.speedrun.record_miss_reason(note.id, MissReason.KNOWLEDGE_GAP)
    assert len(resp.activated_card_ids) >= 1
    assert "miss::knowledge-gap" in col.get_note(note.id).tags
    for cid in suspended_targets:
        assert col.get_card(cid).queue != QUEUE_TYPE_SUSPENDED

    # A non-memory reason is a no-op (no activation), even via the UI path.
    noop = col.speedrun.record_miss_reason(int(served[1]), MissReason.CARELESS)
    assert list(noop.activated_card_ids) == []
    assert "miss::careless" in col.get_note(served[1]).tags


def _sample_bank() -> list[dict]:
    return [
        {
            "uid": "test-bio-1",
            "stem": "Which organelle is the primary site of ATP synthesis?",
            "options": ["Nucleus", "Mitochondrion", "Ribosome", "Golgi apparatus"],
            "correct": "B",
            "explanation": "Mitochondria carry out oxidative phosphorylation.",
            "topics": ["biology"],
            "pool": "served",
            "source": "MMLU — college biology",
            "license": "MIT",
            "origin": "mmlu",
            "difficulty_b": 0.0,
            "discrimination_a": 1.0,
            "ai_generated": False,
        },
        {
            "uid": "test-phys-1",
            "stem": "A block slides down a frictionless incline. Its acceleration is?",
            "options": ["Zero", "g sin(theta)", "g", "g cos(theta)"],
            "correct": "B",
            "explanation": "",
            "topics": ["physics"],
            "pool": "heldout",
            "source": "OpenMCAT — C/P bank",
            "license": "AGPL-3.0",
            "origin": "openmcat",
            "difficulty_b": 1.0,
            "discrimination_a": 1.2,
            "ai_generated": True,
        },
    ]


def test_import_question_bank_is_native_split_and_idempotent():
    col = getEmptyCol()
    col.speedrun.setup_mcat(load_demo_data=False)

    summary = col.speedrun.import_question_bank(questions=_sample_bank())
    assert summary.imported == 2
    assert summary.skipped_existing == 0
    assert summary.by_origin == {"mmlu": 1, "openmcat": 1}
    assert summary.by_pool == {"served": 1, "heldout": 1}

    # Stored as native SpeedrunQuestion notes; held-out is never served (D-8).
    served = col.speedrun.served_question_note_ids()
    assert len(served) == 1
    note = col.get_note(served[0])
    assert note.note_type()["name"] == QUESTION_NOTETYPE_NAME
    assert BANK_TAG in note.tags
    assert "topic::biology" in note.tags
    assert note["correct"] == "B"

    # AI-generated third-party content is honestly tagged for the M2 eval gate.
    assert len(col.find_notes(f"tag:{BANK_AI_GENERATED_TAG}")) == 1

    # Re-import adds nothing (idempotent -> conflict-free sync, no duplicates).
    note_count = col.db.scalar("select count() from notes")
    summary2 = col.speedrun.import_question_bank(questions=_sample_bank())
    assert summary2.imported == 0
    assert summary2.skipped_existing == 2
    assert col.db.scalar("select count() from notes") == note_count


def test_vendored_question_bank_ships_and_imports():
    # The generated (gzipped) bank must ship next to the module and be well-formed.
    assert DEFAULT_BANK_PATH.exists()
    data = load_question_bank()
    assert data["questions"], "vendored bank should be non-empty"
    assert data["attribution"], "vendored bank must carry source attribution"

    col = getEmptyCol()
    col.speedrun.setup_mcat(load_demo_data=False)
    subset = data["questions"][:25]
    summary = col.speedrun.import_question_bank(
        questions=subset, attribution=data["attribution"]
    )
    assert summary.imported == 25
    assert summary.attribution


def test_memory_dashboard_populated_after_demo():
    col = getEmptyCol()
    col.speedrun.setup_mcat(load_demo_data=True)

    score = col.speedrun.get_memory_score()
    # Every blueprint topic is surfaced.
    assert len(score.topics) >= 7
    # The synthetic studied cards cross the abstention thresholds (D-6).
    assert score.graded_count >= 30
    assert not score.abstained
    assert 0.0 < score.overall <= 1.0
    # Per-topic mastery shows a real spread (value ordering is meaningful).
    masteries = sorted(t.mastery for t in score.topics if t.known)
    assert masteries[0] < masteries[-1]
