# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for the deterministic Speedrun question-bank curation tool.

The curation module lives under ``tools/speedrun`` (a maintainer tool, not
shipped runtime code), so it is imported by path here.
"""

from __future__ import annotations

import sys
from pathlib import Path

from anki.speedrun import (
    CONCEPT_TAG_PREFIX,
    DEFAULT_MCAT_BLUEPRINT,
    POOL_HELDOUT_TAG,
    POOL_SERVED_TAG,
    concept_of_note,
    correct_index,
    load_concepts,
    load_question_bank,
    option_lines,
)
from tests.shared import getEmptyCol

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "tools" / "speedrun"))

import curate  # type: ignore[import-not-found]  # noqa: E402


def _item(**overrides) -> dict:
    base = {
        "uid": "u-1",
        "stem": "Which organelle is the primary site of ATP synthesis in cells?",
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
    base.update(overrides)
    return base


# --- rule (a): malformed drops -----------------------------------------------


def test_drops_malformed_items():
    items = [
        _item(uid="ok"),  # kept
        _item(uid="short", stem="Too short"),  # < 15 chars
        _item(uid="empty", stem="   "),  # empty
        _item(uid="fewopts", options=["Only one"]),  # < 2 options
        _item(uid="dup", options=["Same", "same", "Other"]),  # dup (case-insens.)
        _item(uid="badcorrect", correct="Z"),  # unresolvable to an index
        _item(uid="numcorrect5", correct="5"),  # out of range (only 4 options)
    ]
    curated, report = curate.curate_bank(items)
    kept = {q["uid"] for q in curated}
    assert kept == {"ok"}
    d = report["dropped"]
    assert d["short-stem"] == 1
    assert d["empty-stem"] == 1
    assert d["few-options"] == 1
    assert d["duplicate-options"] == 1
    # both "Z" and out-of-range "5" resolve to nothing usable
    assert d["unresolvable-correct"] == 2


def test_numeric_correct_is_resolvable():
    curated, _ = curate.curate_bank([_item(uid="numok", correct="2")])
    assert len(curated) == 1  # 1-based "2" -> valid index


# --- rule (b): length cap + passage drops ------------------------------------


def test_length_cap_removes_long_and_passage_from_served():
    long_stem = "A well formed question stem. " + ("padding " * 120)
    assert len(long_stem) > curate.MAX_SERVED_STEM_CHARS
    passage = (
        "Passage: The Krebs cycle\nA long vignette describing an experiment "
        "in detail that is really a reading-comprehension passage and not a "
        "discrete multiple choice question at all."
    )
    items = [
        _item(uid="short-ok"),
        _item(uid="too-long", stem=long_stem),
        _item(uid="passage", stem=passage),
    ]
    curated, report = curate.curate_bank(items)
    kept = {q["uid"] for q in curated}
    assert "short-ok" in kept
    assert "too-long" not in kept
    assert "passage" not in kept
    assert report["dropped"].get("over-length", 0) == 1
    assert report["dropped"].get("passage-style", 0) == 1
    # no served stem ever exceeds the cap
    assert all(
        len(q["stem"]) <= curate.MAX_SERVED_STEM_CHARS
        for q in curated
        if q["pool"] == "served"
    )


def test_over_length_heldout_item_is_kept_as_heldout_candidate_but_capped():
    # A long *heldout* item is not served-capped (it's never served), but the
    # served length invariant still holds for everything served.
    long_stem = "Heldout long stem. " + ("padding " * 120)
    items = [_item(uid=f"s{i}") for i in range(10)]
    items.append(_item(uid="h-long", stem=long_stem, pool="heldout"))
    curated, _ = curate.curate_bank(items)
    assert all(
        len(q["stem"]) <= curate.MAX_SERVED_STEM_CHARS
        for q in curated
        if q["pool"] == "served"
    )


# --- rule (c): rebalancing respects blueprint ordering -----------------------


def test_allocation_respects_blueprint_weight_ordering():
    weights = {t["name"]: t["weight"] for t in DEFAULT_MCAT_BLUEPRINT["topics"]}
    # Abundant availability everywhere, so allocation is purely weight-driven.
    available = {t: 100_000 for t in weights}
    alloc = curate._allocate(weights, available, curate.TARGET_SERVED_TOTAL)
    # Higher weight must never receive fewer slots than a lower-weight topic.
    ordered = sorted(weights, key=lambda t: weights[t], reverse=True)
    counts = [alloc[t] for t in ordered]
    assert counts == sorted(counts, reverse=True)
    # Highest-weight topic gets the most; lowest gets the least.
    assert alloc["biochemistry"] == max(alloc.values())


def test_tiny_topics_pull_in_all_valid_items():
    # organic-chemistry has only a few items but must not be zeroed out.
    items = [_item(uid="bio-%d" % i, topics=["biology"]) for i in range(50)]
    items += [
        _item(
            uid="ochem-%d" % i,
            topics=["organic-chemistry"],
            stem="A carboxylic acid question about acidity and resonance %d?" % i,
        )
        for i in range(3)
    ]
    curated, report = curate.curate_bank(items)
    served_ochem = [
        q
        for q in curated
        if q["pool"] == "served" and q["topics"] == ["organic-chemistry"]
    ]
    assert len(served_ochem) == 3  # all three pulled in


def test_served_totals_land_in_target_range_on_real_bank():
    data = load_question_bank()
    _curated, report = curate.curate_bank(data["questions"])
    served = report["after"]["served"]
    assert 1200 <= served <= 1800, served


# --- rule (e): concept assignment --------------------------------------------


def test_concept_assignment_is_deterministic_and_in_taxonomy():
    glyc = _item(
        uid="glyc",
        topics=["biochemistry"],
        stem="During glycolysis, what is the net ATP yield from glucose?",
        options=["1 ATP", "2 ATP", "36 ATP", "0 ATP"],
        explanation="Glycolysis converts glucose to pyruvate for a net of 2 ATP.",
    )
    assert curate.assign_concept(glyc) == "glycolysis"

    coulomb = _item(
        uid="cou",
        topics=["physics"],
        stem="How does the electrostatic force between two point charges scale?",
        options=["1/r", "1/r^2", "r", "r^2"],
        explanation="Coulomb's law: the electric field of a point charge is 1/r^2.",
    )
    assert curate.assign_concept(coulomb) == "electrostatics"

    # Off-taxonomy / no-signal item stays topic-only.
    vague = _item(
        uid="vague",
        topics=["biology"],
        stem="Which of the following statements is the most accurate overall?",
        options=["First", "Second", "Third", "Fourth"],
        explanation="",
    )
    assert curate.assign_concept(vague) == ""


def test_every_assigned_concept_belongs_to_item_topic():
    data = load_question_bank()
    curated, _ = curate.curate_bank(data["questions"])
    by_topic: dict[str, set[str]] = {}
    for c in curate.CONCEPT_TAXONOMY:
        by_topic.setdefault(c.topic, set()).add(c.id)
    for q in curated:
        concept = q.get("concept")
        if concept:
            assert concept in by_topic[q["topics"][0]]


# --- determinism -------------------------------------------------------------


def test_curation_is_deterministic():
    data = load_question_bank()
    c1, r1 = curate.curate_bank(data["questions"])
    c2, r2 = curate.curate_bank(data["questions"])
    assert [q["uid"] for q in c1] == [q["uid"] for q in c2]
    assert r1 == r2
    # Serialized gzip bytes are byte-identical too (reproducible vendored file).
    b1 = curate._bank_gz_bytes(curate.build_curated_bank(data)[0])
    b2 = curate._bank_gz_bytes(curate.build_curated_bank(data)[0])
    assert b1 == b2


# --- taxonomy contract -------------------------------------------------------


def test_curate_blueprint_matches_speedrun_blueprint():
    """The tool's local blueprint copy must not drift from the source of truth."""
    assert curate.DEFAULT_BLUEPRINT == DEFAULT_MCAT_BLUEPRINT


