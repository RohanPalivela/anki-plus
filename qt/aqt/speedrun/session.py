# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun (MCAT) Tier-2 guided session controller.

Hitting **Start** on the MCAT home screen runs a FIXED three-phase sequence; the
student only answers and may stop — every transition is decided for them:

1. **Practice** — a capped, interleaved batch of ``pool::served`` questions via
   :class:`~aqt.speedrun.study.SpeedrunStudyDialog`. Misses are classified and
   gate card activation (already implemented in the dialog). We record which
   questions were shown and which topics were involved / missed.
2. **Memory flashcards** — review the activated cards with the NATIVE FSRS
   reviewer scoped to ``Speedrun::Cards``, capped at a session limit. When the
   reviewer finishes (runs out of cards) or the cap is hit, we auto-advance.
3. **Recap** — a short capped batch of *different* served questions on the
   topics studied this session (Phase-1 questions excluded) to measure transfer.
   Then we return to the home state with a refreshed Memory snapshot and a brief
   summary.

Empty phases are skipped with a short note. The recap is treated purely as
feedback/motivation: it never feeds the Memory score (Memory reads FSRS state of
activated cards, which this controller does not touch).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import aqt
import aqt.main
from anki.cards import Card
from anki.decks import DeckId
from anki.notes import NoteId
from anki.speedrun import FLASHCARDS_DECK_NAME, SESSION_STATE_CONFIG_KEY
from aqt import gui_hooks
from aqt.operations.deck import set_current_deck
from aqt.speedrun.study import (
    MODE_PRACTICE,
    MODE_RECAP,
    SpeedrunStudyDialog,
    StudyPhaseResult,
)
from aqt.utils import showInfo, tooltip, tr

# Defer transitions slightly so we never mutate window state from inside a
# dialog's reject() or a reviewer answer callback.
_ADVANCE_DELAY_MS = 50

# Collection-config key holding a paused session's progress so that pressing
# Start again resumes it (same phase, and — for the question phases — the same
# question) instead of restarting. Persisted in the collection so it also
# survives an app restart; the flashcard/review position is left to Anki's own
# queue, matching how those cards are scheduled. Shared with
# ``Speedrun.reset_profile`` (which clears it) via the pylib constant.
_STATE_KEY = SESSION_STATE_CONFIG_KEY


