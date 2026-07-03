# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun (MCAT fork) helpers.

Two responsibilities live here:

1. Thin wrappers over the Rust ``SpeedrunService`` RPCs, plus the minimal
   miss-reason flow: classify a missed question, persist the latest reason as a
   ``miss::<reason>`` note tag (D-11; never ``card.custom_data``), and call
   gated activation.
2. Desktop data-model provisioning (D-13): idempotently create the
   ``SpeedrunQuestion`` notetype, the ``Speedrun::Questions`` deck, and a
   starter MCAT ``speedrunBlueprint`` config. The engine's activation/mastery
   logic is *not* re-implemented here — we only create native objects and call
   the frozen RPCs.
3. Real question-bank import (D-4): a one-time, idempotent import of a vendored,
   legally reusable MCAT-relevant question bank (see ``import_question_bank``)
   into native ``SpeedrunQuestion`` notes. Because they are native objects, a
   single desktop import syncs to Android/other devices for free (D-2). The
   imported bank is the *only* source of served practice questions — there is
   no synthetic/demo content, and the study loop is gated on having imported it.
4. First-principles memory cards (D-2a): the questions only *grade*; they gate
   the activation of linked memory flashcards. Those are original, hand-authored
   first-principles cards (see ``import_first_principles``) that teach the
   concept a family of questions tests — never a restatement of a specific bank
   item, and not part of the served/heldout question set. They link to questions
   by a shared ``topic::`` tag and are imported suspended until a related miss
   activates them.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anki
import anki.collection
from anki import speedrun_pb2
from anki.consts import QUEUE_TYPE_SUSPENDED
from anki.notes import Note, NoteId

if TYPE_CHECKING:
    from anki.decks import DeckId
    from anki.models import NotetypeId

# public exports
MissReason = speedrun_pb2.MissReason
ActivateCardsResponse = speedrun_pb2.ActivateCardsResponse
MemoryScoreResponse = speedrun_pb2.MemoryScoreResponse
PerformanceScoreResponse = speedrun_pb2.PerformanceScoreResponse
ReadinessScoreResponse = speedrun_pb2.ReadinessScoreResponse
SeedSyntheticResponsesResponse = speedrun_pb2.SeedSyntheticResponsesResponse

# Tag prefix for the latest miss reason on a question note (D-11).
MISS_TAG_PREFIX = "miss::"

# Map each MissReason to the tag suffix used by the SPOV3 taxonomy.
_MISS_REASON_TAG_SUFFIX: dict[int, str] = {
    MissReason.KNOWLEDGE_GAP: "knowledge-gap",
    MissReason.MISSING_CONTEXT: "missing-context",
    MissReason.MISUNDERSTANDING: "misunderstanding",
    MissReason.CARELESS: "careless",
}

# --- Native data model (T1 / D-13) -------------------------------------------

#: The runtime-provisioned question notetype. Provisioned via a helper rather
#: than as a stock notetype to keep the fork mergeable (D-13).
QUESTION_NOTETYPE_NAME = "SpeedrunQuestion"
#: Field order is part of the frozen data-model contract (C1).
QUESTION_FIELDS = (
    "stem",
    "options",
    "correct",
    "explanation",
    "source",
    "difficulty_b",
    "discrimination_a",
)
#: Served practice questions live in their own deck.
QUESTIONS_DECK_NAME = "Speedrun::Questions"
#: Linked flashcards (normal notes, suspended by default) live here.
FLASHCARDS_DECK_NAME = "Speedrun::Cards"

# Tag taxonomy (must match the Rust engine's constants).
TOPIC_TAG_PREFIX = "topic::"
POOL_SERVED_TAG = "pool::served"
POOL_HELDOUT_TAG = "pool::heldout"
#: Precise per-question link to a specific first-principles note it depends on
#: (must match ``rslib/src/speedrun/mod.rs``'s ``GATES_TAG_PREFIX``). The Rust
#: engine prefers these over the coarse ``topic::`` link when present. Because
#: the value is a *note id* — only assigned when the first-principles cards are
#: imported into a collection — these tags cannot be shipped in the static
#: vendored data; they are computed by :meth:`Speedrun.link_gated_first_principles`
#: at import time (and back-filled for older collections).
GATES_TAG_PREFIX = "gates::"
#: Legacy marker tag from the old synthetic demo dataset. No new demo content is
#: ever created; this is kept only so :meth:`Speedrun.purge_demo_data` can find
#: and delete any synthetic notes left in a collection provisioned by an older
#: build (D-10 / D-16: synthetic placeholders must never masquerade as content).
LEGACY_DEMO_TAG = "speedrun-demo"

# Real question-bank import (D-4 / D-16) ---------------------------------------
#
# Unlike the synthetic demo above, this imports a *real*, legally reusable bank
# of MCAT-relevant questions (OpenMCAT + MMLU; see the repo README for full
# attribution) as native ``SpeedrunQuestion`` notes. Because questions are
# native Anki objects, a single desktop import syncs to every device for free —
# no per-device re-import, no side tables (D-2). The bank is regenerated by
# ``tools/speedrun/build_question_bank.py`` and vendored as JSON alongside this
# module so import is deterministic and works offline.

#: Marker tag on every imported bank note (find/remove/report the whole bank).
BANK_TAG = "speedrun-bank"
#: Per-note stable id tag, used to make re-import idempotent (never duplicates).
BANK_UID_TAG_PREFIX = "bankuid::"
#: Per-note origin tag, e.g. ``bank::openmcat`` / ``bank::mmlu``.
BANK_SOURCE_TAG_PREFIX = "bank::"
#: Tag marking third-party AI-generated items so the M2 eval harness can find
#: and gate them (D-9); they are honestly labelled rather than hidden.
BANK_AI_GENERATED_TAG = "bank::ai-generated"
#: Vendored, pre-normalized bank shipped next to this module (resolves in dev,
#: tests, and the wheel via the package dir). Gzipped so the repo carries a
#: single small binary blob instead of a huge JSON text diff.
DEFAULT_BANK_PATH = Path(__file__).parent / "data" / "speedrun_question_bank.json.gz"


