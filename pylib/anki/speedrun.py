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
   starter MCAT ``speedrunBlueprint`` config, plus an optional, clearly
   *synthetic* demo dataset so the question-first gating loop and the Memory
   dashboard are demoable end-to-end. The engine's activation/mastery logic is
   *not* re-implemented here — we only create native objects and call the
   frozen RPCs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anki
import anki.collection
from anki import speedrun_pb2
from anki.notes import Note, NoteId

if TYPE_CHECKING:
    from anki.decks import DeckId
    from anki.models import NotetypeId

# public exports
MissReason = speedrun_pb2.MissReason
ActivateCardsResponse = speedrun_pb2.ActivateCardsResponse
MemoryScoreResponse = speedrun_pb2.MemoryScoreResponse

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
#: Marker tag applied to every synthetic demo note so seeding is idempotent and
#: the demo content is trivially findable/removable. Never ship as real content.
DEMO_TAG = "speedrun-demo"

# Config keys (must match rslib/src/speedrun/blueprint.rs + config/bool.rs).
BLUEPRINT_CONFIG_KEY = "speedrunBlueprint"
SWEEP_SAMPLE_SIZE_CONFIG_KEY = "speedrunSweepSampleSize"
ORDERING_CONFIG_KEY = "speedrunOrdering"

# Guided-session per-phase caps (Tier 2). Tunable via collection config so the
# fixed sequence never floods the student (adaptive/capped sizing). Defaults are
# deliberately small to avoid cognitive overload.
SESSION_PRACTICE_CAP_CONFIG_KEY = "speedrunSessionPracticeCap"
SESSION_FLASHCARD_CAP_CONFIG_KEY = "speedrunSessionFlashcardCap"
SESSION_RECAP_CAP_CONFIG_KEY = "speedrunSessionRecapCap"
DEFAULT_SESSION_PRACTICE_CAP = 10
DEFAULT_SESSION_FLASHCARD_CAP = 20
DEFAULT_SESSION_RECAP_CAP = 5

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
    demo_loaded: bool
    demo_already_present: bool = False
    questions_created: int = 0
    suspended_flashcards_created: int = 0
    studied_flashcards_created: int = 0