class SpeedrunSession:
    """Drives the fixed Practice → Flashcards → Recap → Home sequence."""

    def __init__(
        self, mw: aqt.main.AnkiQt, *, state: dict | None = None
    ) -> None:
        self.mw = mw
        self.caps = mw.col.speedrun.session_caps()

        # 0 = idle/fresh, 1/2/3 = current phase.
        self._phase = 0
        self._dialog: SpeedrunStudyDialog | None = None
        self._phase2_hooks_connected = False
        self._finished = False
        self._paused = False

        # The two question phases persist both their (fixed) question list and
        # the resume position; the flashcard phase persists only the phase (its
        # position is owned by Anki's scheduler).
        self.practice_ids: list[NoteId] = []
        self.practice_index = 0
        self.recap_ids: list[NoteId] = []
        self.recap_index = 0

        # Aggregate tracking for the closing summary.
        self.studied_topics: set[str] = set()
        self.missed_topics: set[str] = set()
        self.practice_shown: set[int] = set()
        self.practice_answered = 0
        self.practice_correct = 0
        self.recap_answered = 0
        self.recap_correct = 0
        self.flashcards_reviewed = 0
        self.activated_total = 0

        if state:
            self._load_state(state)

    # Persistence
    ##########################################################################

    def _load_state(self, state: dict) -> None:
        self._phase = int(state.get("phase", 0))
        self.practice_ids = [NoteId(x) for x in state.get("practice_ids", [])]
        self.practice_index = int(state.get("practice_index", 0))
        self.recap_ids = [NoteId(x) for x in state.get("recap_ids", [])]
        self.recap_index = int(state.get("recap_index", 0))
        self.studied_topics = set(state.get("studied_topics", []))
        self.missed_topics = set(state.get("missed_topics", []))
        self.practice_shown = {int(x) for x in state.get("practice_shown", [])}
        self.practice_answered = int(state.get("practice_answered", 0))
        self.practice_correct = int(state.get("practice_correct", 0))
        self.recap_answered = int(state.get("recap_answered", 0))
        self.recap_correct = int(state.get("recap_correct", 0))
        self.flashcards_reviewed = int(state.get("flashcards_reviewed", 0))
        self.activated_total = int(state.get("activated_total", 0))

    def _save_state(self) -> None:
        self.mw.col.set_config(
            _STATE_KEY,
            {
                "phase": self._phase,
                "practice_ids": [int(x) for x in self.practice_ids],
                "practice_index": self.practice_index,
                "recap_ids": [int(x) for x in self.recap_ids],
                "recap_index": self.recap_index,
                "studied_topics": sorted(self.studied_topics),
                "missed_topics": sorted(self.missed_topics),
                "practice_shown": sorted(self.practice_shown),
                "practice_answered": self.practice_answered,
                "practice_correct": self.practice_correct,
                "recap_answered": self.recap_answered,
                "recap_correct": self.recap_correct,
                "flashcards_reviewed": self.flashcards_reviewed,
                "activated_total": self.activated_total,
            },
        )

    def _clear_state(self) -> None:
        if self.mw.col.get_config(_STATE_KEY, None) is not None:
            self.mw.col.remove_config(_STATE_KEY)

    # Lifecycle
    ##########################################################################

    def start(self) -> None:
        if not self.mw.col:
            return
        if getattr(self.mw, "_speedrun_session", None) is not None:
            # A session is already running; ignore re-entry.
            return
        self.mw._speedrun_session = self  # type: ignore[attr-defined]
        # Resume into the persisted phase, or start fresh.
        if self._phase == 2:
            self._phase2()
        elif self._phase == 3:
            self._enter_phase3()
        else:
            self._enter_phase1()

    # Phase 1 — practice questions
    ##########################################################################

    def _enter_phase1(self) -> None:
        self._phase = 1
        if not self.practice_ids:
            # Prefer never-practised questions so a fresh session doesn't
            # replay the same leading batch each time; only fall back to
            # previously-seen ones (oldest first) once the pool is exhausted.
            self.practice_ids = list(
                self.mw.col.speedrun.served_questions_interleaved(
                    unseen_first=True
                )[: self.caps.practice]
            )
            self.practice_index = 0
        if not self.practice_ids or self.practice_index >= len(self.practice_ids):
            self._goto(self._phase2)
            return
        self._dialog = SpeedrunStudyDialog(
            self.mw,
            note_ids=self.practice_ids,
            mode=MODE_PRACTICE,
            title=tr.speedrun_session_practice_title(),
            on_finish=self._on_phase1_finish,
            start_index=self.practice_index,
        )

    def _on_phase1_finish(self, result: StudyPhaseResult) -> None:
        self._dialog = None
        self.studied_topics |= result.involved_topics
        self.missed_topics |= result.missed_topics
        self.practice_shown |= {int(nid) for nid in result.shown_note_ids}
        self.practice_answered += result.answered_count
        self.practice_correct += result.correct_count
        self.activated_total += result.activated_total
        if not result.completed:
            self.practice_index = result.resume_index
            self._pause()
            return
        self.practice_index = len(self.practice_ids)
        self._goto(self._phase2)

    # Phase 2 — memory flashcards (native FSRS reviewer)
    ##########################################################################

    def _phase2(self) -> None:
        self._phase = 2
        deck_id = self.mw.col.decks.id_for_name(FLASHCARDS_DECK_NAME)
        if deck_id is None:
            self._goto(self._enter_phase3)
            return
        self._connect_phase2_hooks()
        set_current_deck(parent=self.mw, deck_id=DeckId(deck_id)).success(
            lambda _: self._begin_flashcard_review()
        ).run_in_background()

    def _begin_flashcard_review(self) -> None:
        if self._phase != 2:
            return
        if sum(self.mw.col.sched.counts()) == 0:
            # Nothing activated / due in Speedrun::Cards — skip cleanly.
            self._end_phase2(advance=True)
            return
        self.mw.col.startTimebox()
        self.mw.moveToState("review")

    def _on_flashcard_answered(
        self, _reviewer: object, _card: Card, _ease: Literal[1, 2, 3, 4]
    ) -> None:
        if self._phase != 2:
            return
        self.flashcards_reviewed += 1
        if self.flashcards_reviewed >= self.caps.flashcards:
            # Respect the session cap: stop after the current card. Deferred so
            # we don't change state from inside the answer callback.
            self._schedule_end_phase2(advance=True)

    def _on_phase2_state_change(
        self,
        new_state: aqt.main.MainWindowState,
        old_state: aqt.main.MainWindowState,
    ) -> None:
        if self._phase != 2 or old_state != "review" or new_state == "review":
            return
        # The reviewer moves to "overview" when it runs out of cards (natural
        # finish). Any other destination means the student navigated away via the
        # toolbar, i.e. chose to stop the session. Deferred so we never re-enter
        # moveToState from inside a state-change hook.
        self._schedule_end_phase2(advance=new_state == "overview")

    def _schedule_end_phase2(self, *, advance: bool) -> None:
        self.mw.progress.single_shot(
            _ADVANCE_DELAY_MS, lambda: self._end_phase2(advance=advance)
        )

    def _end_phase2(self, *, advance: bool) -> None:
        if self._phase != 2:
            return
        self._disconnect_phase2_hooks()
        if not advance:
            # Navigated away mid-review: pause here (phase 2). On resume we drop
            # straight back into the flashcard reviewer.
            self._pause()
            return
        self._phase = 0
        # Don't leave the reviewer/overview sitting behind the recap dialog.
        if self.mw.state in ("review", "overview"):
            self.mw.moveToState("speedrun")
        self._goto(self._enter_phase3)

    def _connect_phase2_hooks(self) -> None:
        if self._phase2_hooks_connected:
            return
        gui_hooks.reviewer_did_answer_card.append(self._on_flashcard_answered)
        gui_hooks.state_did_change.append(self._on_phase2_state_change)
        self._phase2_hooks_connected = True

    def _disconnect_phase2_hooks(self) -> None:
        if not self._phase2_hooks_connected:
            return
        gui_hooks.reviewer_did_answer_card.remove(self._on_flashcard_answered)
        gui_hooks.state_did_change.remove(self._on_phase2_state_change)
        self._phase2_hooks_connected = False

    # Phase 3 — recap questions (transfer check)
    ##########################################################################

    def _enter_phase3(self) -> None:
        self._phase = 3
        if not self.recap_ids:
            recap_ids: list[NoteId] = []
            if self.studied_topics:
                recap_ids = self.mw.col.speedrun.served_questions_interleaved(
                    topics=self.studied_topics,
                    exclude=set(self.practice_shown),
                )[: self.caps.recap]
            self.recap_ids = [NoteId(x) for x in recap_ids]
            self.recap_index = 0
        if not self.recap_ids or self.recap_index >= len(self.recap_ids):
            self._finish(stopped=False)
            return
        self._dialog = SpeedrunStudyDialog(
            self.mw,
            note_ids=self.recap_ids,
            mode=MODE_RECAP,
            title=tr.speedrun_session_recap_title(),
            on_finish=self._on_phase3_finish,
            start_index=self.recap_index,
        )

    def _on_phase3_finish(self, result: StudyPhaseResult) -> None:
        self._dialog = None
        self.recap_answered += result.answered_count
        self.recap_correct += result.correct_count
        # Recap is feedback only: activation is fine, but it must not (and does
        # not) inflate Memory, which reads FSRS state of the flashcards.
        self.activated_total += result.activated_total
        if not result.completed:
            self.recap_index = result.resume_index
            self._pause()
            return
        self._finish(stopped=False)

    # Finish / teardown
    ##########################################################################

    def _goto(self, func: Callable[[], None]) -> None:
        self.mw.progress.single_shot(_ADVANCE_DELAY_MS, func)

    def _pause(self) -> None:
        """Persist progress and detach so the next Start resumes here."""
        if self._finished or self._paused:
            return
        self._paused = True
        self._save_state()
        self._disconnect_phase2_hooks()
        if getattr(self.mw, "_speedrun_session", None) is self:
            self.mw._speedrun_session = None  # type: ignore[attr-defined]
        self._phase = 0
        tooltip(tr.speedrun_session_paused(), period=4000, parent=self.mw)

    def _finish(self, *, stopped: bool) -> None:
        if self._finished:
            return
        self._finished = True
        self._phase = 0
        self._disconnect_phase2_hooks()
        self._clear_state()
        if getattr(self.mw, "_speedrun_session", None) is self:
            self.mw._speedrun_session = None  # type: ignore[attr-defined]

        # Refresh the home Memory snapshot if we're sitting on it (the common
        # case: a phase dialog just closed over the home, or the run completed).
        home = getattr(self.mw, "speedrun_home", None)
        if home is not None and self.mw.state == "speedrun":
            home.refresh()

        self._show_summary(stopped)

    def _show_summary(self, stopped: bool) -> None:
        if stopped:
            tooltip(tr.speedrun_session_stopped(), period=4000, parent=self.mw)
            return
        answered = self.practice_answered + self.recap_answered
        if answered == 0 and self.flashcards_reviewed == 0:
            showInfo(
                tr.speedrun_session_nothing(),
                parent=self.mw,
                title=tr.speedrun_menu(),
            )
            return
        correct = self.practice_correct + self.recap_correct
        lines = [
            tr.speedrun_session_complete(),
            tr.speedrun_session_complete_detail(
                answered_count=answered,
                correct_count=correct,
                reviewed_count=self.flashcards_reviewed,
            ),
        ]
        if self.studied_topics:
            topics = ", ".join(
                sorted(topic.replace("-", " ") for topic in self.studied_topics)
            )
            lines.append(tr.speedrun_session_complete_topics(topics=topics))
        tooltip("<br>".join(lines), period=7000, parent=self.mw)


def start_session(mw: aqt.main.AnkiQt) -> None:
    """Entry point used by the home screen's Start button.

    Practice is gated on the real question bank having been imported, so a first
    Start prompts the student to import before any questions are served. If a
    previous run was paused, its saved progress is resumed instead of starting
    a new sequence.
    """
    from aqt.speedrun import ensure_bank_imported

    if not mw.col or not ensure_bank_imported(mw):
        return
    state = mw.col.get_config(_STATE_KEY, None)
    SpeedrunSession(mw, state=state).start()