def load_question_bank(path: str | Path | None = None) -> dict[str, Any]:
    """Load and parse the vendored question bank JSON (gzip or plain).

    Kept module-level so tests and tooling can read the bank without a
    collection. ``path`` defaults to the vendored gzipped bank.
    """
    bank_path = Path(path) if path is not None else DEFAULT_BANK_PATH
    if bank_path.suffix == ".gz":
        with gzip.open(bank_path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(bank_path, encoding="utf-8") as fh:
        return json.load(fh)


# First-principles memory cards (D-2a linked flashcards) -----------------------
#
# The imported questions only *grade* the student; they gate the activation of
# linked *memory flashcards*. Those cards are original, hand-authored MCAT
# first-principles items (vendored next to this module) that teach the concept a
# family of questions tests — they deliberately never restate a specific bank
# question and are not part of the served/heldout question set. They link to
# questions purely by a shared ``topic::`` tag, are imported as *suspended*
# Basic notes into ``Speedrun::Cards``, and are unsuspended only when a related
# question is missed for a memory reason (or by a coverage sweep).

#: Notetype used for the linked memory cards (stock two-field Basic).
FLASHCARD_NOTETYPE_NAME = "Basic"
#: Marker tag on every first-principles card (find/remove/report the whole set).
FIRST_PRINCIPLES_TAG = "speedrun-first-principles"
#: Per-card stable id tag, used to make re-import idempotent (never duplicates).
FIRST_PRINCIPLES_UID_TAG_PREFIX = "fpuid::"
#: Fine-grained concept tag (e.g. ``concept::electrochemistry``) for auditing.
CONCEPT_TAG_PREFIX = "concept::"
#: Vendored, hand-authored first-principles dataset shipped next to this module.
DEFAULT_FIRST_PRINCIPLES_PATH = (
    Path(__file__).parent / "data" / "speedrun_first_principles.json"
)


def load_first_principles(path: str | Path | None = None) -> dict[str, Any]:
    """Load and parse the vendored first-principles card dataset.

    Kept module-level so tests and tooling can read the set without a
    collection. ``path`` defaults to the vendored JSON.
    """
    fp_path = Path(path) if path is not None else DEFAULT_FIRST_PRINCIPLES_PATH
    with open(fp_path, encoding="utf-8") as fh:
        return json.load(fh)


# Canonical concept taxonomy (curriculum contract) ----------------------------
#
# The machine-readable list of fine-grained MCAT concepts (kebab-case slugs)
# each curated served question maps to via its ``concept::`` tag. It is the
# shared vocabulary other layers (coverage, gating, scoring) consume. Authored
# and regenerated by ``tools/speedrun/curate.py`` (source: its CONCEPT_TAXONOMY)
# and vendored next to this module so it ships in the wheel and resolves
# offline. Slugs are stable; only additive changes are expected.

#: Vendored concept taxonomy shipped next to this module.
DEFAULT_CONCEPTS_PATH = Path(__file__).parent / "data" / "speedrun_concepts.json"


def load_concepts(path: str | Path | None = None) -> dict[str, Any]:
    """Load and parse the vendored concept taxonomy JSON.

    Mirrors :func:`load_first_principles`: kept module-level so tests and
    tooling can read the taxonomy without a collection. ``path`` defaults to the
    vendored taxonomy.
    """
    concepts_path = Path(path) if path is not None else DEFAULT_CONCEPTS_PATH
    with open(concepts_path, encoding="utf-8") as fh:
        return json.load(fh)


def concepts_for_topic(topic: str, path: str | Path | None = None) -> list[str]:
    """Ordered concept slugs belonging to ``topic`` in the vendored taxonomy."""
    data = load_concepts(path)
    return [c["id"] for c in data.get("concepts", []) if c.get("topic") == topic]


def concept_labels(path: str | Path | None = None) -> dict[str, str]:
    """Map each taxonomy concept slug to its human-readable label.

    Used by the curriculum layer to give desktop nice curated labels; Android
    (which does not ship the taxonomy JSON) humanises the slug instead. Only the
    *label* differs between clients — every curriculum stat and the concept /
    topic grouping are computed identically from synced data, so parity holds.
    """
    return {
        c["id"]: c.get("label", c["id"])
        for c in load_concepts(path).get("concepts", [])
    }


def humanize_slug(slug: str) -> str:
    """Fallback label for a kebab-case slug (``amino-acids`` -> ``Amino acids``).

    Deliberately matches Android's ``Speedrun.humanizeSlug`` so a concept with no
    curated taxonomy label reads the same on both clients.
    """
    words = slug.replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else words


# Config keys (must match rslib/src/speedrun/blueprint.rs + config/bool.rs).
BLUEPRINT_CONFIG_KEY = "speedrunBlueprint"
SWEEP_SAMPLE_SIZE_CONFIG_KEY = "speedrunSweepSampleSize"
ORDERING_CONFIG_KEY = "speedrunOrdering"
#: Persisted paused guided-session progress (written by ``aqt.speedrun.session``).
#: Defined here so both the session controller and :meth:`Speedrun.reset_profile`
#: refer to the same key.
SESSION_STATE_CONFIG_KEY = "speedrunSessionState"
#: Requested scope for the NEXT fresh guided session, set by the curriculum home
#: when the student taps a topic/concept and consumed (then cleared) by the
#: session controller. Shape: ``{"topic": str|None, "concept": str|None}``; an
#: absent/empty value means the top-level smart Start (targets weak concepts).
#: Mirrored across clients so a concept tapped on either device scopes the same
#: session; kept transient (never resumes) so it can't strand a stale scope.
SESSION_SCOPE_CONFIG_KEY = "speedrunSessionScope"
#: Set once :meth:`Speedrun.link_gated_first_principles` has stamped precise
#: ``gates::`` links onto a collection's served questions, so the study/session
#: entry points can back-fill legacy collections exactly once instead of
#: re-scanning the whole served pool on every launch.
GATES_LINKED_CONFIG_KEY = "speedrunGatesLinked"

# Guided-session per-phase caps (Tier 2). Tunable via collection config so the
# fixed sequence never floods the student (adaptive/capped sizing). Defaults are
# deliberately small to avoid cognitive overload.
SESSION_PRACTICE_CAP_CONFIG_KEY = "speedrunSessionPracticeCap"
SESSION_FLASHCARD_CAP_CONFIG_KEY = "speedrunSessionFlashcardCap"
SESSION_RECAP_CAP_CONFIG_KEY = "speedrunSessionRecapCap"
DEFAULT_SESSION_PRACTICE_CAP = 10
DEFAULT_SESSION_FLASHCARD_CAP = 20
DEFAULT_SESSION_RECAP_CAP = 5

#: Master switch for every AI feature (grounded card/question rephrasal). Stored
#: in the synced collection config and OFF by default: the app produces all three
#: scores with AI disabled (a hard Plan requirement), and AI only ever runs after
#: the student opts in. Synced so the choice carries across devices.
AI_ENABLED_CONFIG_KEY = "speedrunAiEnabled"

#: Starter MCAT blueprint: AAMC-style *topic labels and approximate relative
#: weights only*.
#:
#: LICENSING (D-3 / D-16): these are topic labels and approximate weights, NOT
#: copyrighted AAMC text or MCAT items. They are a placeholder for demoing
#: value-ordering / coverage / readiness and must be reviewed for licensing
#: before shipping. Weights are relative magnitudes (they happen to sum to 1.0).
DEFAULT_MCAT_BLUEPRINT: dict[str, Any] = {
    "topics": [
        {"name": "biochemistry", "weight": 0.25},
        {"name": "biology", "weight": 0.18},
        {"name": "general-chemistry", "weight": 0.13},
        {"name": "organic-chemistry", "weight": 0.10},
        {"name": "physics", "weight": 0.09},
        {"name": "psychology", "weight": 0.15},
        {"name": "sociology", "weight": 0.10},
    ]
}


@dataclass
class SessionCaps:
    """Per-phase question/card caps for a guided session (Tier 2)."""

    practice: int
    flashcards: int
    recap: int


@dataclass
class SetupSummary:
    """What ``setup_mcat`` provisioned, for surfacing in the UI."""

    notetype_id: int
    questions_deck_id: int
    flashcards_deck_id: int
    blueprint_topics: int
    #: True once the real question bank has been imported (served questions
    #: exist). Practice is gated on this being true.
    bank_imported: bool = False
    #: Number of served practice questions currently available.
    served_question_count: int = 0
    #: How many leftover synthetic demo notes were purged during setup.
    demo_notes_removed: int = 0


@dataclass
class BankImportSummary:
    """Outcome of importing the vendored question bank."""

    total_in_bank: int = 0
    imported: int = 0
    skipped_existing: int = 0
    by_origin: dict[str, int] = field(default_factory=dict)
    by_pool: dict[str, int] = field(default_factory=dict)
    by_topic: dict[str, int] = field(default_factory=dict)
    attribution: list[dict[str, str]] = field(default_factory=list)


@dataclass
class FirstPrinciplesImportSummary:
    """Outcome of importing the first-principles memory cards."""

    total: int = 0
    imported: int = 0
    skipped_existing: int = 0
    suspended: int = 0
    by_topic: dict[str, int] = field(default_factory=dict)


@dataclass
class GatesLinkSummary:
    """Outcome of stamping precise ``gates::`` links onto served questions."""

    #: Served questions considered.
    served_questions: int = 0
    #: Questions that received at least one ``gates::`` link.
    linked: int = 0
    #: Questions with no resolvable first-principles mapping (topic fallback).
    unlinked: int = 0
    #: Total ``gates::`` tags written across all questions.
    gates_written: int = 0
    #: Notes whose tag set actually changed (idempotent re-runs report 0).
    notes_updated: int = 0
    #: Questions linked by a high-confidence signal (concept or strong keyword
    #: overlap) to a specific card, rather than the whole-topic fallback.
    precise: int = 0
    #: Questions linked to every first-principles card of their topic because no
    #: finer signal was available (still exercises the precise path).
    topic_level: int = 0


@dataclass
class _FirstPrinciplesLink:
    """A first-principles note as a candidate activation target for linkage."""

    note_id: int
    concept: str | None
    keywords: set[str]


@dataclass
class ResetProfileSummary:
    """Outcome of resetting the learner's Speedrun progress.

    The imported question bank and memory cards are always kept; only the
    learner's *progress* (activation + FSRS review history + any paused session)
    is cleared.
    """

    #: Memory cards that were active (unsuspended) and got returned to suspended.
    cards_resuspended: int = 0
    #: Memory cards that had scheduling/FSRS history that was cleared.
    cards_forgotten: int = 0
    #: Whether a paused guided-session state was cleared.
    session_cleared: bool = False


def topic_of_note(note: Note) -> str | None:
    """Return the bare topic name from a note's first ``topic::`` tag, if any."""
    for tag in note.tags:
        if tag.startswith(TOPIC_TAG_PREFIX):
            return tag[len(TOPIC_TAG_PREFIX) :]
    return None


def topics_of_note(note: Note) -> list[str]:
    """All bare topic names from a note's ``topic::`` tags (usually one)."""
    return [
        t[len(TOPIC_TAG_PREFIX) :] for t in note.tags if t.startswith(TOPIC_TAG_PREFIX)
    ]


def concept_of_note(note: Note) -> str | None:
    """Return the bare concept from a note's first ``concept::`` tag, if any."""
    for tag in note.tags:
        if tag.startswith(CONCEPT_TAG_PREFIX):
            return tag[len(CONCEPT_TAG_PREFIX) :]
    return None


def concepts_of_note(note: Note) -> list[str]:
    """All bare concept names from a note's ``concept::`` tags (usually one).

    Mirrors :func:`topics_of_note`; used alongside :func:`concept_of_note` when
    scoping the guided-session recap to the concepts practised in Phase 1.
    """
    return [
        t[len(CONCEPT_TAG_PREFIX) :]
        for t in note.tags
        if t.startswith(CONCEPT_TAG_PREFIX)
    ]


#: Common English + MCAT-generic tokens that carry no discriminating signal for
#: question↔first-principles matching, so they are dropped before overlap
#: scoring (otherwise every biology item "matches" every biology card).
_LINK_STOPWORDS = frozenset(
    """
    the a an and or of to in on at for with without from by as is are was were be been
    being it its this that these those which who whom whose what when where why how
    into onto than then thus so if but not no nor can could should would may might must
    will shall do does did done has have had having each per both all any some more most
    less least such only also very much many few one two three four following about
    between within during above below over under out off up down same other another
    given shown best correct answer question choice choices option options statement
    describes following true false increases decreases increase decrease change
    """.split()
)


def link_keywords(text: str) -> set[str]:
    """Lowercase content tokens (len >= 3, non-stopword) used for the
    deterministic question↔first-principles overlap heuristic. Non-alphanumeric
    runs split tokens, so units and punctuation don't create noise."""
    tokens: set[str] = set()
    current: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            current.append(ch)
        elif current:
            tokens.add("".join(current))
            current = []
    if current:
        tokens.add("".join(current))
    return {t for t in tokens if len(t) >= 3 and t not in _LINK_STOPWORDS}


def _round_robin(groups: list[list[NoteId]]) -> list[NoteId]:
    """Interleave per-topic note-id lists so consecutive items differ in topic."""
    result: list[NoteId] = []
    cursors = [0] * len(groups)
    remaining = sum(len(g) for g in groups)
    while remaining:
        for i, group in enumerate(groups):
            if cursors[i] < len(group):
                result.append(group[cursors[i]])
                cursors[i] += 1
                remaining -= 1
    return result


def option_lines(options_field: str) -> list[str]:
    """Split a question's ``options`` field into trimmed, non-empty lines."""
    return [line.strip() for line in options_field.splitlines() if line.strip()]


def correct_index(correct_field: str, num_options: int) -> int:
    """Resolve a ``correct`` field to a 0-based option index, or -1 if invalid.

    Accepts a letter (``A``-``D``, case-insensitive) or a 1-based number.
    """
    text = correct_field.strip()
    if not text:
        return -1
    if text[0].isalpha():
        idx = ord(text[0].upper()) - ord("A")
    else:
        try:
            idx = int(text) - 1
        except ValueError:
            return -1
    return idx if 0 <= idx < num_options else -1


# Recap transfer scoring (scaffold — deferred; see M-future note) --------------
#
# The guided session's Recap phase re-tests the *same concepts* practised in
# Phase 1 with distinct questions ("same material, different phrasing"), so its
# accuracy is a transfer signal: did studying the linked memory cards make each
# concept stick on a *fresh* item? The session controller persists per-concept
# ``(answered, correct)`` tallies in its state; this pure function turns those
# tallies into a score.
#
# NOT SURFACED YET (by design): the user explicitly wants scoring deferred until
# the recap-selection / no-activation changes have bedded in, so nothing here is
# shown in the session summary or the home/dashboard. The current formula is a
# simple accuracy — the overall is micro-averaged across all answered recap
# items, and each concept reports its own ratio.
#
# FUTURE (M-future): weight each concept by its blueprint topic weight (and/or
# item difficulty_b / discrimination_a) so the overall transfer score reflects
# exam-blueprint importance rather than treating every concept equally. The
# per-concept breakdown already gives the surface the data it needs for that.


@dataclass
class ConceptRecapScore:
    """Recap accuracy for a single concept.

    The empty-string concept (``""``) is the topic-fallback bucket used for
    recap questions that carry no ``concept::`` tag.
    """

    concept: str
    answered: int
    correct: int

    @property
    def accuracy(self) -> float:
        """Fraction correct in [0, 1]; 0.0 when nothing was answered."""
        return self.correct / self.answered if self.answered else 0.0


@dataclass
class RecapScore:
    """Aggregate recap/transfer score plus its per-concept breakdown."""

    answered: int
    correct: int
    #: Micro-averaged overall accuracy in [0, 1]; 0.0 when nothing was answered.
    overall: float
    per_concept: list[ConceptRecapScore]


def compute_recap_score(
    answered_by_concept: dict[str, int],
    correct_by_concept: dict[str, int],
) -> RecapScore:
    """Pure transfer-score computation from per-concept recap tallies.

    ``answered_by_concept`` / ``correct_by_concept`` map a concept slug (the
    empty string ``""`` is the topic-fallback bucket for recap questions with no
    ``concept::`` tag) to how many recap questions of that concept were answered
    / answered correctly. Returns the overall micro-averaged accuracy and a
    stable, concept-sorted per-concept breakdown.

    Intentionally simple for now (see the module note above for the intended
    future blueprint-weighted formula) and kept pure — no collection, no I/O —
    so it is trivially unit-testable and reusable by any surface once recap
    scoring is actually shown.
    """
    per_concept = [
        ConceptRecapScore(
            concept=concept,
            answered=answered_by_concept.get(concept, 0),
            correct=correct_by_concept.get(concept, 0),
        )
        for concept in sorted(answered_by_concept)
    ]
    total_answered = sum(answered_by_concept.values())
    total_correct = sum(correct_by_concept.values())
    overall = total_correct / total_answered if total_answered else 0.0
    return RecapScore(
        answered=total_answered,
        correct=total_correct,
        overall=overall,
        per_concept=per_concept,
    )


# Curriculum data/API layer (W4 — concept-structured navigation) --------------
#
# The curriculum turns the flat "start -> random questions -> flashcards ->
# recap" pool into a navigable **topic -> concept** structure so the student
# sees where they are weak instead of an opaque bag of questions. It is built
# ONCE here (and mirrored in Android's ``Speedrun.kt``) entirely from data that
# already syncs — no new notetype, deck, or persisted schema:
#
# * topics + weights come from the ``speedrunBlueprint`` collection config;
# * a concept belongs to a topic purely by content: the ``topic::`` /
#   ``concept::`` tags co-occurring on served questions and first-principles
#   cards (so both clients derive identical membership offline);
# * per-concept progress is read from card state (reps / suspension) and the
#   ``revlog`` (answered / correct on the concept's served-question cards);
# * per-topic mastery reuses the existing ``get_memory_score`` FSRS RPC.
#
# Concepts with neither a served question nor a lesson card are omitted (both
# clients can only see content that synced in). The recap SCORE stays hidden by
# design; curriculum progress/mastery is what we surface.

#: Ease values >= this are treated as a correct answer in the revlog. The study
#: loop grades Good(3)=correct / Again(1)=incorrect, so 2 is the natural cutoff.
_CORRECT_EASE_CUTOFF = 2


@dataclass
class ConceptProgress:
    """Per-concept curriculum stats (all derived from synced data)."""

    concept: str
    label: str
    topic: str
    #: Served practice questions tagged with this concept.
    served_questions: int
    #: First-principles lesson cards tagged with this concept.
    lesson_cards: int
    #: Revlog answers recorded on this concept's served-question cards.
    answered: int
    #: Of those answers, how many were correct (ease >= cutoff).
    correct: int
    #: Lesson cards currently unsuspended (activated by a related miss/sweep).
    lessons_activated: int
    #: Lesson cards that have at least one review (FSRS mastery is building).
    lessons_reviewed: int

    @property
    def accuracy(self) -> float:
        """Fraction of recorded answers that were correct; 0.0 when none."""
        return self.correct / self.answered if self.answered else 0.0

    @property
    def practiced(self) -> bool:
        """True once the student has answered any of this concept's questions."""
        return self.answered > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "label": self.label,
            "topic": self.topic,
            "servedQuestions": self.served_questions,
            "lessonCards": self.lesson_cards,
            "answered": self.answered,
            "correct": self.correct,
            "lessonsActivated": self.lessons_activated,
            "lessonsReviewed": self.lessons_reviewed,
            "accuracy": self.accuracy,
            "practiced": self.practiced,
        }


