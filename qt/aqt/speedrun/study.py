# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun question-first study loop (SPOV3 core, minimal M1 slice).

Serves ``pool::served`` questions from ``Speedrun::Questions``, grades each
answer through the native ``answer_card`` path (so it lands in ``revlog``), and
on an incorrect answer offers a miss-reason chooser that calls
``col.speedrun.record_miss_reason``. Qualifying misses (knowledge-gap /
missing-context) unsuspend the question's linked cards via the Rust gating RPC;
the dialog then surfaces how many cards were activated.

The dialog backs both the standalone Tools entry (all served questions) and the
Tier-2 guided session (Phase 1 "Practice" / Phase 3 "Recap"): the session
controller injects an explicit question list, a mode/title, and an on-finish
callback so it can drive the fixed sequence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

import aqt
import aqt.main
from anki.cards import CardId
from anki.notes import NoteId
from anki.speedrun import (
    QUESTION_NOTETYPE_NAME,
    MissReason,
    correct_index,
    option_lines,
    topic_of_note,
)
from aqt.qt import *
from aqt.utils import disable_help_button, restoreGeom, saveGeom, tooltip, tr

# Dialog modes. "standalone" keeps the original Tools→Study behaviour; the two
# session modes are driven by the guided-session controller.
MODE_STANDALONE = "standalone"
MODE_PRACTICE = "practice"
MODE_RECAP = "recap"

# (MissReason, label, hint) for the four chooser buttons.
_MISS_BUTTONS = [
    (MissReason.KNOWLEDGE_GAP, "speedrun_miss_knowledge_gap", True),
    (MissReason.MISSING_CONTEXT, "speedrun_miss_missing_context", True),
    (MissReason.MISUNDERSTANDING, "speedrun_miss_misunderstanding", False),
    (MissReason.CARELESS, "speedrun_miss_careless", False),
]


@dataclass
class StudyPhaseResult:
    """What a study phase produced, handed back to the session controller."""

    #: True if the student reached the end of the injected questions; False if
    #: they closed the dialog early (i.e. chose to stop the session).
    completed: bool
    #: Note ids actually shown this phase (drives recap exclusion).
    shown_note_ids: list[NoteId] = field(default_factory=list)
    #: Bare topic names of every question shown this phase.
    involved_topics: set[str] = field(default_factory=set)
    #: Bare topic names of questions answered incorrectly this phase.
    missed_topics: set[str] = field(default_factory=set)
    answered_count: int = 0
    correct_count: int = 0
    activated_total: int = 0
    #: Index into the injected question list to resume at if the phase was
    #: stopped early (the question the student was on, or the next one if the
    #: current question was already answered). Only meaningful when
    #: ``completed`` is False.
    resume_index: int = 0


