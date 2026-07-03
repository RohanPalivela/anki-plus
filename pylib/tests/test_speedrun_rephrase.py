# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for the Speedrun AI rephrasal generator (``anki.speedrun_rephrase``).

Split in two: the pure provider/grader/leakage logic (no built backend needed)
and the native ``generate_variants`` path plus the offline eval harness (which
need a live collection / the vendored gold set).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from anki.speedrun import BANK_AI_GENERATED_TAG
from anki.speedrun_rephrase import (
    AI_GENERATED_TAG,
    DEFAULT_MIN_QUALITY,
    LABEL_BAD_TEACHING,
    LABEL_CORRECT_USEFUL,
    LABEL_WRONG,
    VARIANT_OF_TAG_PREFIX,
    VARIANT_UID_TAG_PREFIX,
    HeuristicCardGrader,
    HeuristicGrader,
    MockProvider,
    OpenAIProvider,
    RephrasedCard,
    RephrasedQuestion,
    SourceCard,
    SourceQuestion,
    ai_variant_note_ids,
    generate_card_variants,
    generate_variants,
    jaccard,
    parse_card_provider_json,
    parse_provider_json,
    remove_card_variants,
    rephrase_question,
    scan_leakage,
    tokens,
)
from tests.shared import getEmptyCol

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(**overrides: Any) -> SourceQuestion:
    base: dict[str, Any] = dict(
        stem="Which organelle is the primary site of ATP synthesis?",
        options=["Nucleus", "Mitochondrion", "Ribosome", "Golgi apparatus"],
        correct="B",
        explanation="Mitochondria carry out oxidative phosphorylation to make ATP.",
        source="MMLU — college biology",
        topics=["biology"],
        concept="cellular-respiration",
    )
    base.update(overrides)
    return SourceQuestion(**base)


# --- Pure tokenisation / value objects ---------------------------------------


def test_tokens_drop_stopwords_and_short_tokens():
    toks = tokens("The Mitochondrion is an organelle of the cell")
    assert "mitochondrion" in toks
    assert "organelle" in toks
    assert "the" not in toks  # stopword
    assert "is" not in toks  # stopword + short
    assert "of" not in toks


def test_jaccard_bounds():
    assert jaccard(set(), set()) == 0.0
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert 0.0 < jaccard({"a", "b"}, {"a", "c"}) < 1.0


def test_source_question_from_dict_normalizes_correct_and_options():
    src = SourceQuestion.from_dict(
        {
            "stem": "s",
            "options": "Nucleus\nMitochondrion\nRibosome\nGolgi",
            "correct": "2",  # 1-based number -> index 1 -> letter B
            "explanation": "e",
            "source": "src",
            "topics": ["biology"],
        }
    )
    assert src.options == ["Nucleus", "Mitochondrion", "Ribosome", "Golgi"]
    assert src.correct == "B"
    assert src.correct_option == "Mitochondrion"


# --- MockProvider + HeuristicGrader ------------------------------------------


def test_mock_good_preserves_answer_rewords_and_grades_useful():
    src = _source()
    variant = MockProvider().rephrase(src)
    # Fact preserved: the correct option text is unchanged.
    assert variant.correct_option == src.correct_option == "Mitochondrion"
    # Stem is reworded (not a verbatim copy).
    assert variant.stem != src.stem
    grade = HeuristicGrader().grade(src, variant)
    assert grade.label == LABEL_CORRECT_USEFUL
    assert grade.answer_preserved and grade.options_grounded and grade.reworded
    assert grade.score >= DEFAULT_MIN_QUALITY


def test_mock_wrong_is_caught_as_answer_drift():
    src = _source()
    variant = MockProvider(quality="wrong").rephrase(src)
    assert variant.correct_option != src.correct_option
    grade = HeuristicGrader().grade(src, variant)
    assert grade.label == LABEL_WRONG
    assert not grade.acceptable


def test_mock_verbatim_is_bad_teaching_not_wrong():
    src = _source()
    variant = MockProvider(quality="verbatim").rephrase(src)
    grade = HeuristicGrader().grade(src, variant)
    assert grade.label == LABEL_BAD_TEACHING
    # Bad teaching is filtered but never dangerous, so still "acceptable".
    assert grade.acceptable