def topic_of_note(note: Note) -> str | None:
    """Return the bare topic name from a note's first ``topic::`` tag, if any."""
    for tag in note.tags:
        if tag.startswith(TOPIC_TAG_PREFIX):
            return tag[len(TOPIC_TAG_PREFIX) :]
    return None


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
        load_demo_data: bool = True,
        enable_value_ordering: bool = True,
    ) -> SetupSummary:
        """Idempotently provision the Speedrun data model for MCAT.

        Creates the ``SpeedrunQuestion`` notetype, the ``Speedrun::Questions``
        and ``Speedrun::Cards`` decks, and the starter blueprint config; turns
        on value ordering; and optionally loads the synthetic demo dataset.

        Re-running is safe: existing objects are reused and demo seeding is
        skipped if it has already run.
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

        summary = SetupSummary(
            notetype_id=int(notetype_id),
            questions_deck_id=int(questions_deck),
            flashcards_deck_id=int(flashcards_deck),
            blueprint_topics=len(blueprint.get("topics", [])),
            demo_loaded=False,
        )
        if load_demo_data:
            self._seed_demo_data(
                notetype_id, questions_deck, flashcards_deck, blueprint, summary
            )
        return summary

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

    def _seed_demo_data(
        self,
        notetype_id: NotetypeId,
        questions_deck: DeckId,
        flashcards_deck: DeckId,
        blueprint: dict[str, Any],
        summary: SetupSummary,
    ) -> None:
        """Create the synthetic demo dataset (idempotent via ``DEMO_TAG``).

        Per topic this creates: 2 served questions, 3 *suspended* linked
        flashcards (the gating targets), and 5 *already-studied* flashcards
        carrying a synthetic FSRS memory state so the Memory dashboard renders
        real per-topic mastery. Everything is obviously synthetic placeholder
        content (D-10 / D-16) and must never be shipped as real material.
        """
        col = self.col
        if col.find_notes(f"tag:{DEMO_TAG}"):
            # Already seeded; report what exists so the UI stays informative.
            summary.demo_loaded = True
            summary.demo_already_present = True
            summary.questions_created = len(
                col.find_notes(f"note:{QUESTION_NOTETYPE_NAME} tag:{DEMO_TAG}")
            )
            return

        from anki.cards import FSRSMemoryState
        from anki.consts import CARD_TYPE_REV, QUEUE_TYPE_REV

        question_nt = col.models.get(notetype_id)
        basic_nt = col.models.by_name("Basic")
        assert question_nt is not None and basic_nt is not None

        topics = [t["name"] for t in blueprint.get("topics", [])]
        now = int(time.time())
        today = col.sched.today
        letters = ["A", "B", "C", "D"]
        # Fraction of one stability period that has elapsed since "review",
        # cycled across topics to produce a visible mastery spread.
        elapsed_ratios = [0.2, 0.4, 0.7, 1.0, 1.6, 2.5, 4.0]
        stability_days = 80.0

        suspend_card_ids: list[Any] = []

        for topic_index, topic in enumerate(topics):
            topic_tag = f"{TOPIC_TAG_PREFIX}{topic}"
            label = topic.replace("-", " ")

            for q in range(2):
                note = col.new_note(question_nt)
                correct = letters[(topic_index + q) % 4]
                note["stem"] = (
                    f"Synthetic demo: placeholder MCAT-style question {q + 1} on "
                    f"{label}. (Not real content.) Which option is designated "
                    "correct?"
                )
                note["options"] = "\n".join(
                    f"Synthetic option {opt} for {label}"
                    for opt in ("alpha", "beta", "gamma", "delta")
                )
                note["correct"] = correct
                note["explanation"] = (
                    f"Synthetic explanation: option {correct} is the designated "
                    f"answer for this placeholder {label} item."
                )
                note["source"] = "Synthetic seed (not real MCAT content)"
                note["difficulty_b"] = f"{-1.0 + 0.5 * q + 0.2 * topic_index:.2f}"
                note["discrimination_a"] = f"{0.8 + 0.1 * topic_index:.2f}"
                note.tags = [topic_tag, POOL_SERVED_TAG, DEMO_TAG]
                col.add_note(note, questions_deck)
                summary.questions_created += 1

            for f in range(3):
                note = col.new_note(basic_nt)
                note["Front"] = f"Synthetic flashcard {f + 1} ({label}) - front"
                note["Back"] = (
                    f"Synthetic flashcard {f + 1} ({label}) - back (placeholder)"
                )
                note.tags = [topic_tag, DEMO_TAG]
                col.add_note(note, flashcards_deck)
                suspend_card_ids.extend(c.id for c in note.cards())
                summary.suspended_flashcards_created += 1

            elapsed = int(
                elapsed_ratios[topic_index % len(elapsed_ratios)]
                * stability_days
                * 86400
            )
            for s in range(5):
                note = col.new_note(basic_nt)
                note["Front"] = f"Synthetic studied card {s + 1} ({label}) - front"
                note["Back"] = f"Synthetic studied card {s + 1} ({label}) - back"
                note.tags = [topic_tag, DEMO_TAG]
                col.add_note(note, flashcards_deck)
                for card in note.cards():
                    card.memory_state = FSRSMemoryState(
                        stability=stability_days,
                        difficulty=5.0 + 0.3 * topic_index,
                    )
                    card.last_review_time = now - elapsed
                    card.type = CARD_TYPE_REV
                    card.queue = QUEUE_TYPE_REV
                    card.reps = 3
                    card.due = today + 30
                    col.update_card(card)
                summary.studied_flashcards_created += 1

        if suspend_card_ids:
            col.sched.suspend_cards(suspend_card_ids)
        summary.demo_loaded = True

    # Question-first study loop support (M2 2b surface, minimal M1 slice)
    ##########################################################################

    def served_question_note_ids(self) -> list[NoteId]:
        """Note ids of served (never held-out) practice questions, in a stable
        interleaved-ish order. Held-out questions are never returned."""
        query = (
            f"note:{QUESTION_NOTETYPE_NAME} tag:{POOL_SERVED_TAG} "
            f"-tag:{POOL_HELDOUT_TAG}"
        )
        return list(self.col.find_notes(query, order=True))

    def served_questions_interleaved(
        self,
        *,
        topics: set[str] | None = None,
        exclude: set[int] | None = None,
    ) -> list[NoteId]:
        """Served question note ids interleaved across topics (no topic blocking).

        ``topics`` restricts the result to those bare topic names (used by the
        recap phase to target only the just-studied topics); ``exclude`` drops
        specific note ids (used to keep the recap set disjoint from Phase 1).

        Note: this loads each served note to read its ``topic::`` tag. The served
        pool is small (tens of items) so this is cheap; revisit if it grows.
        """
        exclude = exclude or set()
        groups: dict[str, list[NoteId]] = {}
        for nid in self.served_question_note_ids():
            if nid in exclude:
                continue
            topic = topic_of_note(self.col.get_note(nid)) or ""
            if topics is not None and topic not in topics:
                continue
            groups.setdefault(topic, []).append(nid)
        return _round_robin(list(groups.values()))

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