class SpeedrunStudyDialog(QDialog):
    """A minimal but fully working question-first study surface.

    Standalone use serves every ``pool::served`` question. The guided session
    injects ``note_ids`` (a capped, interleaved batch), a ``mode``/``title``, and
    an ``on_finish`` callback so the controller can advance the fixed sequence.
    """

    TITLE = "speedrunStudy"

    def __init__(
        self,
        mw: aqt.main.AnkiQt,
        *,
        note_ids: list[NoteId] | None = None,
        mode: str = MODE_STANDALONE,
        title: str | None = None,
        on_finish: Callable[[StudyPhaseResult], None] | None = None,
        allow_sweep: bool = True,
        start_index: int = 0,
    ) -> None:
        QDialog.__init__(self, mw, Qt.WindowType.Window)
        self.mw = mw
        self.mw.garbage_collect_on_dialog_finish(self)
        self._dirty = False

        if note_ids is None:
            self.note_ids: list[NoteId] = list(
                mw.col.speedrun.served_question_note_ids()
            )
        else:
            self.note_ids = list(note_ids)
        self.mode = mode
        self._on_finish = on_finish
        # The guided session keeps full control; hide the manual sweep there.
        self._allow_sweep = allow_sweep and mode == MODE_STANDALONE
        # Clamp the resume point in case the injected list shrank since it was
        # persisted.
        self.index = max(0, min(start_index, len(self.note_ids)))
        self.answered_count = 0
        self.correct_count = 0
        self.activated_total = 0

        # Session tracking surfaced to the controller via StudyPhaseResult.
        self.shown_note_ids: list[NoteId] = []
        self.involved_topics: set[str] = set()
        self.missed_topics: set[str] = set()
        self._current_topic: str | None = None
        self._completed = False
        self._finish_emitted = False
        self._at_end = False

        self._answered_current = False
        self.current_card_id: CardId | None = None
        self.current_correct_index = -1
        self.option_buttons: list[QPushButton] = []

        self.setWindowTitle(title or tr.speedrun_study_title())
        disable_help_button(self)
        self.setMinimumWidth(560)
        restoreGeom(self, self.TITLE, default_size=(620, 640))
        self._build_ui(header=title)

        if not self.note_ids:
            self._show_empty_state()
        elif self.index >= len(self.note_ids):
            # Resuming past the end: this phase was already finished.
            self._show_finished_state()
        else:
            self._load_question()
        self.show()

    # UI construction
    ##########################################################################

    def _build_ui(self, header: str | None = None) -> None:
        outer = QVBoxLayout(self)

        if header:
            self.header_label = QLabel(header)
            self.header_label.setStyleSheet(
                "font-size: 17px; font-weight: 800; margin-bottom: 4px;"
            )
            outer.addWidget(self.header_label)

        self.progress_label = QLabel()
        self.progress_label.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.progress_label)

        self.topic_label = QLabel()
        self.topic_label.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.topic_label)

        self.stem_label = QLabel()
        self.stem_label.setWordWrap(True)
        self.stem_label.setTextFormat(Qt.TextFormat.RichText)
        self.stem_label.setStyleSheet("font-size: 15px; margin: 8px 0;")
        outer.addWidget(self.stem_label)

        self.options_box = QVBoxLayout()
        outer.addLayout(self.options_box)

        self.result_label = QLabel()
        self.result_label.setStyleSheet("font-weight: bold; font-size: 15px;")
        self.result_label.setVisible(False)
        outer.addWidget(self.result_label)

        self.explanation_label = QLabel()
        self.explanation_label.setWordWrap(True)
        self.explanation_label.setVisible(False)
        outer.addWidget(self.explanation_label)

        # Miss-reason chooser (incorrect answers only).
        self.miss_container = QWidget()
        miss_layout = QVBoxLayout(self.miss_container)
        miss_layout.setContentsMargins(0, 6, 0, 0)
        self.why_label = QLabel(tr.speedrun_study_why_missed())
        self.why_label.setStyleSheet("font-weight: bold;")
        miss_layout.addWidget(self.why_label)
        miss_grid = QHBoxLayout()
        for reason, key, activates in _MISS_BUTTONS:
            button = QPushButton(getattr(tr, key)())
            # Keep the button label terse; the activation behavior is a tooltip.
            button.setToolTip(
                tr.speedrun_miss_activates()
                if activates
                else tr.speedrun_miss_no_activation()
            )
            qconnect(button.clicked, partial(self._on_miss_reason, reason))
            miss_grid.addWidget(button)
        miss_layout.addLayout(miss_grid)
        self.miss_container.setVisible(False)
        outer.addWidget(self.miss_container)

        self.activation_label = QLabel()
        self.activation_label.setWordWrap(True)
        self.activation_label.setStyleSheet(
            "color: palette(highlight); font-weight: bold;"
        )
        self.activation_label.setVisible(False)
        outer.addWidget(self.activation_label)

        outer.addStretch(1)

        self.next_button = QPushButton(tr.speedrun_study_next())
        self.next_button.setVisible(False)
        qconnect(self.next_button.clicked, self._on_next)
        outer.addWidget(self.next_button)

        # Footer: tally + sweep + close.
        self.tally_label = QLabel()
        outer.addWidget(self.tally_label)

        footer = QHBoxLayout()
        self.sweep_button = QPushButton(tr.speedrun_study_run_sweep())
        qconnect(self.sweep_button.clicked, self._on_sweep)
        self.sweep_button.setVisible(self._allow_sweep)
        footer.addWidget(self.sweep_button)
        footer.addStretch(1)
        # In a guided session, closing means "stop the session" rather than just
        # "close a tool window", so label it accordingly.
        close_label = (
            tr.actions_close()
            if self.mode == MODE_STANDALONE
            else tr.speedrun_session_stop()
        )
        close_button = QPushButton(close_label)
        qconnect(close_button.clicked, self.reject)
        footer.addWidget(close_button)
        outer.addLayout(footer)

        self._update_tally()

    # Question lifecycle
    ##########################################################################

    def _clear_options(self) -> None:
        for button in self.option_buttons:
            self.options_box.removeWidget(button)
            button.deleteLater()
        self.option_buttons = []

    def _load_question(self) -> None:
        self._answered_current = False
        self.result_label.setVisible(False)
        self.explanation_label.setVisible(False)
        self.miss_container.setVisible(False)
        self.activation_label.setVisible(False)
        self.next_button.setVisible(False)
        self._clear_options()

        note_id = NoteId(self.note_ids[self.index])
        note = self.mw.col.get_note(note_id)
        cards = note.cards()
        self.current_card_id = cards[0].id if cards else None
        bare_topic = topic_of_note(note)
        topic = bare_topic or "—"
        self._current_topic = bare_topic
        if note_id not in self.shown_note_ids:
            self.shown_note_ids.append(note_id)
        if bare_topic:
            self.involved_topics.add(bare_topic)
        self.progress_label.setText(
            tr.speedrun_study_progress(
                current_count=self.index + 1, total_count=len(self.note_ids)
            )
        )
        self.topic_label.setText(tr.speedrun_study_topic(topic=topic))
        self.stem_label.setText(note["stem"])

        options = option_lines(note["options"])
        self.current_correct_index = correct_index(note["correct"], len(options))
        self._explanation = note["explanation"]
        self._source = note["source"]
        for i, text in enumerate(options):
            letter = chr(ord("A") + i)
            button = QPushButton(f"{letter}.  {text}")
            button.setStyleSheet("text-align: left; padding: 8px;")
            qconnect(button.clicked, partial(self._on_option, i))
            self.options_box.addWidget(button)
            self.option_buttons.append(button)

    def _on_option(self, chosen_index: int) -> None:
        if self._answered_current or self.current_card_id is None:
            return
        self._answered_current = True
        self._dirty = True
        is_correct = chosen_index == self.current_correct_index

        # Mark up the options.
        for i, button in enumerate(self.option_buttons):
            button.setEnabled(False)
            if i == self.current_correct_index:
                button.setStyleSheet(
                    "text-align: left; padding: 8px; background: #2e7d32; color: white;"
                )
            elif i == chosen_index:
                button.setStyleSheet(
                    "text-align: left; padding: 8px; background: #c62828; color: white;"
                )

        # Native review write (Again=incorrect, Good=correct) -> revlog. Graded
        # out of queue (from_queue=False): the question card is fetched by note
        # id, not served from the study queue, and a stale review queue (e.g. the
        # flashcard queue left cached after Phase 2) would otherwise reject this
        # with "not at top of queue".
        card = self.mw.col.get_card(self.current_card_id)
        card.start_timer()
        self.mw.col.sched.answerCard(card, 3 if is_correct else 1, from_queue=False)

        self.answered_count += 1
        if is_correct:
            self.correct_count += 1
            self.result_label.setText(tr.speedrun_study_correct())
            self.result_label.setStyleSheet(
                "font-weight: bold; font-size: 15px; color: #2e7d32;"
            )
        else:
            if self._current_topic:
                self.missed_topics.add(self._current_topic)
            self.result_label.setText(tr.speedrun_study_incorrect())
            self.result_label.setStyleSheet(
                "font-weight: bold; font-size: 15px; color: #c62828;"
            )
        self.result_label.setVisible(True)

        explanation = self._explanation
        if self._source:
            explanation = f"{explanation}<br><small>{self._source}</small>"
        self.explanation_label.setText(
            f"<b>{tr.speedrun_study_explanation()}:</b> {explanation}"
        )
        self.explanation_label.setVisible(True)

        if is_correct:
            self.next_button.setVisible(True)
        else:
            self.miss_container.setVisible(True)
        self._update_tally()

    def _on_miss_reason(self, reason: MissReason.V) -> None:
        note_id = int(self.note_ids[self.index])
        resp = self.mw.col.speedrun.record_miss_reason(note_id, reason)
        count = len(resp.activated_card_ids)
        self.activated_total += count
        if count:
            self.activation_label.setText(tr.speedrun_study_activated(count=count))
        elif reason in (MissReason.KNOWLEDGE_GAP, MissReason.MISSING_CONTEXT):
            # Qualifying reason but nothing was unsuspended. Distinguish the two
            # very different causes so the message is honest: either this topic
            # has no linked memory cards at all, or they are already active.
            if self._topic_has_linked_flashcards():
                self.activation_label.setText(tr.speedrun_study_already_active())
            else:
                self.activation_label.setText(tr.speedrun_study_no_linked_cards())
        else:
            self.activation_label.setText(tr.speedrun_study_none_activated())
        self.activation_label.setVisible(True)
        self.miss_container.setVisible(False)
        self.next_button.setVisible(True)
        self._update_tally()

    def _topic_has_linked_flashcards(self) -> bool:
        """True if the current topic has any *memory flashcards* (non-question
        notes sharing its ``topic::`` tag), regardless of suspension state.

        Used only to phrase the "nothing activated" message correctly: the
        practice questions themselves also carry ``topic::`` tags, so they must
        be excluded — otherwise every topic looks like it has linked cards."""
        if not self._current_topic:
            return False
        query = (
            f"tag:topic::{self._current_topic} -note:{QUESTION_NOTETYPE_NAME}"
        )
        return bool(self.mw.col.find_cards(query))

    def _on_next(self) -> None:
        # In a session, the finished screen reuses this button as "Continue".
        if self._at_end:
            self.reject()
            return
        self.index += 1
        if self.index >= len(self.note_ids):
            self._show_finished_state()
        else:
            self._load_question()

    def _on_sweep(self) -> None:
        resp = self.mw.col.speedrun.run_coverage_sweep()
        count = len(resp.activated_card_ids)
        self.activated_total += count
        self._dirty = True
        tooltip(tr.speedrun_study_sweep_done(count=count), parent=self)
        self._update_tally()

    # Helpers
    ##########################################################################

    def _update_tally(self) -> None:
        self.tally_label.setText(
            tr.speedrun_study_tally(
                answered_count=self.answered_count,
                correct_count=self.correct_count,
                activated_count=self.activated_total,
            )
        )

    def _show_empty_state(self) -> None:
        self.progress_label.setText("")
        self.stem_label.setText(tr.speedrun_study_no_questions())
        self.sweep_button.setEnabled(False)
        if self.mode != MODE_STANDALONE:
            # Nothing to do this phase; let the controller advance on close.
            self._completed = True
            self._at_end = True
            self.next_button.setText(tr.speedrun_session_continue())
            self.next_button.setVisible(True)

    def _show_finished_state(self) -> None:
        self._clear_options()
        self.result_label.setVisible(False)
        self.explanation_label.setVisible(False)
        self.miss_container.setVisible(False)
        self.activation_label.setVisible(False)
        self.progress_label.setText("")
        self.topic_label.setText("")
        self.stem_label.setText(tr.speedrun_study_finished())
        if self.mode == MODE_STANDALONE:
            self.next_button.setVisible(False)
            return
        # Session: reaching the end means this phase completed successfully.
        self._completed = True
        self._at_end = True
        self.next_button.setText(tr.speedrun_session_continue())
        self.next_button.setVisible(True)

    def _emit_finish(self) -> None:
        if self._finish_emitted:
            return
        self._finish_emitted = True
        if self._on_finish is None:
            return
        # Resume on the question the student is currently viewing, unless they
        # already answered it — in that case pick up at the next one.
        resume_index = self.index + 1 if self._answered_current else self.index
        self._on_finish(
            StudyPhaseResult(
                completed=self._completed,
                shown_note_ids=list(self.shown_note_ids),
                involved_topics=set(self.involved_topics),
                missed_topics=set(self.missed_topics),
                answered_count=self.answered_count,
                correct_count=self.correct_count,
                activated_total=self.activated_total,
                resume_index=resume_index,
            )
        )

    def reject(self) -> None:
        saveGeom(self, self.TITLE)
        # Standalone reflects new revlog entries / activated cards immediately;
        # in a session the controller owns the next transition, so don't reset
        # the main window out from under it.
        if self._dirty and self.mode == MODE_STANDALONE:
            self.mw.reset()
        self._emit_finish()
        QDialog.reject(self)