def test_grader_preserves_short_and_enumerated_answers():
    """A verbatim-identical correct option counts as preserved even when it has
    no content tokens (True/False, numbers, "All of the above")."""
    for options, correct in (
        (["True", "False"], "A"),
        (["7.4", "1.0", "10.0", "13.5"], "A"),
        (["All of the above", "None", "Only I", "Only II"], "A"),
    ):
        src = _source(options=options, correct=correct)
        variant = MockProvider().rephrase(src)
        grade = HeuristicGrader().grade(src, variant)
        assert grade.answer_preserved, options
        assert grade.label != LABEL_WRONG, options


def test_grader_flags_leakage_near_duplicate():
    src = _source()
    # Heldout text mirrors production (_heldout_texts): stem + options.
    heldout = (
        "Which organelle is the primary site of ATP synthesis? "
        "Nucleus Mitochondrion Ribosome Golgi apparatus"
    )
    grader = HeuristicGrader(heldout_texts=[heldout])
    # A variant that mirrors the heldout stem+options should be flagged leaked.
    leaky = RephrasedQuestion(
        stem="Which organelle is the primary site of ATP synthesis?",
        options=list(src.options),
        correct="B",
        explanation="x" * 50,
        source=src.source,
    )
    grade = grader.grade(src, leaky)
    assert grade.leaked
    assert grade.label == LABEL_WRONG


# --- Provider JSON parsing / OpenAI availability -----------------------------


def test_parse_provider_json_accepts_index_and_inherits_source():
    src = _source()
    content = (
        '{"stem": "Reworded?", "options": ["Nucleus", "Mitochondrion", '
        '"Ribosome", "Golgi apparatus"], "correct_index": 1, '
        '"explanation": "because"}'
    )
    variant = parse_provider_json(content, src)
    assert variant.correct == "B"
    assert variant.correct_option == "Mitochondrion"
    assert variant.source == src.source  # inherited when omitted


def test_openai_provider_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert OpenAIProvider.available() is False


def test_rephrase_question_delegates_to_provider():
    variant = rephrase_question(
        {
            "stem": "s",
            "options": ["Nucleus", "Mitochondrion", "Ribosome", "Golgi"],
            "correct": "B",
            "explanation": "e",
            "source": "src",
        },
        MockProvider(),
    )
    assert isinstance(variant, RephrasedQuestion)
    assert variant.correct_option == "Mitochondrion"


def test_scan_leakage_flags_heldout_inputs_and_near_dups():
    inputs = [{"uid": "a", "pool": "served"}, {"uid": "b", "pool": "heldout"}]
    heldout = [
        {
            "uid": "b",
            "stem": "Which organelle is the primary site of ATP synthesis?",
            "options": ["Nucleus", "Mitochondrion", "Ribosome", "Golgi apparatus"],
        }
    ]
    variant_texts = [
        "Which organelle is the primary site of ATP synthesis? "
        "Nucleus Mitochondrion Ribosome Golgi apparatus"
    ]
    report = scan_leakage(inputs, variant_texts, heldout)
    assert report.heldout_in_inputs == ["b"]
    assert report.near_duplicates
    assert not report.clean


# --- Native generation (needs a collection) ----------------------------------


def _served_bank() -> list[dict]:
    return [
        {
            "uid": "reph-bio-1",
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
        }
    ]


def _source_card(**overrides: Any) -> SourceCard:
    base: dict[str, Any] = dict(
        front="First principle: how do you compute the work done by a constant force?",
        back=(
            "Work W = F d cos(theta); only the force component along the "
            "displacement does work, so perpendicular forces do zero work. "
            "Net work equals the change in kinetic energy."
        ),
        source="first-principles fp-phys-work-1",
        topics=["physics"],
        concept="work-energy",
    )
    base.update(overrides)
    return SourceCard(**base)