@dataclass
class TopicProgress:
    """A blueprint topic plus its concepts and aggregate progress."""

    topic: str
    label: str
    weight: float
    #: FSRS mastery point estimate from ``get_memory_score`` (0.0 if unknown).
    mastery: float
    #: False when the Memory model has no activated-card data for this topic.
    mastery_known: bool
    concepts: list[ConceptProgress]
    served_questions: int
    lesson_cards: int
    answered: int
    correct: int

    @property
    def accuracy(self) -> float:
        return self.correct / self.answered if self.answered else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "label": self.label,
            "weight": self.weight,
            "mastery": self.mastery,
            "masteryKnown": self.mastery_known,
            "servedQuestions": self.served_questions,
            "lessonCards": self.lesson_cards,
            "answered": self.answered,
            "correct": self.correct,
            "accuracy": self.accuracy,
            "concepts": [c.to_dict() for c in self.concepts],
        }


@dataclass
class Curriculum:
    """The full topic -> concept curriculum with progress and mastery."""

    topics: list[TopicProgress]
    #: Overall FSRS mastery point estimate from ``get_memory_score``.
    overall_mastery: float
    #: True when the Memory model abstains (too little graded data).
    mastery_abstained: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "topics": [t.to_dict() for t in self.topics],
            "overallMastery": self.overall_mastery,
            "masteryAbstained": self.mastery_abstained,
        }

    def concept(self, slug: str) -> ConceptProgress | None:
        for topic in self.topics:
            for concept in topic.concepts:
                if concept.concept == slug:
                    return concept
        return None


