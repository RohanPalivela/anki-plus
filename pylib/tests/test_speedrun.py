# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from anki.consts import QUEUE_TYPE_SUSPENDED
from anki.decks import DeckId
from anki.speedrun import (
    BANK_AI_GENERATED_TAG,
    BANK_TAG,
    DEFAULT_BANK_PATH,
    DEFAULT_FIRST_PRINCIPLES_PATH,
    DEFAULT_MCAT_BLUEPRINT,
    FIRST_PRINCIPLES_TAG,
    FLASHCARDS_DECK_NAME,
    LEGACY_DEMO_TAG,
    QUESTION_FIELDS,
    QUESTION_NOTETYPE_NAME,
    QUESTIONS_DECK_NAME,
    SESSION_STATE_CONFIG_KEY,
    MissReason,
    load_first_principles,
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


def _seed_graded_flashcards(col, topics, per_topic=5):
    """Create already-reviewed flashcards with FSRS state so the Memory model
    has enough graded data to score (mirrors what real study would produce).

    Elapsed-since-review is spread across topics so per-topic mastery varies,
    keeping the value-ordering signal meaningful.
    """
    import time

    from anki.cards import FSRSMemoryState
    from anki.consts import CARD_TYPE_REV, QUEUE_TYPE_REV

    now = int(time.time())
    today = col.sched.today
    elapsed_ratios = [0.2, 0.4, 0.7, 1.0, 1.6, 2.5, 4.0]
    stability_days = 80.0
    for i, topic in enumerate(topics):
        elapsed = int(
            elapsed_ratios[i % len(elapsed_ratios)] * stability_days * 86400
        )
        for _ in range(per_topic):
            note = _add_note(col, [f"topic::{topic}"])
            for card in note.cards():
                card.memory_state = FSRSMemoryState(
                    stability=stability_days, difficulty=5.0 + 0.3 * i
                )
                card.last_review_time = now - elapsed
                card.type = CARD_TYPE_REV
                card.queue = QUEUE_TYPE_REV
                card.reps = 3
                card.due = today + 30
                col.update_card(card)


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
    summary = col.speedrun.setup_mcat()

    # Notetype with the frozen field order.
    notetype = col.models.by_name(QUESTION_NOTETYPE_NAME)
    assert notetype is not None
    assert [f["name"] for f in notetype["flds"]] == list(QUESTION_FIELDS)

    # Decks + blueprint + value ordering toggle.
    assert col.decks.id_for_name(QUESTIONS_DECK_NAME) is not None
    blueprint = col.get_config("speedrunBlueprint")
    assert blueprint and len(blueprint["topics"]) == summary.blueprint_topics
    assert col.get_config("speedrunOrdering") is True

    # Setup never creates practice content: no served questions, bank not
    # imported, and nothing to purge in a fresh collection.
    assert summary.served_question_count == 0
    assert not summary.bank_imported
    assert summary.demo_notes_removed == 0
    assert col.speedrun.served_question_note_ids() == []
    assert not col.speedrun.has_question_bank()

    # Re-running is a no-op for content (idempotent, D-13).
    note_count = col.db.scalar("select count() from notes")
    col.speedrun.setup_mcat()
    assert col.db.scalar("select count() from notes") == note_count


def test_setup_mcat_purges_legacy_synthetic_demo_data():
    col = getEmptyCol()
    col.speedrun.setup_mcat()

    # Simulate a collection provisioned by an older build that seeded synthetic
    # demo notes (a served placeholder question + a linked flashcard).
    _add_note(col, ["topic::biology", "pool::served", LEGACY_DEMO_TAG])
    _add_note(col, ["topic::biology", LEGACY_DEMO_TAG])

    summary = col.speedrun.setup_mcat()
    assert summary.demo_notes_removed == 2
    assert col.find_notes(f"tag:{LEGACY_DEMO_TAG}") == []
    # A second purge finds nothing left.
    assert col.speedrun.purge_demo_data() == 0


def test_has_question_bank_gates_on_served_import():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    assert not col.speedrun.has_question_bank()

    # A held-out-only import does not satisfy the gate (nothing is served).
    heldout_only = [q for q in _sample_bank() if q["pool"] == "heldout"]
    col.speedrun.import_question_bank(questions=heldout_only)
    assert not col.speedrun.has_question_bank()

    # Importing a served question opens the gate.
    col.speedrun.import_question_bank(questions=_sample_bank())
    assert col.speedrun.has_question_bank()


def test_question_first_loop_miss_activates_linked_cards():
    """Drives the exact pylib sequence the Qt study dialog uses."""
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    # Real served questions come from the imported bank; add two so we can test
    # both a qualifying and a non-qualifying miss.
    col.speedrun.import_question_bank(
        questions=[
            {**_sample_bank()[0], "uid": "loop-bio-1", "topics": ["biology"]},
            {
                **_sample_bank()[0],
                "uid": "loop-bio-2",
                "topics": ["biology"],
                "pool": "served",
            },
        ]
    )
    # Linked flashcards (same topic) start suspended — these are the gating
    # targets a qualifying miss should unsuspend.
    flashcard = _add_note(col, ["topic::biology"])
    col.sched.suspend_cards([c.id for c in flashcard.cards()])

    served = col.speedrun.served_question_note_ids()
    assert len(served) == 2

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
    col.speedrun.setup_mcat()

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
    col.speedrun.setup_mcat()
    subset = data["questions"][:25]
    summary = col.speedrun.import_question_bank(
        questions=subset, attribution=data["attribution"]
    )
    assert summary.imported == 25
    assert summary.attribution


def test_served_questions_unseen_first_avoids_repeats():
    """``unseen_first`` keeps a capped Practice batch from re-serving questions
    that were already practised, so a new session gets fresh problems until the
    served pool is exhausted."""
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    # Six served questions spread across two topics.
    col.speedrun.import_question_bank(
        questions=[
            {
                **_sample_bank()[0],
                "uid": f"seen-{i}",
                "topics": ["biology" if i % 2 else "physics"],
                "pool": "served",
            }
            for i in range(6)
        ]
    )
    served = col.speedrun.served_question_note_ids()
    assert len(served) == 6

    # Default ordering is deterministic (this is the source of the repeats).
    first_batch = col.speedrun.served_questions_interleaved()[:3]
    assert col.speedrun.served_questions_interleaved()[:3] == first_batch

    # Practise the first batch: answer each linked question card once.
    import time

    for nid in first_batch:
        card = col.get_note(nid).cards()[0]
        card.start_timer()
        col.sched.answerCard(card, 3, from_queue=False)
        time.sleep(0.01)

    # unseen_first now surfaces the three not-yet-practised questions before any
    # repeats, so a capped batch of 3 shares nothing with the practised batch.
    unseen_batch = col.speedrun.served_questions_interleaved(unseen_first=True)[:3]
    assert set(unseen_batch).isdisjoint(set(first_batch))
    assert set(unseen_batch) == set(served) - set(first_batch)

    # Practising everything exhausts the unseen pool; only then do repeats
    # return — and oldest-practised first, so it isn't the same leading batch.
    for nid in unseen_batch:
        card = col.get_note(nid).cards()[0]
        card.start_timer()
        col.sched.answerCard(card, 3, from_queue=False)
        time.sleep(0.01)
    rotated = col.speedrun.served_questions_interleaved(unseen_first=True)
    assert set(rotated) == set(served)
    assert set(rotated[:3]) == set(first_batch)


def _sample_first_principles() -> list[dict]:
    return [
        {
            "uid": "fp-test-bio-1",
            "topic": "biology",
            "concept": "membrane-transport",
            "front": "First principle: passive vs active transport?",
            "back": "Passive moves down the gradient (no ATP); active moves against it (ATP).",
        },
        {
            "uid": "fp-test-phys-1",
            "topic": "physics",
            "concept": "work-energy",
            "front": "First principle: definition of mechanical work?",
            "back": "W = F d cos(theta); net work equals the change in kinetic energy.",
        },
    ]


def test_import_first_principles_native_suspended_and_idempotent():
    col = getEmptyCol()
    col.speedrun.setup_mcat()

    summary = col.speedrun.import_first_principles(cards=_sample_first_principles())
    assert summary.imported == 2
    assert summary.suspended == 2
    assert summary.by_topic == {"biology": 1, "physics": 1}
    assert col.speedrun.has_first_principles()

    # Stored as native Basic notes in Speedrun::Cards, tagged + suspended.
    nids = col.find_notes(f"tag:{FIRST_PRINCIPLES_TAG}")
    assert len(nids) == 2
    deck_id = col.decks.id_for_name(FLASHCARDS_DECK_NAME)
    for nid in nids:
        note = col.get_note(nid)
        assert note.note_type()["name"] == "Basic"
        assert any(t.startswith("topic::") for t in note.tags)
        assert any(t.startswith("concept::") for t in note.tags)
        for card in note.cards():
            assert card.did == deck_id
            assert card.queue == QUEUE_TYPE_SUSPENDED

    # First-principles cards are NOT part of the served question pool.
    assert col.speedrun.served_question_note_ids() == []

    # Re-import adds nothing (idempotent -> conflict-free sync).
    note_count = col.db.scalar("select count() from notes")
    summary2 = col.speedrun.import_first_principles(cards=_sample_first_principles())
    assert summary2.imported == 0
    assert summary2.skipped_existing == 2
    assert col.db.scalar("select count() from notes") == note_count


def test_missed_question_activates_linked_first_principles_card():
    """The end-to-end gate: a missed served question unsuspends the linked
    first-principles memory card that shares its topic — not another question."""
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(
        questions=[{**_sample_bank()[0], "uid": "q-bio-1", "topics": ["biology"]}]
    )
    col.speedrun.import_first_principles(cards=_sample_first_principles())

    bio_fp = col.find_cards(f"tag:topic::biology tag:{FIRST_PRINCIPLES_TAG}")
    assert bio_fp and all(col.get_card(c).queue == QUEUE_TYPE_SUSPENDED for c in bio_fp)

    served = col.speedrun.served_question_note_ids()
    assert len(served) == 1  # only the question is served, never the FP card

    resp = col.speedrun.record_miss_reason(int(served[0]), MissReason.KNOWLEDGE_GAP)
    # The biology first-principles card is activated; the physics one stays off.
    assert set(resp.activated_card_ids) == set(bio_fp)
    assert all(col.get_card(c).queue != QUEUE_TYPE_SUSPENDED for c in bio_fp)
    phys_fp = col.find_cards(f"tag:topic::physics tag:{FIRST_PRINCIPLES_TAG}")
    assert all(col.get_card(c).queue == QUEUE_TYPE_SUSPENDED for c in phys_fp)


def test_vendored_first_principles_ship_and_are_not_restated_questions():
    assert DEFAULT_FIRST_PRINCIPLES_PATH.exists()
    data = load_first_principles()
    cards = data["cards"]
    assert cards, "vendored first-principles set should be non-empty"

    blueprint_topics = {t["name"] for t in DEFAULT_MCAT_BLUEPRINT["topics"]}
    uids = set()
    for card in cards:
        assert card["uid"] and card["uid"] not in uids, "uids must be unique"
        uids.add(card["uid"])
        assert card["topic"] in blueprint_topics, f"unknown topic: {card['topic']}"
        assert card["front"] and card["back"]

    # Guard the core constraint: a first-principles card must never restate a
    # bank question verbatim (they are a separate set).
    bank_stems = {
        " ".join(q.get("stem", "").split()) for q in load_question_bank()["questions"]
    }
    for card in cards:
        front = " ".join(card["front"].split())
        assert front not in bank_stems, f"FP card restates a bank question: {card['uid']}"

    # Imports cleanly into a real collection.
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    summary = col.speedrun.import_first_principles()
    assert summary.imported == len(cards)
    assert summary.suspended == summary.imported


def test_reset_profile_clears_progress_but_keeps_content():
    """reset_profile re-suspends + forgets activated memory cards (graded_count
    returns to 0) and drops any paused session, while keeping the imported
    question bank and memory-card notes intact."""
    from anki.cards import FSRSMemoryState
    from anki.consts import CARD_TYPE_REV, QUEUE_TYPE_REV

    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(
        questions=[{**_sample_bank()[0], "uid": "q-bio-1", "topics": ["biology"]}]
    )
    col.speedrun.import_first_principles(cards=_sample_first_principles())

    # Activate the biology first-principles card by missing its linked question.
    served = col.speedrun.served_question_note_ids()
    col.speedrun.record_miss_reason(int(served[0]), MissReason.KNOWLEDGE_GAP)
    bio_fp = col.find_cards(f"tag:topic::biology tag:{FIRST_PRINCIPLES_TAG}")
    assert bio_fp and all(
        col.get_card(c).queue != QUEUE_TYPE_SUSPENDED for c in bio_fp
    )

    # Grade it (simulate an FSRS review) so it reads as graded, then leave a
    # paused guided-session behind.
    for cid in bio_fp:
        card = col.get_card(cid)
        card.memory_state = FSRSMemoryState(stability=40.0, difficulty=5.0)
        card.last_review_time = col.sched.today  # any non-null review marker
        card.type = CARD_TYPE_REV
        card.queue = QUEUE_TYPE_REV
        card.reps = 1
        col.update_card(card)
    assert col.speedrun.get_memory_score().graded_count == len(bio_fp)
    col.set_config(SESSION_STATE_CONFIG_KEY, {"phase": 1, "practice_index": 3})

    note_count_before = col.db.scalar("select count() from notes")

    summary = col.speedrun.reset_profile()

    assert summary.session_cleared is True
    assert summary.cards_resuspended >= len(bio_fp)
    assert summary.cards_forgotten >= len(bio_fp)

    # All first-principles cards are back to suspended with no FSRS memory.
    all_fp = col.find_cards(f"tag:{FIRST_PRINCIPLES_TAG}")
    assert all_fp
    for cid in all_fp:
        card = col.get_card(cid)
        assert card.queue == QUEUE_TYPE_SUSPENDED
        assert card.memory_state is None

    # graded_count returns to 0, paused session is gone.
    assert col.speedrun.get_memory_score().graded_count == 0
    assert col.get_config(SESSION_STATE_CONFIG_KEY, None) is None

    # Content is kept: question bank + first-principles notes all still present.
    assert col.speedrun.has_question_bank()
    assert col.speedrun.served_question_note_ids() == list(served)
    assert col.speedrun.has_first_principles()
    assert col.db.scalar("select count() from notes") == note_count_before


def test_reset_profile_is_safe_without_speedrun_data():
    col = getEmptyCol()
    summary = col.speedrun.reset_profile()
    assert summary.session_cleared is False
    assert summary.cards_resuspended == 0
    assert summary.cards_forgotten == 0


def test_memory_dashboard_populated_after_reviews():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    blueprint = col.get_config("speedrunBlueprint")
    topics = [t["name"] for t in blueprint["topics"]]

    # Before any reviews exist the Memory model abstains (D-6).
    assert col.speedrun.get_memory_score().abstained

    # Real graded reviews (not synthetic demo content) give the model data.
    _seed_graded_flashcards(col, topics, per_topic=5)

    score = col.speedrun.get_memory_score()
    # Every blueprint topic is surfaced.
    assert len(score.topics) >= 7
    # Enough graded cards across enough topics to cross the abstention gate.
    assert score.graded_count >= 30
    assert not score.abstained
    assert 0.0 < score.overall <= 1.0
    # Per-topic mastery shows a real spread (value ordering is meaningful).
    masteries = sorted(t.mastery for t in score.topics if t.known)
    assert masteries[0] < masteries[-1]