def test_mock_card_good_preserves_fact_rewords_and_grades_useful():
    src = _source_card()
    variant = MockProvider().rephrase_card(src)
    assert variant.front != src.front  # front reworded
    assert variant.back == src.back  # fact preserved verbatim
    grade = HeuristicCardGrader().grade(src, variant)
    assert grade.label == LABEL_CORRECT_USEFUL
    assert grade.fact_preserved and grade.reworded and grade.good_teaching
    assert grade.score >= DEFAULT_MIN_QUALITY


def test_mock_card_wrong_is_caught_as_fact_drift():
    src = _source_card()
    variant = MockProvider(quality="wrong").rephrase_card(src)
    grade = HeuristicCardGrader().grade(src, variant)
    assert grade.label == LABEL_WRONG
    assert not grade.fact_preserved
    assert not grade.acceptable


def test_mock_card_verbatim_front_is_bad_teaching_not_wrong():
    src = _source_card()
    variant = MockProvider(quality="verbatim").rephrase_card(src)
    grade = HeuristicCardGrader().grade(src, variant)
    assert grade.label == LABEL_BAD_TEACHING
    assert grade.fact_preserved and not grade.reworded
    assert grade.acceptable  # bad teaching is filtered but never dangerous


def test_card_grader_flags_leakage_near_duplicate():
    src = _source_card()
    heldout = src.front + " " + src.back
    grader = HeuristicCardGrader(heldout_texts=[heldout])
    leaky = RephrasedCard(front=src.front, back=src.back)
    grade = grader.grade(src, leaky)
    assert grade.leaked
    assert grade.label == LABEL_WRONG


def test_parse_card_provider_json_reads_front_and_back():
    src = _source_card()
    content = '{"front": "Reworded front?", "back": "Reworded but same fact."}'
    variant = parse_card_provider_json(content, src)
    assert variant.front == "Reworded front?"
    assert variant.back == "Reworded but same fact."


def _first_principles_cards() -> list[dict]:
    return [
        {
            "uid": "fp-bio-atp-1",
            "topic": "biology",
            "concept": "cellular-respiration",
            "front": "First principle: where is most ATP produced in the cell?",
            "back": (
                "Most ATP is produced in the mitochondrion via oxidative "
                "phosphorylation, which uses the electron transport chain and "
                "chemiosmosis to drive ATP synthase."
            ),
        }
    ]


def test_generate_card_variants_ai_off_is_clean_noop():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    before = col.db.scalar("select count() from notes")
    summary = generate_card_variants(col, provider=None)
    assert summary.ai_disabled is True
    assert summary.written == 0
    assert col.db.scalar("select count() from notes") == before


def test_generate_card_variants_writes_suspended_variant_and_is_idempotent():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_first_principles(cards=_first_principles_cards())

    summary = generate_card_variants(col, provider=MockProvider())
    assert summary.considered == 1
    assert summary.written == 1

    nids = col.find_notes(f"tag:{AI_GENERATED_TAG}")
    assert len(nids) == 1
    note = col.get_note(nids[0])
    tags = note.tags
    assert any(t.startswith(VARIANT_OF_TAG_PREFIX) for t in tags)
    assert any(t.startswith(VARIANT_UID_TAG_PREFIX) for t in tags)
    assert "topic::biology" in tags
    assert "concept::cellular-respiration" in tags
    # The fact is preserved and the front reworded.
    assert "mitochondrion" in note["Back"].lower()
    assert note["Front"] != _first_principles_cards()[0]["front"]
    # Written as suspended (review-only until a related miss activates it).
    for card in note.cards():
        assert card.queue == -1  # QUEUE_TYPE_SUSPENDED

    # Re-running writes nothing new (stable variantuid gate).
    again = generate_card_variants(col, provider=MockProvider())
    assert again.written == 0
    assert again.skipped_existing >= 1
    assert len(col.find_notes(f"tag:{AI_GENERATED_TAG}")) == 1


