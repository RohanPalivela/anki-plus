# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Coverage contract for the hand-authored first-principles lesson cards.

The first-principles set is the *teaching* half of the Speedrun loop: every
concept in the canonical taxonomy (``speedrun_concepts.json``) must have at
least one lesson card, and every card must anchor to a real taxonomy concept
whose topic it matches. Those two invariants keep the ``gates::`` linkage
(which prefers an exact ``concept::`` match) landing precisely and guarantee no
concept is left without a lesson to activate on a miss.
"""

from __future__ import annotations

from collections import Counter

from anki.speedrun import load_concepts, load_first_principles


def _concept_topics() -> dict[str, str]:
    return {c["id"]: c["topic"] for c in load_concepts()["concepts"]}


def test_every_taxonomy_concept_has_a_first_principles_card():
    """Coverage: each of the taxonomy's concepts has >=1 lesson card."""
    concept_topics = _concept_topics()
    covered = {card["concept"] for card in load_first_principles()["cards"]}
    missing = sorted(set(concept_topics) - covered)
    assert not missing, f"concepts with no first-principles card: {missing}"


def test_every_card_concept_is_in_taxonomy_with_matching_topic():
    """Anchor integrity: a card's concept must exist in the taxonomy and its
    topic must equal that concept's topic (else gates:: linkage misfires)."""
    concept_topics = _concept_topics()
    for card in load_first_principles()["cards"]:
        concept = card["concept"]
        assert concept in concept_topics, f"off-taxonomy concept: {concept}"
        assert card["topic"] == concept_topics[concept], (
            f"{card['uid']}: topic {card['topic']!r} != "
            f"taxonomy topic {concept_topics[concept]!r} for {concept!r}"
        )


def test_first_principles_cards_are_well_formed_and_unique():
    """Every card is a distinct, correctly-shaped lesson item."""
    cards = load_first_principles()["cards"]
    uids: set[str] = set()
    for card in cards:
        uid = card["uid"]
        assert uid and uid not in uids, f"duplicate/empty uid: {uid!r}"
        uids.add(uid)
        assert card["front"].startswith("First principle:"), uid
        assert card["back"].strip(), uid


def test_high_yield_topics_get_extra_lesson_depth():
    """Biochemistry and organic chemistry are high-weight but data-starved, so
    the lesson cards carry the teaching load: they must average more than one
    card per concept (i.e. genuine depth, not bare coverage)."""
    concept_topics = _concept_topics()
    cards = load_first_principles()["cards"]
    cards_per_topic = Counter(card["topic"] for card in cards)
    concepts_per_topic = Counter(concept_topics.values())
    for topic in ("biochemistry", "organic-chemistry"):
        assert cards_per_topic[topic] > concepts_per_topic[topic], topic
