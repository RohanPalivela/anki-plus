# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from anki.consts import QUEUE_TYPE_SUSPENDED
from anki.decks import DeckId
from anki.speedrun import (
    BANK_AI_GENERATED_TAG,
    BANK_TAG,
    BANK_UID_TAG_PREFIX,
    DEFAULT_BANK_PATH,
    DEFAULT_FIRST_PRINCIPLES_PATH,
    DEFAULT_MCAT_BLUEPRINT,
    FIRST_PRINCIPLES_TAG,
    FLASHCARDS_DECK_NAME,
    GATES_LINKED_CONFIG_KEY,
    GATES_TAG_PREFIX,
    LEGACY_DEMO_TAG,
    QUESTION_FIELDS,
    QUESTION_NOTETYPE_NAME,
    QUESTIONS_DECK_NAME,
    SESSION_STATE_CONFIG_KEY,
    MissReason,
    compute_recap_score,
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
        elapsed = int(elapsed_ratios[i % len(elapsed_ratios)] * stability_days * 86400)
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


def test_gates_linkage_prefers_named_cards_over_topic():
    """A question carrying a resolvable ``gates::<nid>`` tag activates exactly
    the named card, not its whole coarse ``topic::`` set (precise > coarse)."""
    col = getEmptyCol()
    gated = _add_note(col, ["topic::biochem"])
    sibling = _add_note(col, ["topic::biochem"])
    gated_cids = [c.id for c in gated.cards()]
    sibling_cids = [c.id for c in sibling.cards()]
    col.sched.suspend_cards(gated_cids + sibling_cids)

    question = _add_note(col, ["topic::biochem", f"gates::{gated.id}"])
    resp = col.speedrun.activate_cards_for_miss(question.id, MissReason.KNOWLEDGE_GAP)
    assert sorted(resp.activated_card_ids) == sorted(gated_cids)
    # The topic-only sibling stays suspended: precise gates linkage wins.
    assert all(col.get_card(c).queue == QUEUE_TYPE_SUSPENDED for c in sibling_cids)


def test_dangling_gates_falls_back_to_topic_linkage():
    """A ``gates::`` tag that resolves to nothing usable falls back to topic
    linkage rather than silently activating zero cards for a legit miss."""
    col = getEmptyCol()
    sibling = _add_note(col, ["topic::physics"])
    sibling_cids = [c.id for c in sibling.cards()]
    col.sched.suspend_cards(sibling_cids)

    question = _add_note(col, ["topic::physics", "gates::999999999"])
    resp = col.speedrun.activate_cards_for_miss(question.id, MissReason.KNOWLEDGE_GAP)
    assert sorted(resp.activated_card_ids) == sorted(sibling_cids)


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


def _glycolysis_scenario(col):
    """Import one biology question that clearly concerns glycolysis plus two
    biology first-principles cards (glycolysis + membrane transport), so the
    linkage pass has a genuine signal to pick the precise card. Returns
    (question_nid, glycolysis_fp_nid, membrane_fp_nid)."""
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(
        questions=[
            {
                **_sample_bank()[0],
                "uid": "q-glyc",
                "topics": ["biology"],
                "pool": "served",
                "stem": (
                    "During glycolysis, what is the net yield of ATP and NADH "
                    "per glucose molecule?"
                ),
                "options": ["1 ATP", "2 ATP and 2 NADH", "36 ATP", "none"],
                "correct": "B",
                "explanation": (
                    "Glycolysis nets two ATP and two NADH per glucose in the cytoplasm."
                ),
            }
        ]
    )
    col.speedrun.import_first_principles(
        cards=[
            {
                "uid": "fp-glyc-1",
                "topic": "biology",
                "concept": "glycolysis",
                "front": "What is the net yield of glycolysis?",
                "back": "Glucose to two pyruvate nets two ATP and two NADH.",
            },
            {
                "uid": "fp-mem-1",
                "topic": "biology",
                "concept": "membrane-transport",
                "front": "Passive vs active transport?",
                "back": "Passive moves down the gradient; active spends ATP.",
            },
        ]
    )
    question = col.speedrun.served_question_note_ids()[0]
    glyc = col.find_notes(f"tag:{FIRST_PRINCIPLES_TAG} tag:concept::glycolysis")[0]
    mem = col.find_notes(f"tag:{FIRST_PRINCIPLES_TAG} tag:concept::membrane-transport")[
        0
    ]
    return question, glyc, mem


def test_import_emits_precise_gates_for_matching_question():
    """(a) The import/linkage pass stamps a precise ``gates::`` tag pointing at
    only the first-principles card the question actually concerns."""
    col = getEmptyCol()
    question, glyc, mem = _glycolysis_scenario(col)

    gates = [t for t in col.get_note(question).tags if t.startswith(GATES_TAG_PREFIX)]
    # Precise: linked to the glycolysis card, not the membrane-transport one.
    assert gates == [f"{GATES_TAG_PREFIX}{glyc}"]
    assert f"{GATES_TAG_PREFIX}{mem}" not in gates
    # The pass marks the collection as linked (so back-fill runs once).
    assert col.get_config(GATES_LINKED_CONFIG_KEY) is True


def test_link_gated_first_principles_is_idempotent():
    """(b) Re-running the linkage pass writes nothing and never duplicates."""
    col = getEmptyCol()
    question, _glyc, _mem = _glycolysis_scenario(col)

    tags_before = list(col.get_note(question).tags)
    summary = col.speedrun.link_gated_first_principles()
    assert summary.notes_updated == 0  # already linked at import time
    tags_after = list(col.get_note(question).tags)
    assert tags_after == tags_before
    # Exactly one gates:: tag — no accumulation across runs.
    assert sum(t.startswith(GATES_TAG_PREFIX) for t in tags_after) == 1


def test_gates_path_activates_precise_first_principles_card():
    """(c) End-to-end: a miss on the glycolysis question activates only the
    glycolysis card via the gates path, not the whole biology topic."""
    col = getEmptyCol()
    question, glyc, mem = _glycolysis_scenario(col)

    glyc_cids = col.find_cards(f"nid:{glyc}")
    mem_cids = col.find_cards(f"nid:{mem}")
    assert all(col.get_card(c).queue == QUEUE_TYPE_SUSPENDED for c in glyc_cids)
    assert all(col.get_card(c).queue == QUEUE_TYPE_SUSPENDED for c in mem_cids)

    resp = col.speedrun.record_miss_reason(int(question), MissReason.KNOWLEDGE_GAP)
    assert set(resp.activated_card_ids) == set(glyc_cids)
    assert all(col.get_card(c).queue != QUEUE_TYPE_SUSPENDED for c in glyc_cids)
    # The other topic card stays suspended — precision, not whole-topic.
    assert all(col.get_card(c).queue == QUEUE_TYPE_SUSPENDED for c in mem_cids)


def test_no_first_principles_leaves_no_gates_and_uses_topic_fallback():
    """(d) A question whose topic has no first-principles card gets no
    ``gates::`` tag and still activates its topic-linked cards via the Rust
    topic fallback — activation is never broken."""
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(
        questions=[
            {**_sample_bank()[0], "uid": "q-x", "topics": ["biology"], "pool": "served"}
        ]
    )
    # A topic-linked flashcard that is NOT a first-principles card.
    flashcard = _add_note(col, ["topic::biology"])
    cids = [c.id for c in flashcard.cards()]
    col.sched.suspend_cards(cids)

    col.speedrun.link_gated_first_principles()
    question = col.speedrun.served_question_note_ids()[0]
    assert not [
        t for t in col.get_note(question).tags if t.startswith(GATES_TAG_PREFIX)
    ]

    resp = col.speedrun.record_miss_reason(int(question), MissReason.KNOWLEDGE_GAP)
    assert set(resp.activated_card_ids) == set(cids)


def test_ensure_gates_linked_runs_once_then_noops():
    """The back-fill helper links a legacy collection exactly once."""
    col = getEmptyCol()
    _glycolysis_scenario(col)
    # Import already linked + set the flag, so ensure_* is a no-op.
    assert col.speedrun.ensure_gates_linked() is None

    # Simulate a legacy collection (bank present, flag never set).
    col.remove_config(GATES_LINKED_CONFIG_KEY)
    summary = col.speedrun.ensure_gates_linked()
    assert summary is not None
    assert summary.served_questions >= 1
    assert col.get_config(GATES_LINKED_CONFIG_KEY) is True


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
        assert front not in bank_stems, (
            f"FP card restates a bank question: {card['uid']}"
        )

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
    assert bio_fp and all(col.get_card(c).queue != QUEUE_TYPE_SUSPENDED for c in bio_fp)

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


# Performance + Readiness models (M3) — 2PL-IRT + Monte-Carlo readiness
##############################################################################

_BLUEPRINT_TOPICS = [
    "biochemistry",
    "biology",
    "general-chemistry",
    "organic-chemistry",
    "physics",
    "psychology",
    "sociology",
]


def _served_bank_across_topics(per_topic=3) -> list[dict]:
    """Served questions spread across every blueprint topic (so coverage is
    full) with a spread of difficulties, for the Performance/Readiness models."""
    base = _sample_bank()[0]
    out: list[dict] = []
    for t in _BLUEPRINT_TOPICS:
        for i in range(per_topic):
            out.append(
                {
                    **base,
                    "uid": f"m3-{t}-{i}",
                    "topics": [t],
                    "pool": "served",
                    "difficulty_b": round(-1.0 + 0.5 * i, 2),
                    "discrimination_a": 1.0,
                }
            )
    return out


def test_performance_score_abstains_then_beats_chance_with_synthetic_seed():
    """The Performance RPC abstains on no data, and once synthetic seed
    responses are added it scores above chance and is honestly labelled
    synthetic (the gate: synthetic data is never silent)."""
    col = getEmptyCol()
    col.speedrun.setup_mcat()

    # Fresh collection: no responses -> abstain, not synthetic.
    perf = col.speedrun.get_performance_score()
    assert perf.abstained
    assert not perf.synthetic
    assert perf.graded_count == 0

    col.speedrun.import_question_bank(questions=_served_bank_across_topics())
    added = col.speedrun.seed_synthetic_responses(
        responses_per_question=4, true_theta=1.5, seed=123
    )
    assert added.added > 0

    perf = col.speedrun.get_performance_score()
    # Gated + labelled: any surfaced score reports synthetic == True.
    assert perf.synthetic
    assert not perf.abstained
    # Beats chance (4-option guessing floor is 0.25).
    assert perf.overall > 0.4
    # Honest interval brackets the point estimate within [0, 1].
    assert 0.0 <= perf.range_low <= perf.overall <= perf.range_high <= 1.0
    # A +1.5 true ability recovers a positive theta.
    assert perf.theta > 0.0
    # Per-topic breakdown covers the whole blueprint.
    assert len(perf.topics) >= len(_BLUEPRINT_TOPICS)


def test_readiness_score_abstains_then_projects_scaled_score():
    """The Readiness RPC abstains on thin data and, once seeded with full
    coverage, emits a deterministic MCAT-scaled median + ordered 80% interval."""
    col = getEmptyCol()
    col.speedrun.setup_mcat()

    assert col.speedrun.get_readiness_score().abstained

    col.speedrun.import_question_bank(questions=_served_bank_across_topics(per_topic=3))
    col.speedrun.seed_synthetic_responses(
        responses_per_question=6, true_theta=1.5, seed=7
    )

    readiness = col.speedrun.get_readiness_score()
    assert readiness.synthetic
    assert not readiness.abstained
    # Scaled score sits inside the MCAT range with an ordered interval.
    assert 472.0 <= readiness.scaled_low <= readiness.scaled_median
    assert readiness.scaled_median <= readiness.scaled_high <= 528.0
    # Coverage is full (all blueprint topics exercised) and confidence positive.
    assert readiness.coverage > 0.9
    assert readiness.confidence > 0.0
    # Fixed seed -> reproducible projection on a re-fetch.
    again = col.speedrun.get_readiness_score()
    assert again.scaled_median == readiness.scaled_median
    assert list(again.top_reasons) == list(readiness.top_reasons)


def test_readiness_abstains_on_low_coverage_even_with_data():
    """Even with plenty of responses, Readiness abstains when only a couple of
    blueprint topics have been exercised (coverage below the threshold)."""
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    # Questions in only two of seven blueprint topics -> ~0.29 coverage.
    base = _sample_bank()[0]
    narrow = [
        {
            **base,
            "uid": f"narrow-{t}-{i}",
            "topics": [t],
            "pool": "served",
            "difficulty_b": 0.0,
            "discrimination_a": 1.0,
        }
        for t in ("biology", "physics")
        for i in range(4)
    ]
    col.speedrun.import_question_bank(questions=narrow)
    col.speedrun.seed_synthetic_responses(
        responses_per_question=6, true_theta=1.5, seed=99
    )
    readiness = col.speedrun.get_readiness_score()
    assert readiness.abstained  # low coverage triggers the give-up rule
    assert readiness.coverage < 0.5


def test_seed_synthetic_responses_is_undoable_and_gated():
    """The synthetic seeder only adds revlog rows, flips the synthetic flag, and
    is undoable — it never leaks into a real (unseeded) score silently."""
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(questions=_served_bank_across_topics(per_topic=2))

    revlog_before = col.db.scalar("select count() from revlog")
    added = col.speedrun.seed_synthetic_responses(
        responses_per_question=3, true_theta=1.0, seed=5
    )
    assert added.added > 0
    assert col.db.scalar("select count() from revlog") == revlog_before + added.added
    # A separate, never-seeded collection reports synthetic == False (no leak).
    other = getEmptyCol()
    other.speedrun.setup_mcat()
    assert not other.speedrun.get_performance_score().synthetic


# Recap: same-material (concept-scoped) selection + transfer scoring scaffold
##############################################################################


def _bank_uid_index(col) -> dict[str, int]:
    """Map each served question's ``bankuid::`` to its note id."""
    index: dict[str, int] = {}
    for nid in col.speedrun.served_question_note_ids():
        note = col.get_note(nid)
        uid = next(
            t[len(BANK_UID_TAG_PREFIX) :]
            for t in note.tags
            if t.startswith(BANK_UID_TAG_PREFIX)
        )
        index[uid] = int(nid)
    return index


def test_recap_selection_scoped_to_practiced_concepts():
    """Recap selects distinct questions of the SAME concepts practised in
    Phase 1 (not merely the same topic), excludes the exact Phase-1 item, and
    falls back to topic scope for concept-less questions so it is never empty."""
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(
        questions=[
            # Two glycolysis (same concept) biology questions,
            {
                **_sample_bank()[0],
                "uid": "glyc-a",
                "topics": ["biology"],
                "concept": "glycolysis",
                "pool": "served",
            },
            {
                **_sample_bank()[0],
                "uid": "glyc-b",
                "topics": ["biology"],
                "concept": "glycolysis",
                "pool": "served",
            },
            # a different-concept biology question (same topic, other material),
            {
                **_sample_bank()[0],
                "uid": "mem-a",
                "topics": ["biology"],
                "concept": "membrane-transport",
                "pool": "served",
            },
            # a concept-less biology question (topic-fallback candidate),
            {
                **_sample_bank()[0],
                "uid": "bare-a",
                "topics": ["biology"],
                "pool": "served",
            },
            # and an unrelated-topic question.
            {
                **_sample_bank()[0],
                "uid": "phys-a",
                "topics": ["physics"],
                "concept": "work-energy",
                "pool": "served",
            },
        ]
    )
    by_uid = _bank_uid_index(col)

    # Phase 1 practised the glycolysis biology question "glyc-a".
    recap = col.speedrun.served_questions_interleaved(
        topics={"biology"},
        concepts={"glycolysis"},
        exclude={by_uid["glyc-a"]},
    )

    # Same material, different item: the other glycolysis question is included.
    assert by_uid["glyc-b"] in recap
    # Topic fallback: the concept-less biology question is included (never empty).
    assert by_uid["bare-a"] in recap
    # The exact Phase-1 item is excluded (recap re-tests, not replays).
    assert by_uid["glyc-a"] not in recap
    # Different concept in the same topic is NOT "same material".
    assert by_uid["mem-a"] not in recap
    # Unrelated topic is excluded by the topic scope.
    assert by_uid["phys-a"] not in recap


def test_compute_recap_score_overall_and_per_concept():
    """The pure transfer score micro-averages overall accuracy and reports a
    stable, concept-sorted per-concept breakdown (incl. the "" fallback bucket)."""
    answered = {"glycolysis": 4, "membrane-transport": 2, "": 2}
    correct = {"glycolysis": 3, "membrane-transport": 1}

    score = compute_recap_score(answered, correct)

    assert score.answered == 8
    assert score.correct == 4
    assert score.overall == 0.5  # 4 / 8, micro-averaged across all items
    # Stable order, sorted by concept slug ("" sorts first).
    assert [c.concept for c in score.per_concept] == [
        "",
        "glycolysis",
        "membrane-transport",
    ]
    per = {c.concept: c for c in score.per_concept}
    assert per["glycolysis"].accuracy == 0.75
    assert per["membrane-transport"].accuracy == 0.5
    # A concept answered but never correct reports 0.0 (no ZeroDivision).
    assert per[""].answered == 2 and per[""].correct == 0
    assert per[""].accuracy == 0.0


def test_compute_recap_score_empty_abstains_without_error():
    """No recap answers yet -> a zeroed score, never a divide-by-zero."""
    score = compute_recap_score({}, {})
    assert score.answered == 0
    assert score.correct == 0
    assert score.overall == 0.0
    assert score.per_concept == []


# Curriculum data/API layer (W4 — concept-structured navigation)
##############################################################################


def _curriculum_bank() -> list[dict]:
    """Two glycolysis + one membrane-transport biology questions, and one
    physics question — enough to exercise concept grouping and counts."""
    base = _sample_bank()[0]
    return [
        {**base, "uid": "glyc-a", "topics": ["biology"], "concept": "glycolysis"},
        {**base, "uid": "glyc-b", "topics": ["biology"], "concept": "glycolysis"},
        {
            **base,
            "uid": "mem-a",
            "topics": ["biology"],
            "concept": "membrane-transport",
        },
        {
            **base,
            "uid": "phys-a",
            "topics": ["physics"],
            "concept": "work-energy",
            "pool": "served",
        },
    ]


def _answer(col, note_id, correct):
    card = col.get_note(note_id).cards()[0]
    card.start_timer()
    col.sched.answerCard(card, 3 if correct else 1, from_queue=False)


def test_curriculum_groups_concepts_with_counts_and_lessons():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(questions=_curriculum_bank())
    col.speedrun.import_first_principles(
        cards=[
            {
                "uid": "fp-glyc",
                "topic": "biology",
                "concept": "glycolysis",
                "front": "Net yield of glycolysis?",
                "back": "Two ATP and two NADH per glucose.",
            },
            {
                "uid": "fp-mem",
                "topic": "biology",
                "concept": "membrane-transport",
                "front": "Passive vs active transport?",
                "back": "Passive follows the gradient; active spends ATP.",
            },
        ]
    )

    curriculum = col.speedrun.curriculum()

    # Every blueprint topic is surfaced (structure), even empty ones.
    topic_names = {t.topic for t in curriculum.topics}
    assert {"biology", "physics"} <= topic_names
    assert len(curriculum.topics) == len(DEFAULT_MCAT_BLUEPRINT["topics"])

    biology = next(t for t in curriculum.topics if t.topic == "biology")
    # Concepts are grouped under their content topic, sorted by slug.
    assert [c.concept for c in biology.concepts] == ["glycolysis", "membrane-transport"]
    glyc = next(c for c in biology.concepts if c.concept == "glycolysis")
    assert glyc.served_questions == 2
    assert glyc.lesson_cards == 1
    # Curated taxonomy label is used on desktop.
    assert glyc.label == "Glycolysis & carbohydrate metabolism"
    # Lesson cards import suspended -> not activated, not reviewed, not practised.
    assert glyc.lessons_activated == 0 and glyc.lessons_reviewed == 0
    assert not glyc.practiced

    # A concept with no served questions is not invented; physics has work-energy.
    physics = next(t for t in curriculum.topics if t.topic == "physics")
    assert [c.concept for c in physics.concepts] == ["work-energy"]
    assert physics.concepts[0].lesson_cards == 0


def test_curriculum_progress_reads_revlog_and_activation():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(questions=_curriculum_bank())
    col.speedrun.import_first_principles(
        cards=[
            {
                "uid": "fp-glyc",
                "topic": "biology",
                "concept": "glycolysis",
                "front": "Net yield of glycolysis?",
                "back": "Two ATP and two NADH per glucose.",
            }
        ]
    )
    by_uid = _bank_uid_index(col)

    # Answer one glycolysis question right, one wrong -> 1/2 = 50% accuracy.
    _answer(col, by_uid["glyc-a"], correct=True)
    _answer(col, by_uid["glyc-b"], correct=False)
    # A qualifying miss activates the linked glycolysis lesson card.
    col.speedrun.record_miss_reason(by_uid["glyc-b"], MissReason.KNOWLEDGE_GAP)

    glyc = col.speedrun.curriculum().concept("glycolysis")
    assert glyc is not None
    assert glyc.answered == 2
    assert glyc.correct == 1
    assert glyc.accuracy == 0.5
    assert glyc.practiced
    # The gated miss unsuspended the glycolysis lesson card.
    assert glyc.lessons_activated == 1


def test_weak_concepts_prioritises_unpractised_then_low_accuracy():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(questions=_curriculum_bank())
    by_uid = _bank_uid_index(col)

    # glycolysis: practised but weak (1/2). membrane-transport: never practised.
    # work-energy (physics): practised and solid (correct).
    _answer(col, by_uid["glyc-a"], correct=True)
    _answer(col, by_uid["glyc-b"], correct=False)
    _answer(col, by_uid["phys-a"], correct=True)

    weak = col.speedrun.weak_concepts()
    # Never-practised concept comes first, then the low-accuracy one; the solid
    # concept is not weak and is excluded.
    assert weak[0] == "membrane-transport"
    assert "glycolysis" in weak
    assert "work-energy" not in weak


def test_curriculum_to_dict_is_json_shaped():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(questions=_curriculum_bank())

    data = col.speedrun.curriculum().to_dict()
    assert set(data) == {"topics", "overallMastery", "masteryAbstained"}
    biology = next(t for t in data["topics"] if t["topic"] == "biology")
    assert set(biology) >= {
        "topic",
        "label",
        "weight",
        "mastery",
        "masteryKnown",
        "concepts",
        "servedQuestions",
    }
    concept = biology["concepts"][0]
    assert set(concept) >= {
        "concept",
        "label",
        "topic",
        "servedQuestions",
        "lessonCards",
        "answered",
        "correct",
        "accuracy",
        "practiced",
    }