def test_vendored_taxonomy_matches_module_and_loads():
    vendored = load_concepts()
    generated = curate.build_taxonomy_json()
    assert vendored == generated, (
        "speedrun_concepts.json is stale; run `python tools/speedrun/curate.py "
        "--in-place`"
    )
    # Every concept is well-formed and kebab-case, grouped by blueprint topics.
    blueprint_topics = {t["name"] for t in DEFAULT_MCAT_BLUEPRINT["topics"]}
    slugs = set()
    for c in vendored["concepts"]:
        assert c["topic"] in blueprint_topics
        assert c["id"] == c["id"].lower().replace(" ", "")
        assert "_" not in c["id"] and " " not in c["id"]
        assert c["id"] not in slugs, "duplicate concept slug"
        slugs.add(c["id"])
        assert c["label"]


def test_first_principles_concepts_are_in_taxonomy():
    """The FP cards' concepts anchor the gates:: linkage, so the taxonomy must
    cover them all."""
    from anki.speedrun import load_first_principles

    taxonomy_slugs = {c["id"] for c in load_concepts()["concepts"]}
    for card in load_first_principles()["cards"]:
        assert card["concept"] in taxonomy_slugs, card["concept"]


# --- rule (f): curated vendored bank imports cleanly -------------------------


def test_vendored_curated_bank_is_well_formed():
    data = load_question_bank()
    questions = data["questions"]
    assert questions
    served = [q for q in questions if q.get("pool") != "heldout"]
    assert served
    # Every served item is a clean, resolvable, length-capped MCQ.
    for q in served:
        opts = curate._normalized_options(q["options"])
        assert len(opts) >= 2
        assert correct_index(str(q["correct"]), len(opts)) >= 0
        assert len(q["stem"]) <= curate.MAX_SERVED_STEM_CHARS


def test_import_question_bank_imports_curated_and_every_served_note_resolves():
    col = getEmptyCol()
    col.speedrun.setup_mcat()
    data = load_question_bank()
    summary = col.speedrun.import_question_bank(
        questions=data["questions"], attribution=data.get("attribution")
    )
    assert summary.imported == len(data["questions"])

    served = col.speedrun.served_question_note_ids()
    assert served
    taxonomy_slugs = {c["id"] for c in load_concepts()["concepts"]}
    concept_tagged = 0
    for nid in served:
        note = col.get_note(nid)
        assert POOL_SERVED_TAG in note.tags
        assert POOL_HELDOUT_TAG not in note.tags
        opts = option_lines(note["options"])
        # The correct answer resolves to a real option for every served note.
        assert correct_index(note["correct"], len(opts)) >= 0
        concept = concept_of_note(note)
        if concept:
            concept_tagged += 1
            assert concept in taxonomy_slugs
            assert any(t == f"{CONCEPT_TAG_PREFIX}{concept}" for t in note.tags)
    # A strong majority of served questions carry a taxonomy concept.
    assert concept_tagged / len(served) >= 0.75