def test_remove_card_variants_deletes_only_ai_notes():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_first_principles(cards=_first_principles_cards())
    source_notes = col.db.scalar("select count() from notes")

    generate_card_variants(col, provider=MockProvider())
    assert len(ai_variant_note_ids(col)) == 1
    assert col.db.scalar("select count() from notes") == source_notes + 1

    removed = remove_card_variants(col)
    assert removed == 1
    # Only the AI variant is gone; the source first-principles card remains.
    assert ai_variant_note_ids(col) == []
    assert col.db.scalar("select count() from notes") == source_notes

    # Idempotent: a second cleanup with nothing to remove is a clean no-op.
    assert remove_card_variants(col) == 0

    # Regression: after deletion the idempotency check must NOT treat the
    # removed variant as still-existing (orphaned tags in the registry once
    # caused this), so regeneration writes a fresh variant again.
    regenerated = generate_card_variants(col, provider=MockProvider())
    assert regenerated.written == 1
    assert regenerated.skipped_existing == 0
    assert len(ai_variant_note_ids(col)) == 1


def test_ai_enabled_toggle_defaults_off_and_persists():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    assert col.speedrun.ai_enabled() is False
    col.speedrun.set_ai_enabled(True)
    assert col.speedrun.ai_enabled() is True
    col.speedrun.set_ai_enabled(False)
    assert col.speedrun.ai_enabled() is False


def test_ai_generated_tag_matches_speedrun_constant():
    assert AI_GENERATED_TAG == BANK_AI_GENERATED_TAG


def test_generate_variants_ai_off_is_clean_noop():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    before = col.db.scalar("select count() from notes")
    summary = generate_variants(col, provider=None)
    assert summary.ai_disabled is True
    assert summary.written == 0
    assert col.db.scalar("select count() from notes") == before


def test_generate_variants_writes_traceable_variant_and_is_idempotent():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    col.speedrun.import_question_bank(questions=_served_bank())
    served = col.speedrun.served_question_note_ids()
    assert served

    summary = generate_variants(col, provider=MockProvider())
    assert summary.written == 1
    assert summary.considered == 1

    nids = col.find_notes(f"tag:{AI_GENERATED_TAG}")
    assert len(nids) == 1
    note = col.get_note(nids[0])
    tags = note.tags
    assert any(t.startswith(VARIANT_OF_TAG_PREFIX) for t in tags)
    assert any(t.startswith(VARIANT_UID_TAG_PREFIX) for t in tags)
    assert any(t.startswith("topic::") for t in tags)
    # The fact is preserved and the origin is traceable.
    assert "Mitochondrion" in note["options"]
    assert "AI rephrasal of" in note["source"]

    # Re-running writes nothing new (stable variantuid gate).
    again = generate_variants(col, provider=MockProvider())
    assert again.written == 0
    assert again.skipped_existing >= 1
    assert len(col.find_notes(f"tag:{AI_GENERATED_TAG}")) == 1


def test_generate_variants_refuses_heldout():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    heldout = {**_served_bank()[0], "uid": "reph-held-1", "pool": "heldout"}
    col.speedrun.import_question_bank(questions=[heldout, _served_bank()[0]])
    # Feed every question note id, including the heldout one.
    all_qs = list(col.find_notes("note:SpeedrunQuestion"))
    summary = generate_variants(col, all_qs, provider=MockProvider())
    assert summary.refused_heldout >= 1


# --- Offline eval harness ----------------------------------------------------


def _load_eval_module():
    import sys

    path = _REPO_ROOT / "tools" / "speedrun" / "rephrase_eval.py"
    spec = importlib.util.spec_from_file_location("speedrun_rephrase_eval", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve string annotations
    # (the module uses ``from __future__ import annotations``).
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_eval_mock_passes_and_wrong_provider_fails():
    ev = _load_eval_module()
    gold = ev.load_gold()

    good = ev.evaluate(MockProvider(), gold)
    assert good.passed
    assert good.accuracy >= ev.PASS_ACCURACY_CUTOFF
    assert good.wrong_rate <= ev.MAX_WRONG_RATE
    assert good.leakage_clean and good.heldout_refused
    assert good.beats_baseline

    bad = ev.evaluate(MockProvider(quality="wrong"), gold)
    assert not bad.passed
    assert bad.wrong_rate > ev.MAX_WRONG_RATE
