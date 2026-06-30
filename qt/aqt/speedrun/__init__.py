# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun (MCAT fork) desktop entry points.

Adds a Tools → "Speedrun (MCAT)" submenu wiring up three actions:

* **Set up Speedrun (MCAT)** — runtime data-model provisioning (D-13) plus an
  optional synthetic demo dataset.
* **Study (question-first)** — the SPOV3 gating loop (see :mod:`aqt.speedrun.study`).
* **Memory dashboard** — the FSRS-derived Memory tile (Svelte) with M3
  placeholders for Performance/Readiness.

This module is additive and fork-specific; it is wired in from
``AnkiQt.setupMenus`` with a single call to :func:`setup_speedrun_menu`.
"""

from __future__ import annotations

import aqt
import aqt.main
from aqt.qt import *
from aqt.utils import askUser, disable_help_button, restoreGeom, saveGeom, showInfo, tr
from aqt.webview import AnkiWebView, AnkiWebViewKind


def setup_speedrun_menu(mw: aqt.main.AnkiQt) -> None:
    """Add the Speedrun submenu to the Tools menu. Idempotent per window."""
    if getattr(mw, "_speedrun_menu", None) is not None:
        return
    menu = QMenu(tr.speedrun_menu(), mw)

    setup_action = menu.addAction(tr.speedrun_setup_action())
    qconnect(setup_action.triggered, lambda: run_setup(mw))

    study_action = menu.addAction(tr.speedrun_study_action())
    qconnect(study_action.triggered, lambda: open_study(mw))

    dashboard_action = menu.addAction(tr.speedrun_dashboard_action())
    qconnect(dashboard_action.triggered, lambda: open_dashboard(mw))

    mw.form.menuTools.addSeparator()
    mw.form.menuTools.addMenu(menu)
    # Keep a reference so the menu isn't garbage-collected.
    mw._speedrun_menu = menu  # type: ignore[attr-defined]


def run_setup(mw: aqt.main.AnkiQt) -> None:
    """Provision the Speedrun data model, optionally loading demo data."""
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return

    load_demo = askUser(
        tr.speedrun_setup_load_demo_prompt(),
        parent=mw,
        title=tr.speedrun_menu(),
        defaultno=False,
    )

    mw.progress.start(label=tr.speedrun_setup_action(), immediate=True)
    try:
        summary = mw.col.speedrun.setup_mcat(load_demo_data=load_demo)
    finally:
        mw.progress.finish()
    mw.reset()

    from anki.speedrun import QUESTION_NOTETYPE_NAME, QUESTIONS_DECK_NAME

    lines = [
        tr.speedrun_setup_complete(),
        "",
        tr.speedrun_setup_summary(
            notetype=QUESTION_NOTETYPE_NAME,
            deck=QUESTIONS_DECK_NAME,
            topic_count=summary.blueprint_topics,
        ),
    ]
    if summary.demo_already_present:
        lines.append(tr.speedrun_setup_demo_skipped())
    elif summary.demo_loaded:
        total_cards = (
            summary.suspended_flashcards_created + summary.studied_flashcards_created
        )
        lines.append(
            tr.speedrun_setup_demo_summary(
                question_count=summary.questions_created, card_count=total_cards
            )
        )
    showInfo("\n".join(lines), parent=mw, title=tr.speedrun_menu())


def open_study(mw: aqt.main.AnkiQt) -> None:
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return
    from aqt.speedrun.study import SpeedrunStudyDialog

    SpeedrunStudyDialog(mw)


def open_dashboard(mw: aqt.main.AnkiQt) -> None:
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return
    MemoryDashboardDialog(mw)


class MemoryDashboardDialog(QDialog):
    """Hosts the Svelte ``speedrun-dashboard`` page in a webview."""

    TITLE = "speedrunDashboard"

    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        QDialog.__init__(self, mw, Qt.WindowType.Window)
        self.mw = mw
        self.mw.garbage_collect_on_dialog_finish(self)
        self.setWindowTitle(tr.speedrun_dashboard_title())
        disable_help_button(self)
        self.setMinimumSize(560, 640)
        restoreGeom(self, self.TITLE, default_size=(720, 820))

        self.web = AnkiWebView(kind=AnkiWebViewKind.DECK_STATS)
        self.web.load_sveltekit_page("speedrun-dashboard")
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.web)
        self.setLayout(layout)
        self.show()

    def reject(self) -> None:
        saveGeom(self, self.TITLE)
        self.web.cleanup()
        self.web = None  # type: ignore[assignment]
        QDialog.reject(self)
