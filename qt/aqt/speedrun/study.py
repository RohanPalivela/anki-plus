# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun question-first study loop (SPOV3 core, minimal M1 slice).

Serves ``pool::served`` questions from ``Speedrun::Questions``, grades each
answer through the native ``answer_card`` path (so it lands in ``revlog``), and
on an incorrect answer offers a miss-reason chooser that calls
``col.speedrun.record_miss_reason``. Qualifying misses (knowledge-gap /
missing-context) unsuspend the question's linked cards via the Rust gating RPC;
the dialog then surfaces how many cards were activated.
"""

from __future__ import annotations

from functools import partial

import aqt
import aqt.main
from anki.cards import CardId
from anki.notes import NoteId
from anki.speedrun import MissReason, correct_index, option_lines
from aqt.qt import *
from aqt.utils import disable_help_button, restoreGeom, saveGeom, tooltip, tr

# (MissReason, label, hint) for the four chooser buttons.
_MISS_BUTTONS = [
    (MissReason.KNOWLEDGE_GAP, "speedrun_miss_knowledge_gap", True),
    (MissReason.MISSING_CONTEXT, "speedrun_miss_missing_context", True),
    (MissReason.MISUNDERSTANDING, "speedrun_miss_misunderstanding", False),
    (MissReason.CARELESS, "speedrun_miss_careless", False),
]


class SpeedrunStudyDialog(QDialog):
    """A minimal but fully working question-first study surface."""

    TITLE = "speedrunStudy"

    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        QDialog.__init__(self, mw, Qt.WindowType.Window)
        self.mw = mw
        self.mw.garbage_collect_on_dialog_finish(self)
        self._dirty = False

        self.note_ids: list[NoteId] = list(mw.col.speedrun.served_question_note_ids())
        self.index = 0
        self.answered_count = 0
        self.correct_count = 0
        self.activated_total = 0

        self._answered_current = False
        self.current_card_id: CardId | None = None
        self.current_correct_index = -1
        self.option_buttons: list[QPushButton] = []

        self.setWindowTitle(tr.speedrun_study_title())
        disable_help_button(self)
        self.setMinimumWidth(560)
        restoreGeom(self, self.TITLE, default_size=(620, 640))
        self._build_ui()

        if not self.note_ids:
            self._show_empty_state()
        else:
            self._load_question()
        self.show()

    # UI construction
    ##########################################################################

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

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
        footer.addWidget(self.sweep_button)
        footer.addStretch(1)
        close_button = QPushButton(tr.actions_close())
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

        note = self.mw.col.get_note(NoteId(self.note_ids[self.index]))
        cards = note.cards()
        self.current_card_id = cards[0].id if cards else None
        topic = next(
            (t.split("::", 1)[1] for t in note.tags if t.startswith("topic::")),
            "—",
        )
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

        # Native review write (Again=incorrect, Good=correct) -> revlog.
        card = self.mw.col.get_card(self.current_card_id)
        card.start_timer()
        self.mw.col.sched.answerCard(card, 3 if is_correct else 1)

        self.answered_count += 1
        if is_correct:
            self.correct_count += 1
            self.result_label.setText(tr.speedrun_study_correct())
            self.result_label.setStyleSheet(
                "font-weight: bold; font-size: 15px; color: #2e7d32;"
            )
        else:
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
            # Qualifying reason, but this topic's linked cards are already active
            # (e.g. an earlier miss on the same topic already unsuspended them).
            self.activation_label.setText(tr.speedrun_study_already_active())
        else:
            self.activation_label.setText(tr.speedrun_study_none_activated())
        self.activation_label.setVisible(True)
        self.miss_container.setVisible(False)
        self.next_button.setVisible(True)
        self._update_tally()

    def _on_next(self) -> None:
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

    def _show_finished_state(self) -> None:
        self._clear_options()
        self.result_label.setVisible(False)
        self.explanation_label.setVisible(False)
        self.miss_container.setVisible(False)
        self.activation_label.setVisible(False)
        self.next_button.setVisible(False)
        self.progress_label.setText("")
        self.topic_label.setText("")
        self.stem_label.setText(tr.speedrun_study_finished())

    def reject(self) -> None:
        saveGeom(self, self.TITLE)
        if self._dirty:
            # Reflect new revlog entries / activated cards in the main window.
            self.mw.reset()
        QDialog.reject(self)