class Speedrun:
    def __init__(self, col: anki.collection.Collection) -> None:
        self.col = col.weakref()

    # Gating engine RPCs (Rust-implemented)
    ##########################################################################

    def activate_cards_for_miss(
        self, question_note_id: int, miss_reason: MissReason.V
    ) -> ActivateCardsResponse:
        """Unsuspend a missed question's linked cards iff the reason qualifies
        (KNOWLEDGE_GAP / MISSING_CONTEXT); a no-op otherwise."""
        return self.col._backend.activate_cards_for_miss(
            question_note_id=question_note_id, miss_reason=miss_reason
        )

    def run_coverage_sweep(self, sample_size: int = 0) -> ActivateCardsResponse:
        """Re-activate a spread of cards across all blueprint topics. A
        sample_size of 0 uses the configured default (never "sweep nothing")."""
        return self.col._backend.run_coverage_sweep(sample_size=sample_size)

    def get_memory_score(self) -> MemoryScoreResponse:
        """Per-topic FSRS mastery + overall point estimate, range, coverage, and
        an explicit abstention flag."""
        return self.col._backend.get_memory_score()

    def get_performance_score(self) -> PerformanceScoreResponse:
        """2PL-IRT P(correct on a representative new question): point estimate,
        range (from the ability standard error), coverage, per-topic breakdown,
        the fitted ability (theta) + its SE, an abstention flag, and a
        ``synthetic`` flag when synthetic seed data is included."""
        return self.col._backend.get_performance_score()

    def get_readiness_score(self) -> ReadinessScoreResponse:
        """Monte-Carlo projected MCAT scaled score (472–528): median + 80%
        interval, coverage, confidence, graded count, abstention, top reasons,
        and a ``synthetic`` flag when synthetic seed data is included."""
        return self.col._backend.get_readiness_score()

    def seed_synthetic_responses(
        self,
        *,
        responses_per_question: int = 0,
        true_theta: float = 0.0,
        seed: int = 0,
    ) -> SeedSyntheticResponsesResponse:
        """DEV/TEST ONLY: fabricate deterministic synthetic practice responses so
        the Performance/Readiness models can be exercised on a collection with no
        real history (e.g. to demonstrate "beats chance").

        This is gated, never silent: it flips a collection flag so every surfaced
        Performance/Readiness score is labelled ``synthetic`` on all clients. It
        is atomic + undoable and only adds ``revlog`` rows — it never alters FSRS
        state, scheduling, or content. Zero arguments use the engine defaults
        (still deterministic). Do not call this in a normal user's flow.
        """
        return self.col._backend.seed_synthetic_responses(
            responses_per_question=responses_per_question,
            true_theta=true_theta,
            seed=seed,
        )

    # AI features (opt-in, synced toggle)
    ##########################################################################

    def ai_enabled(self) -> bool:
        """Whether the student has opted into AI features (default False).

        The score models never depend on this — the app produces Memory,
        Performance, and Readiness with AI off. It only gates grounded card /
        question rephrasal (see :mod:`anki.speedrun_rephrase`)."""
        return bool(self.col.get_config(AI_ENABLED_CONFIG_KEY, False))

    def set_ai_enabled(self, enabled: bool) -> None:
        """Persist the AI opt-in in the synced collection config."""
        self.col.set_config(AI_ENABLED_CONFIG_KEY, bool(enabled))

    def first_principles_note_ids(self) -> list[int]:
        """Note ids of the imported first-principles memory cards (rephrasal
        sources), excluding any that are themselves AI-generated variants."""
        return [
            int(nid)
            for nid in self.col.find_notes(
                f"tag:{FIRST_PRINCIPLES_TAG} -tag:{BANK_AI_GENERATED_TAG}"
            )
        ]

    # Miss-reason flow
    ##########################################################################

    def record_miss_reason(
        self, question_note_id: int, miss_reason: MissReason.V
    ) -> ActivateCardsResponse:
        """Persist the latest miss reason as a `miss::<reason>` note tag,
        replacing any prior one (D-11), then run gated activation.

        Attempt correctness/time history already lives natively in `revlog`, so
        only the latest reason needs a home; `card.custom_data` is deliberately
        not used.
        """
        suffix = _MISS_REASON_TAG_SUFFIX.get(miss_reason)
        if suffix is not None:
            note = self.col.get_note(NoteId(question_note_id))
            # Keep only the new miss reason: drop any prior miss::* tag, then add.
            note.tags = [t for t in note.tags if not t.startswith(MISS_TAG_PREFIX)]
            note.tags.append(f"{MISS_TAG_PREFIX}{suffix}")
            self.col.update_note(note)
        return self.activate_cards_for_miss(question_note_id, miss_reason)

    # Data-model provisioning (T1 / D-13)
    ##########################################################################

    def setup_mcat(
        self,
        *,
        enable_value_ordering: bool = True,
    ) -> SetupSummary:
        """Idempotently provision the Speedrun data model for MCAT.

        Creates the ``SpeedrunQuestion`` notetype, the ``Speedrun::Questions``
        and ``Speedrun::Cards`` decks, and the starter blueprint config; turns
        on value ordering; and purges any leftover synthetic demo data from an
        older build. Real practice questions come exclusively from
        :meth:`import_question_bank` — setup never creates placeholder content.

        Re-running is safe: existing objects are reused.
        """
        notetype_id = self.ensure_question_notetype()
        questions_deck, flashcards_deck = self.ensure_decks()
        blueprint = self.ensure_blueprint()

        if self.col.get_config(SWEEP_SAMPLE_SIZE_CONFIG_KEY) is None:
            self.col.set_config(SWEEP_SAMPLE_SIZE_CONFIG_KEY, 2)
        for key, default in (
            (SESSION_PRACTICE_CAP_CONFIG_KEY, DEFAULT_SESSION_PRACTICE_CAP),
            (SESSION_FLASHCARD_CAP_CONFIG_KEY, DEFAULT_SESSION_FLASHCARD_CAP),
            (SESSION_RECAP_CAP_CONFIG_KEY, DEFAULT_SESSION_RECAP_CAP),
        ):
            if self.col.get_config(key) is None:
                self.col.set_config(key, default)
        if enable_value_ordering:
            # Opt into value = topic_weight x weakness ordering of activated cards.
            self.col.set_config(ORDERING_CONFIG_KEY, True)

        demo_removed = self.purge_demo_data()

        return SetupSummary(
            notetype_id=int(notetype_id),
            questions_deck_id=int(questions_deck),
            flashcards_deck_id=int(flashcards_deck),
            blueprint_topics=len(blueprint.get("topics", [])),
            bank_imported=self.has_question_bank(),
            served_question_count=len(self.served_question_note_ids()),
            demo_notes_removed=demo_removed,
        )

    def has_question_bank(self) -> bool:
        """True once the real question bank has been imported (served questions
        exist). The study loop is gated on this so students never practise
        against an empty or synthetic pool."""
        return bool(self.col.find_notes(f"tag:{BANK_TAG} tag:{POOL_SERVED_TAG}"))

    def purge_demo_data(self) -> int:
        """Delete every leftover synthetic demo note (tagged ``LEGACY_DEMO_TAG``)
        and return how many were removed.

        No synthetic content is created anymore; this only cleans up collections
        that were provisioned by an older build so the study loop serves real
        imported questions exclusively. Idempotent and cheap when nothing
        matches.
        """
        demo_notes = list(self.col.find_notes(f"tag:{LEGACY_DEMO_TAG}"))
        if demo_notes:
            self.col.remove_notes(demo_notes)
        return len(demo_notes)

    def import_question_bank(
        self,
        path: str | Path | None = None,
        *,
        questions: list[dict[str, Any]] | None = None,
        attribution: list[dict[str, str]] | None = None,
    ) -> BankImportSummary:
        """Import the vendored real question bank as native ``SpeedrunQuestion``
        notes, so a single desktop import syncs to every device (D-2/D-4).

        Idempotent: each item carries a stable ``bankuid::`` tag, so re-running —
        or running after the bank has already synced from another device — never
        duplicates. Pass ``questions`` to import an in-memory list (used by
        tests); otherwise the JSON at ``path`` (default: the vendored bank) is
        read.
        """
        if questions is None:
            data = load_question_bank(path)
            questions = data.get("questions", [])
            if attribution is None:
                attribution = data.get("attribution", [])

        notetype_id = self.ensure_question_notetype()
        questions_deck, _ = self.ensure_decks()
        notetype = self.col.models.get(notetype_id)
        assert notetype is not None

        existing = self._existing_bank_uids()
        summary = BankImportSummary(
            total_in_bank=len(questions), attribution=attribution or []
        )
        for item in questions:
            uid = str(item.get("uid", "")).strip()
            if not uid or uid in existing:
                summary.skipped_existing += 1
                continue
            note = self._new_bank_note(notetype, item, uid)
            self.col.add_note(note, questions_deck)
            existing.add(uid)
            summary.imported += 1
            origin = str(item.get("origin", "unknown"))
            pool = "heldout" if item.get("pool") == "heldout" else "served"
            summary.by_origin[origin] = summary.by_origin.get(origin, 0) + 1
            summary.by_pool[pool] = summary.by_pool.get(pool, 0) + 1
            for topic in item.get("topics", []):
                summary.by_topic[topic] = summary.by_topic.get(topic, 0) + 1
        # Refresh precise gates:: links now that the served pool may have grown
        # (no-op if the first-principles cards aren't imported yet).
        self.link_gated_first_principles()
        return summary

    def import_first_principles(
        self,
        path: str | Path | None = None,
        *,
        cards: list[dict[str, Any]] | None = None,
        suspend: bool = True,
    ) -> FirstPrinciplesImportSummary:
        """Import the vendored first-principles memory cards as native, suspended
        ``Basic`` notes in ``Speedrun::Cards`` (D-2a linked flashcards).

        These are the activation *targets*: linked to questions by a shared
        ``topic::`` tag, they stay suspended until a related question is missed
        for a memory reason. Idempotent via a per-card ``fpuid::`` tag, so
        re-running — or running after they have synced from another device —
        never duplicates. Pass ``cards`` to import an in-memory list (tests);
        otherwise the JSON at ``path`` (default: the vendored set) is read.
        """
        if cards is None:
            cards = load_first_principles(path).get("cards", [])

        _, flashcards_deck = self.ensure_decks()
        basic = self.col.models.by_name(FLASHCARD_NOTETYPE_NAME)
        assert basic is not None, "stock Basic notetype must exist"

        existing = self._existing_first_principles_uids()
        summary = FirstPrinciplesImportSummary(total=len(cards))
        new_card_ids: list[Any] = []
        for item in cards:
            uid = str(item.get("uid", "")).strip()
            if not uid or uid in existing:
                summary.skipped_existing += 1
                continue
            note = self.col.new_note(basic)
            note["Front"] = str(item.get("front", ""))
            note["Back"] = str(item.get("back", ""))
            topic = str(item.get("topic", "")).strip()
            tags = [FIRST_PRINCIPLES_TAG, f"{FIRST_PRINCIPLES_UID_TAG_PREFIX}{uid}"]
            if topic:
                tags.append(f"{TOPIC_TAG_PREFIX}{topic}")
            if concept := str(item.get("concept", "")).strip():
                tags.append(f"{CONCEPT_TAG_PREFIX}{concept}")
            note.tags = tags
            self.col.add_note(note, flashcards_deck)
            existing.add(uid)
            summary.imported += 1
            if topic:
                summary.by_topic[topic] = summary.by_topic.get(topic, 0) + 1
            new_card_ids.extend(c.id for c in note.cards())

        if suspend and new_card_ids:
            # Suspend so they are inert until a missed question activates them.
            self.col.sched.suspend_cards(new_card_ids)
            summary.suspended = len(new_card_ids)
        # These cards are the activation targets, so (re)compute the precise
        # gates:: links from the already-imported served questions to them.
        self.link_gated_first_principles()
        return summary

    def has_first_principles(self) -> bool:
        """True if any first-principles memory cards have been imported."""
        return bool(self.col.find_notes(f"tag:{FIRST_PRINCIPLES_TAG}"))

    # Precise question -> first-principles linkage (gates::) --------------------
    #
    # ``gates::<note_id>`` makes a missed question activate exactly the memory
    # card(s) it depends on instead of every card in its (coarse) topic. Because
    # the value is a runtime note id, it can't ship in the vendored data — it is
    # computed here, at import time, from signals already present in the content:
    # the shared ``topic::`` scope, then (within a topic) an exact ``concept::``
    # match, the concept name appearing in the question, or keyword overlap
    # between the question (stem+options+explanation) and the card text. When no
    # finer signal exists the question is linked to all of its topic's cards, so
    # the precise path is still exercised; a question with no topic card at all
    # keeps relying on the Rust topic fallback (never a broken activation).

    #: Minimum shared content keywords for a "precise" keyword-overlap link.
    #: Below this, we don't trust a single/­double common word to name the one
    #: right card, and fall back to gating the whole topic.
    _GATES_MIN_KEYWORD_OVERLAP = 3

    def ensure_gates_linked(self) -> GatesLinkSummary | None:
        """Back-fill ``gates::`` links once per collection (idempotent no-op
        afterwards). Cheap to call on every study/session start: returns
        ``None`` without scanning once the one-time link has run."""
        if self.col.get_config(GATES_LINKED_CONFIG_KEY):
            return None
        return self.link_gated_first_principles()

    def link_gated_first_principles(self) -> GatesLinkSummary:
        """(Re)compute and persist precise ``gates::`` links from every served
        question to the specific first-principles card(s) it depends on.

        Idempotent: existing ``gates::`` tags are recomputed and only notes whose
        tag set actually changes are written, so re-import / re-run never
        duplicates. Degrades gracefully: a question with no first-principles card
        in its topic is left without ``gates::`` and keeps using the topic
        fallback in the Rust engine.

        Note: this loads each served question to read its fields/tags. It runs at
        import time (behind the import progress UI) or once as a back-fill, not
        in the hot study loop, so the linear scan is acceptable for the
        tens-to-thousands-item served pool.
        """
        summary = GatesLinkSummary()
        fp_index = self._first_principles_index()
        served = self.served_question_note_ids()
        summary.served_questions = len(served)
        if not fp_index:
            # No activation targets yet (e.g. questions imported before the
            # first-principles cards). Don't mark done, so a later call — after
            # the cards are imported or synced in — links against them.
            return summary

        changed: list[Note] = []
        for nid in served:
            note = self.col.get_note(nid)
            chosen, precise = self._gates_for_question(note, fp_index)
            desired = sorted(set(chosen))
            existing = sorted(
                int(t[len(GATES_TAG_PREFIX) :])
                for t in note.tags
                if t.startswith(GATES_TAG_PREFIX)
                and t[len(GATES_TAG_PREFIX) :].isdigit()
            )
            if desired != existing:
                note.tags = [t for t in note.tags if not t.startswith(GATES_TAG_PREFIX)]
                note.tags.extend(f"{GATES_TAG_PREFIX}{cid}" for cid in desired)
                changed.append(note)
            if desired:
                summary.linked += 1
                summary.gates_written += len(desired)
                if precise:
                    summary.precise += 1
                else:
                    summary.topic_level += 1
            else:
                summary.unlinked += 1

        if changed:
            self.col.update_notes(changed)
            summary.notes_updated = len(changed)
        self.col.set_config(GATES_LINKED_CONFIG_KEY, True)
        return summary

    def _first_principles_index(self) -> dict[str, list[_FirstPrinciplesLink]]:
        """Bare topic name -> the first-principles cards tagged with it, each
        carrying its concept and content keywords for matching."""
        index: dict[str, list[_FirstPrinciplesLink]] = {}
        for nid in self.col.find_notes(f"tag:{FIRST_PRINCIPLES_TAG}"):
            note = self.col.get_note(nid)
            concept = concept_of_note(note)
            front = note["Front"] if "Front" in note else ""
            back = note["Back"] if "Back" in note else ""
            keywords = link_keywords(
                f"{front} {back} {(concept or '').replace('-', ' ')}"
            )
            entry = _FirstPrinciplesLink(int(nid), concept, keywords)
            for topic in topics_of_note(note):
                index.setdefault(topic, []).append(entry)
        return index

    def _gates_for_question(
        self, note: Note, fp_index: dict[str, list[_FirstPrinciplesLink]]
    ) -> tuple[list[int], bool]:
        """Return ``(first_principles_note_ids, precise)`` for one question.

        ``precise`` is True when a specific card was chosen by a concept or
        strong-keyword signal, False when the whole topic was gated as a
        fallback. An empty list means no first-principles card shares the
        question's topic (topic fallback in Rust handles it).
        """
        candidates: list[_FirstPrinciplesLink] = []
        seen: set[int] = set()
        for topic in topics_of_note(note):
            for fp in fp_index.get(topic, []):
                if fp.note_id not in seen:
                    seen.add(fp.note_id)
                    candidates.append(fp)
        if not candidates:
            return [], False

        # 1. Exact concept-tag match (highest confidence; used when a future
        #    bank tags questions with their fine-grained concept::).
        q_concept = concept_of_note(note)
        if q_concept:
            exact = [fp.note_id for fp in candidates if fp.concept == q_concept]
            if exact:
                return exact, True

        q_words = link_keywords(
            f"{note['stem']} {note['options']} {note['explanation']}"
        )

        # 2. The card's concept name appears (whole) in the question text.
        phrase = [
            fp.note_id
            for fp in candidates
            if fp.concept
            and (cw := link_keywords(fp.concept.replace("-", " ")))
            and cw <= q_words
        ]
        if phrase:
            return phrase, True

        # 3. Strongest content-keyword overlap, if it clears the threshold.
        scored = [(len(q_words & fp.keywords), fp.note_id) for fp in candidates]
        best = max(score for score, _ in scored)
        if best >= self._GATES_MIN_KEYWORD_OVERLAP:
            return [nid for score, nid in scored if score == best], True

        # 4. No finer signal: gate every card of the topic (safe superset that
        #    still lights up the precise path). Deterministic order.
        return sorted(fp.note_id for fp in candidates), False

    def reset_profile(self) -> ResetProfileSummary:
        """Reset the learner's Speedrun *progress*, keeping all imported content.

        Restores the pristine post-import state so activation and the Memory
        model start fresh, WITHOUT deleting the question bank or the memory
        cards. It:

        * clears any paused guided-session state, so a stale session does not
          resume;
        * forgets every first-principles memory card (``schedule_cards_as_new``),
          which clears their FSRS ``memory_state`` so ``graded_count`` returns to
          0 and they read as ungraded again;
        * re-suspends those cards, returning them to the inert state they start
          in (activation begins from scratch).

        Idempotent and safe on a collection with no Speedrun data.
        """
        summary = ResetProfileSummary()

        # 1. Drop any paused session so Start does not resume old progress.
        if self.col.get_config(SESSION_STATE_CONFIG_KEY, None) is not None:
            self.col.remove_config(SESSION_STATE_CONFIG_KEY)
            summary.session_cleared = True
        # Also drop any pending curriculum scope so a fresh Start is unscoped.
        if self.col.get_config(SESSION_SCOPE_CONFIG_KEY, None) is not None:
            self.col.remove_config(SESSION_SCOPE_CONFIG_KEY)

        # 2. Gather the memory cards and how much progress they carry (measured
        #    before mutating, so the reported counts are meaningful).
        all_ids = list(self.col.find_cards(f"tag:{FIRST_PRINCIPLES_TAG}"))
        if not all_ids:
            return summary
        summary.cards_resuspended = len(
            self.col.find_cards(f"tag:{FIRST_PRINCIPLES_TAG} -is:suspended")
        )
        summary.cards_forgotten = len(
            self.col.find_cards(f"tag:{FIRST_PRINCIPLES_TAG} -is:new")
        )

        # 3. Forget first (clears memory_state and unsuspends into the new
        #    queue), then re-suspend so they end inert with no FSRS history.
        self.col.sched.schedule_cards_as_new(all_ids)
        self.col.sched.suspend_cards(all_ids)
        return summary

    def _existing_first_principles_uids(self) -> set[str]:
        """Stable ids of already-imported first-principles cards (idempotency)."""
        prefix = FIRST_PRINCIPLES_UID_TAG_PREFIX
        return {
            tag[len(prefix) :] for tag in self.col.tags.all() if tag.startswith(prefix)
        }

    def _existing_bank_uids(self) -> set[str]:
        """Stable ids of already-imported bank notes (for idempotent import)."""
        prefix = BANK_UID_TAG_PREFIX
        return {
            tag[len(prefix) :] for tag in self.col.tags.all() if tag.startswith(prefix)
        }

    def _new_bank_note(self, notetype: Any, item: dict[str, Any], uid: str) -> Note:
        """Build (but don't add) a ``SpeedrunQuestion`` note from a bank item."""
        note = self.col.new_note(notetype)
        note["stem"] = str(item.get("stem", ""))
        # Collapse intra-option whitespace so each option stays on one line
        # (the study loop parses ``options`` line-by-line).
        note["options"] = "\n".join(
            " ".join(str(opt).split()) for opt in item.get("options", [])
        )
        note["correct"] = str(item.get("correct", ""))
        note["explanation"] = str(item.get("explanation", ""))
        note["source"] = str(item.get("source", ""))
        note["difficulty_b"] = f"{float(item.get('difficulty_b', 0.0)):.2f}"
        note["discrimination_a"] = f"{float(item.get('discrimination_a', 1.0)):.2f}"

        tags = [f"{TOPIC_TAG_PREFIX}{topic}" for topic in item.get("topics", [])]
        # A fine-grained concept (when the bank carries one) lets the linkage
        # pass make an exact question->first-principles match; harmless when
        # absent (older banks) — linkage then uses keyword/topic signals.
        if concept := str(item.get("concept", "")).strip():
            tags.append(f"{CONCEPT_TAG_PREFIX}{concept}")
        tags.append(
            POOL_HELDOUT_TAG if item.get("pool") == "heldout" else POOL_SERVED_TAG
        )
        tags.append(BANK_TAG)
        tags.append(f"{BANK_UID_TAG_PREFIX}{uid}")
        tags.append(f"{BANK_SOURCE_TAG_PREFIX}{item.get('origin', 'unknown')}")
        if item.get("ai_generated"):
            tags.append(BANK_AI_GENERATED_TAG)
        note.tags = tags
        return note

    def ensure_question_notetype(self) -> NotetypeId:
        """Return the ``SpeedrunQuestion`` notetype id, creating it if absent."""
        models = self.col.models
        existing = models.id_for_name(QUESTION_NOTETYPE_NAME)
        if existing is not None:
            return existing

        notetype = models.new(QUESTION_NOTETYPE_NAME)
        for field_name in QUESTION_FIELDS:
            models.add_field(notetype, models.new_field(field_name))
        template = models.new_template("Card 1")
        template["qfmt"] = "{{stem}}\n<hr id=options>\n{{options}}"
        template["afmt"] = (
            "{{FrontSide}}\n<hr id=answer>\n"
            "<b>Correct:</b> {{correct}}<br>\n{{explanation}}<br>\n"
            "<small>{{source}}</small>"
        )
        models.add_template(notetype, template)
        models.add_dict(notetype)

        created = models.id_for_name(QUESTION_NOTETYPE_NAME)
        assert created is not None
        return created

    def ensure_decks(self) -> tuple[DeckId, DeckId]:
        """Return (questions_deck_id, flashcards_deck_id), creating as needed."""
        questions = self.col.decks.id(QUESTIONS_DECK_NAME)
        flashcards = self.col.decks.id(FLASHCARDS_DECK_NAME)
        assert questions is not None and flashcards is not None
        return questions, flashcards

    def ensure_blueprint(
        self,
        blueprint: dict[str, Any] | None = None,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Persist the MCAT blueprint config (D-3). Existing config is kept
        unless ``overwrite`` is set; pass ``blueprint`` to override the default
        (D-3 allows a provided file)."""
        existing = self.col.get_config(BLUEPRINT_CONFIG_KEY)
        if existing and not overwrite:
            return existing
        chosen = blueprint if blueprint is not None else DEFAULT_MCAT_BLUEPRINT
        self.col.set_config(BLUEPRINT_CONFIG_KEY, chosen)
        return chosen

    # Question-first study loop support (M2 2b surface, minimal M1 slice)
    ##########################################################################

    def served_question_note_ids(self) -> list[NoteId]:
        """Note ids of served (never held-out) practice questions, in a stable
        creation-order list. Held-out questions are never returned.

        Ordering is intentionally the backend default (note id / creation order)
        rather than the browser's configured sort column: it keeps parity with
        the Android implementation and avoids depending on per-profile browser
        config, which is unset on freshly synced collections (previously this
        surfaced a spurious "None is not a valid sort order" and non-deterministic
        results). Callers that need interleaving apply it in
        ``served_questions_interleaved``."""
        query = (
            f"note:{QUESTION_NOTETYPE_NAME} tag:{POOL_SERVED_TAG} "
            f"-tag:{POOL_HELDOUT_TAG}"
        )
        return list(self.col.find_notes(query))

    def served_questions_interleaved(
        self,
        *,
        topics: set[str] | None = None,
        concepts: set[str] | None = None,
        exclude: set[int] | None = None,
        unseen_first: bool = False,
    ) -> list[NoteId]:
        """Served question note ids interleaved across topics (no topic blocking).

        ``topics`` restricts the result to those bare topic names; ``exclude``
        drops specific note ids (used to keep the recap set disjoint from
        Phase 1).

        ``concepts`` restricts to those fine-grained ``concept::`` slugs so the
        recap tests the *same material* practised in Phase 1 ("same concepts,
        different phrasing") rather than merely the same coarse topics. A
        question that carries a ``concept::`` tag is kept only when its concept
        is in ``concepts``; a question with **no** concept falls back to the
        ``topics`` scope (so recap is never empty when concepts are sparse — the
        bank only tags ~82% of served questions with a concept). ``concepts`` is
        ANDed with ``topics`` when both are given. NOTE: matching a *distinct*
        question of the same concept is today's best "same material, different
        phrasing"; true paraphrase VARIANTS (rewording the very same item) are a
        future content enhancement, not something the bank carries yet.

        ``unseen_first`` orders never-practised questions (their card has zero
        reps) ahead of already-practised ones so a capped batch (e.g. a guided
        session's Practice phase) never re-serves the same problems while fresh
        ones remain. Once the whole served pool has been practised, previously
        seen questions come back rotated oldest-review-first, so repeats are the
        least recently seen rather than always the same leading batch.

        Note: this loads each served note to read its ``topic::`` / ``concept::``
        tag (and, when ``unseen_first`` is set, its card's review state). The
        served pool is small (tens of items) so this is cheap; revisit if it
        grows.
        """
        exclude = exclude or set()

        def in_scope(nid: NoteId) -> str | None:
            """Return the bare topic (round-robin key) if ``nid`` is in scope,
            else ``None``."""
            if nid in exclude:
                return None
            note = self.col.get_note(nid)
            topic = topic_of_note(note) or ""
            if topics is not None and topic not in topics:
                return None
            if concepts is not None:
                concept = concept_of_note(note)
                # Concept-tagged questions must match a practised concept; a
                # question with no concept falls back to the topic scope above
                # so concept-scoped recap is never starved when tags are sparse.
                if concept is not None and concept not in concepts:
                    return None
            return topic

        if not unseen_first:
            groups: dict[str, list[NoteId]] = {}
            for nid in self.served_question_note_ids():
                topic = in_scope(nid)
                if topic is None:
                    continue
                groups.setdefault(topic, []).append(nid)
            return _round_robin(list(groups.values()))

        # Partition into never-practised vs. practised. Practised questions are
        # sorted oldest-review-first so, once the pool is exhausted, repeats
        # rotate rather than always replaying the same leading batch.
        unseen_groups: dict[str, list[NoteId]] = {}
        seen: list[tuple[int, str, NoteId]] = []
        for nid in self.served_question_note_ids():
            topic = in_scope(nid)
            if topic is None:
                continue
            cards = self.col.get_note(nid).cards()
            card = cards[0] if cards else None
            if card is None or card.reps == 0:
                unseen_groups.setdefault(topic, []).append(nid)
            else:
                seen.append((card.last_review_time or 0, topic, nid))

        result = _round_robin(list(unseen_groups.values()))
        seen.sort(key=lambda item: item[0])
        seen_groups: dict[str, list[NoteId]] = {}
        for _last_review, topic, nid in seen:
            seen_groups.setdefault(topic, []).append(nid)
        result.extend(_round_robin(list(seen_groups.values())))
        return result

    def session_caps(self) -> SessionCaps:
        """Per-phase caps for a guided session, from config (with defaults)."""

        def cap(key: str, default: int) -> int:
            value = self.col.get_config(key)
            try:
                value = int(value)
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        return SessionCaps(
            practice=cap(SESSION_PRACTICE_CAP_CONFIG_KEY, DEFAULT_SESSION_PRACTICE_CAP),
            flashcards=cap(
                SESSION_FLASHCARD_CAP_CONFIG_KEY, DEFAULT_SESSION_FLASHCARD_CAP
            ),
            recap=cap(SESSION_RECAP_CAP_CONFIG_KEY, DEFAULT_SESSION_RECAP_CAP),
        )

    # Curriculum data/API layer (W4)
    ##########################################################################

    def curriculum(self) -> Curriculum:
        """Build the topic -> concept curriculum with per-concept progress.

        Pure read: scans served questions + first-principles cards for their
        ``topic::`` / ``concept::`` tags, reads answered/correct from the
        ``revlog`` and lesson activation/review from card state, and folds in the
        per-topic FSRS mastery from :meth:`get_memory_score`. See the module
        note above for the design; mirrored byte-for-byte in Android's
        ``Speedrun.curriculum``.
        """
        labels = self._concept_labels_safe()

        # (topic, concept) -> served question count, and card id -> concept for
        # the single revlog query below. A concept is assigned the topic it
        # co-occurs with on content (derived identically on both clients).
        served_counts: dict[tuple[str, str], int] = {}
        concept_topic: dict[str, str] = {}
        card_concept: dict[int, str] = {}
        for nid in self.served_question_note_ids():
            note = self.col.get_note(nid)
            topic = topic_of_note(note) or ""
            concept = concept_of_note(note)
            if concept is None:
                continue
            self._assign_concept_topic(concept_topic, concept, topic)
            key = (topic, concept)
            served_counts[key] = served_counts.get(key, 0) + 1
            for card in note.cards():
                card_concept[int(card.id)] = concept

        # Lesson (first-principles) cards per concept, with activation/review.
        lesson_cards: dict[str, int] = {}
        lessons_activated: dict[str, int] = {}
        lessons_reviewed: dict[str, int] = {}
        for nid in self.col.find_notes(f"tag:{FIRST_PRINCIPLES_TAG}"):
            note = self.col.get_note(nid)
            concept = concept_of_note(note)
            if concept is None:
                continue
            topic = topic_of_note(note) or ""
            self._assign_concept_topic(concept_topic, concept, topic)
            for card in note.cards():
                lesson_cards[concept] = lesson_cards.get(concept, 0) + 1
                if card.queue != QUEUE_TYPE_SUSPENDED:
                    lessons_activated[concept] = lessons_activated.get(concept, 0) + 1
                if card.reps > 0:
                    lessons_reviewed[concept] = lessons_reviewed.get(concept, 0) + 1

        answered, correct = self._revlog_by_concept(card_concept)

        # Group concepts under their assigned topic.
        by_topic: dict[str, list[ConceptProgress]] = {}
        for concept, topic in concept_topic.items():
            served = served_counts.get((topic, concept), 0)
            # A concept may carry served questions under one topic tag but the
            # lesson card under another; served count keyed by (topic, concept)
            # handles the common case, and lesson counts are per-concept.
            if served == 0:
                served = sum(
                    count for (t, c), count in served_counts.items() if c == concept
                )
            cp = ConceptProgress(
                concept=concept,
                label=labels.get(concept) or humanize_slug(concept),
                topic=topic,
                served_questions=served,
                lesson_cards=lesson_cards.get(concept, 0),
                answered=answered.get(concept, 0),
                correct=correct.get(concept, 0),
                lessons_activated=lessons_activated.get(concept, 0),
                lessons_reviewed=lessons_reviewed.get(concept, 0),
            )
            by_topic.setdefault(topic, []).append(cp)

        memory = self.get_memory_score()
        topic_mastery = {t.topic: (t.mastery, t.known) for t in memory.topics}

        # Order: blueprint topics first (by descending weight, then name), then
        # any content-only topics; concepts sorted by slug for identical order
        # on both clients.
        blueprint = self.col.get_config(BLUEPRINT_CONFIG_KEY) or DEFAULT_MCAT_BLUEPRINT
        weights = {
            t["name"]: float(t.get("weight", 0.0)) for t in blueprint.get("topics", [])
        }
        ordered_topics = sorted(weights, key=lambda t: (-weights[t], t))
        for extra in sorted(by_topic):
            if extra not in weights:
                ordered_topics.append(extra)

        topics: list[TopicProgress] = []
        for topic in ordered_topics:
            concepts = sorted(by_topic.get(topic, []), key=lambda c: c.concept)
            if not concepts and topic not in weights:
                continue
            mastery, known = topic_mastery.get(topic, (0.0, False))
            topics.append(
                TopicProgress(
                    topic=topic,
                    label=humanize_slug(topic),
                    weight=weights.get(topic, 0.0),
                    mastery=float(mastery),
                    mastery_known=bool(known),
                    concepts=concepts,
                    served_questions=sum(c.served_questions for c in concepts),
                    lesson_cards=sum(c.lesson_cards for c in concepts),
                    answered=sum(c.answered for c in concepts),
                    correct=sum(c.correct for c in concepts),
                )
            )

        return Curriculum(
            topics=topics,
            overall_mastery=float(memory.overall),
            mastery_abstained=bool(memory.abstained),
        )

    @staticmethod
    def _assign_concept_topic(
        concept_topic: dict[str, str], concept: str, topic: str
    ) -> None:
        """Assign ``concept`` to ``topic`` deterministically (lexicographically
        smallest topic wins if a concept appears under several), so both clients
        group identically regardless of scan order."""
        existing = concept_topic.get(concept)
        if existing is None or topic < existing:
            concept_topic[concept] = topic

    def _revlog_by_concept(
        self, card_concept: dict[int, str]
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Answered / correct tallies per concept from the served-question
        cards' revlog (one query). Correct == ease >= the study-loop cutoff."""
        answered: dict[str, int] = {}
        correct: dict[str, int] = {}
        if not card_concept:
            return answered, correct
        ids = ",".join(str(cid) for cid in card_concept)
        for cid, ease in self.col.db.all(
            f"select cid, ease from revlog where cid in ({ids})"
        ):
            concept = card_concept.get(int(cid))
            if concept is None:
                continue
            answered[concept] = answered.get(concept, 0) + 1
            if int(ease) >= _CORRECT_EASE_CUTOFF:
                correct[concept] = correct.get(concept, 0) + 1
        return answered, correct

    def _concept_labels_safe(self) -> dict[str, str]:
        """Curated taxonomy labels, or an empty map if the taxonomy is missing
        (Android has none; it falls back to :func:`humanize_slug`)."""
        try:
            return concept_labels()
        except (FileNotFoundError, ValueError):
            return {}

    #: A concept at/above this accuracy is considered solid enough to not be
    #: prioritised by the smart Start (it can still be studied on request).
    WEAK_ACCURACY_THRESHOLD = 0.8

    def weak_concepts(self, limit: int = 0) -> list[str]:
        """Concept slugs the top-level Start should target first: under-covered
        or low-accuracy concepts that actually have questions to serve.

        A concept counts as weak/under-covered when it has not been practised,
        or its accuracy is below :data:`WEAK_ACCURACY_THRESHOLD`, or it has
        activatable lesson cards that have never been reviewed. Ordered
        weakest-first by ``(practised, accuracy, answered, slug)`` so a fresh,
        unscoped session targets the student's weak material instead of a purely
        random pool. ``limit`` of 0 returns all matches, ordered. Kept as a pure
        read for parity with Android's ``Speedrun.weakConcepts``.
        """
        weak: list[ConceptProgress] = []
        for topic in self.curriculum().topics:
            for c in topic.concepts:
                if c.served_questions == 0:
                    continue
                under_reviewed = c.lesson_cards > 0 and c.lessons_reviewed == 0
                if (
                    not c.practiced
                    or c.accuracy < self.WEAK_ACCURACY_THRESHOLD
                    or under_reviewed
                ):
                    weak.append(c)
        weak.sort(key=lambda c: (c.practiced, c.accuracy, c.answered, c.concept))
        slugs = [c.concept for c in weak]
        return slugs[:limit] if limit > 0 else slugs
