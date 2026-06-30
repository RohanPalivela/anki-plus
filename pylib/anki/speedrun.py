# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun (MCAT fork) helpers.

Thin wrappers over the Rust `SpeedrunService` RPCs, plus the minimal miss-reason
flow: classify a missed question, persist the latest reason as a `miss::<reason>`
note tag (D-11; never `card.custom_data`), and call gated activation.
"""

from __future__ import annotations

import anki
import anki.collection
from anki import speedrun_pb2
from anki.notes import NoteId

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
