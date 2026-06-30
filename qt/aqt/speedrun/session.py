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
from anki.speedrun import FLASHCARDS_DECK_NAME
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


class SpeedrunSession:
    """Drives the fixed Practice → Flashcards → Recap → Home sequence."""

    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        self.mw = mw
        self.caps = mw.col.speedrun.session_caps()

        # 0 = idle, 1/2/3 = current phase.
        self._phase = 0
        self._dialog: SpeedrunStudyDialog | None = None
        self._phase2_hooks_connected = False
        self._finished = False

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

    # Lifecycle
    ##########################################################################

    def start(self) -> None:
        if not self.mw.col:
            return
        if getattr(self.mw, "_speedrun_session", None) is not None:
            # A session is already running; ignore re-entry.
            return
        self.mw._speedrun_session = self  # type: ignore[attr-defined]
        self._phase1()

    # Phase 1 — practice questions
    ##########################################################################

    def _phase1(self) -> None:
        self._phase = 1
        practice_ids = self.mw.col.speedrun.served_questions_interleaved()[
            : self.caps.practice
        ]
        if not practice_ids:
            self._goto(self._phase2)
            return
        self._dialog = SpeedrunStudyDialog(
            self.mw,
            note_ids=practice_ids,
            mode=MODE_PRACTICE,
            title=tr.speedrun_session_practice_title(),
            on_finish=self._on_phase1_finish,
        )

    def _on_phase1_finish(self, result: StudyPhaseResult) -> None:
        self._dialog = None
        self.studied_topics |= result.involved_topics
        self.missed_topics |= result.missed_topics
        self.practice_shown |= {int(nid) for nid in result.shown_note_ids}
        self.practice_answered = result.answered_count
        self.practice_correct = result.correct_count
        self.activated_total += result.activated_total
        if not result.completed:
            self._finish(stopped=True)
            return
        self._goto(self._phase2)

    # Phase 2 — memory flashcards (native FSRS reviewer)
    ##########################################################################

    def _phase2(self) -> None:
        self._phase = 2
        self.flashcards_reviewed = 0
        deck_id = self.mw.col.decks.id_for_name(FLASHCARDS_DECK_NAME)
        if deck_id is None:
            self._goto(self._phase3)
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
        self._phase = 0
        self._disconnect_phase2_hooks()
        if not advance:
            self._finish(stopped=True)
            return
        # Don't leave the reviewer/overview sitting behind the recap dialog.
        if self.mw.state in ("review", "overview"):
            self.mw.moveToState("speedrun")
        self._goto(self._phase3)

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

    def _phase3(self) -> None:
        self._phase = 3
        recap_ids: list[NoteId] = []
        if self.studied_topics:
            recap_ids = self.mw.col.speedrun.served_questions_interleaved(
                topics=self.studied_topics,
                exclude=set(self.practice_shown),
            )[: self.caps.recap]
        if not recap_ids:
            self._finish(stopped=False)
            return
        self._dialog = SpeedrunStudyDialog(
            self.mw,
            note_ids=recap_ids,
            mode=MODE_RECAP,
            title=tr.speedrun_session_recap_title(),
            on_finish=self._on_phase3_finish,
        )

    def _on_phase3_finish(self, result: StudyPhaseResult) -> None:
        self._dialog = None
        self.recap_answered = result.answered_count
        self.recap_correct = result.correct_count
        # Recap is feedback only: activation is fine, but it must not (and does
        # not) inflate Memory, which reads FSRS state of the flashcards.
        self.activated_total += result.activated_total
        self._finish(stopped=not result.completed)

    # Finish / teardown
    ##########################################################################

    def _goto(self, func: Callable[[], None]) -> None:
        self.mw.progress.single_shot(_ADVANCE_DELAY_MS, func)

    def _finish(self, *, stopped: bool) -> None:
        if self._finished:
            return
        self._finished = True
        self._phase = 0
        self._disconnect_phase2_hooks()
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
    """Entry point used by the home screen's Start button."""
    SpeedrunSession(mw).start()
