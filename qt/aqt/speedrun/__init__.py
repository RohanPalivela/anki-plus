# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun (MCAT fork) desktop entry points.

Adds a Tools → "Speedrun (MCAT)" submenu wiring up these actions:

* **Set up Speedrun (MCAT)** — runtime data-model provisioning (D-13); also
  purges any leftover synthetic demo data from older builds.
* **Study (question-first)** — the SPOV3 gating loop (see :mod:`aqt.speedrun.study`).
  Gated on the question bank having been imported first.
* **Memory dashboard** — the FSRS-derived Memory tile (Svelte) with M3
  placeholders for Performance/Readiness.
* **Import question bank** — one-time import of the vendored, legally reusable
  MCAT-relevant question bank as native notes (which then sync to every device).
  This is the *only* source of practice questions; it also imports the linked
  first-principles memory cards (suspended) that a missed question activates.
* **Enable AI rewording** — a checkable, synced master switch (off by default)
  for the grounded AI features. The three scores are produced with it off.
* **Generate AI flashcard variants** — reword the first-principles memory cards
  into grounded, source-checked variants (see :mod:`anki.speedrun_rephrase`);
  gated on the switch and on an OpenAI key/library being available.
* **Delete AI flashcard variants** — the inverse: remove every
  ``bank::ai-generated`` variant in one undoable step. Not gated on the switch,
  so variants can be cleaned up after AI is turned off.

This module is additive and fork-specific; it is wired in from
``AnkiQt.setupMenus`` with a single call to :func:`setup_speedrun_menu`.
"""

from __future__ import annotations

from typing import Any

import aqt
import aqt.main
import aqt.toolbar
from anki.collection import OpChanges
from aqt import gui_hooks
from aqt.qt import *
from aqt.utils import (
    askUser,
    disable_help_button,
    restoreGeom,
    saveGeom,
    showInfo,
    tooltip,
    tr,
)
from aqt.webview import AnkiWebView, AnkiWebViewKind

#: Guard so the global toolbar hook is only registered once.
_toolbar_hook_added = False


def setup_speedrun_menu(mw: aqt.main.AnkiQt) -> None:
    """Add the Speedrun submenu to the Tools menu. Idempotent per window."""
    _ensure_toolbar_home_link()
    if getattr(mw, "_speedrun_menu", None) is not None:
        return
    menu = QMenu(tr.speedrun_menu(), mw)

    setup_action = menu.addAction(tr.speedrun_setup_action())
    qconnect(setup_action.triggered, lambda: run_setup(mw))

    study_action = menu.addAction(tr.speedrun_study_action())
    qconnect(study_action.triggered, lambda: open_study(mw))

    dashboard_action = menu.addAction(tr.speedrun_dashboard_action())
    qconnect(dashboard_action.triggered, lambda: open_dashboard(mw))

    menu.addSeparator()
    import_bank_action = menu.addAction(tr.speedrun_import_bank_action())
    qconnect(import_bank_action.triggered, lambda: run_import_bank(mw))

    menu.addSeparator()
    # Eval-only surfaces (brief §7d / §9). These generate the *real* held-out and
    # paraphrase revlog the validation harnesses read; plain-English labels keep
    # this fork tooling off the translated string API.
    heldout_action = menu.addAction("Study held-out questions (eval)")
    qconnect(heldout_action.triggered, lambda: open_heldout_study(mw))
    import_paraphrase_action = menu.addAction("Import paraphrase set (eval)")
    qconnect(import_paraphrase_action.triggered, lambda: run_import_paraphrase(mw))
    paraphrase_study_action = menu.addAction("Study paraphrase questions (eval)")
    qconnect(paraphrase_study_action.triggered, lambda: open_paraphrase_study(mw))

    menu.addSeparator()
    # Master AI switch (checkable) + the generation action it gates. Off by
    # default; the app scores fine with AI disabled.
    ai_toggle = menu.addAction(tr.speedrun_ai_enable_action())
    ai_toggle.setCheckable(True)
    qconnect(ai_toggle.triggered, lambda checked: set_ai_enabled(mw, checked))
    generate_cards_action = menu.addAction(tr.speedrun_ai_generate_cards_action())
    qconnect(generate_cards_action.triggered, lambda: run_generate_card_variants(mw))
    # Cleanup is the inverse of generate and intentionally NOT gated on the
    # switch: a user who turned AI off can still delete the variants it produced.
    cleanup_cards_action = menu.addAction(tr.speedrun_ai_cleanup_cards_action())
    qconnect(cleanup_cards_action.triggered, lambda: run_cleanup_card_variants(mw))

    def _sync_ai_menu() -> None:
        # Reflect the synced config each time the menu opens (it may have changed
        # via sync or on another window), and gate the generate action on it.
        enabled = bool(mw.col and mw.col.speedrun.ai_enabled())
        ai_toggle.setChecked(enabled)
        generate_cards_action.setEnabled(enabled)

    qconnect(menu.aboutToShow, _sync_ai_menu)

    mw.form.menuTools.addSeparator()
    mw.form.menuTools.addMenu(menu)
    # Keep a reference so the menu isn't garbage-collected.
    mw._speedrun_menu = menu  # type: ignore[attr-defined]


def _ensure_toolbar_home_link() -> None:
    """Register the additive top-toolbar hook that adds an MCAT "Home" link.

    Done via ``top_toolbar_did_init_links`` rather than editing
    ``aqt/toolbar.py`` so the fork stays mergeable. The standard "Decks" link is
    already present in the toolbar, so standard Anki remains reachable.
    """
    global _toolbar_hook_added
    if _toolbar_hook_added:
        return
    gui_hooks.top_toolbar_did_init_links.append(_on_top_toolbar_did_init_links)
    _toolbar_hook_added = True


def _on_top_toolbar_did_init_links(
    links: list[str], toolbar: aqt.toolbar.Toolbar
) -> None:
    home_link = toolbar.create_link(
        "speedrunHome",
        tr.speedrun_home_link(),
        lambda: toolbar.mw.moveToState("speedrun"),
        tip=tr.speedrun_home_link_tip(),
        id="speedrunHome",
    )
    links.insert(0, home_link)


def run_setup(mw: aqt.main.AnkiQt) -> None:
    """Provision the Speedrun data model and purge any leftover demo data.

    If the real question bank has not been imported yet, offer to import it now
    so the student is never left with an empty (or synthetic) practice pool.
    """
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return

    mw.progress.start(label=tr.speedrun_setup_action(), immediate=True)
    try:
        summary = mw.col.speedrun.setup_mcat()
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
    if summary.demo_notes_removed:
        lines.append(tr.speedrun_setup_demo_removed(count=summary.demo_notes_removed))
    showInfo("\n".join(lines), parent=mw, title=tr.speedrun_menu())

    # Guide the student straight into importing the bank if they have not yet.
    if not summary.bank_imported:
        ensure_bank_imported(mw)


def run_import_bank(mw: aqt.main.AnkiQt) -> bool:
    """Import the vendored real question bank as native notes (one-time; syncs).

    Idempotent — re-running only adds items not already present, so it is safe to
    run again after the bank has grown or synced from another device. Returns
    whether the collection now has served questions available.
    """
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return False

    # Setup is a prerequisite for import (notetype/decks/blueprint); running it
    # here makes "Import question bank" work as a standalone first action and
    # also purges any leftover synthetic demo data.
    mw.progress.start(label=tr.speedrun_import_bank_running(), immediate=True)
    summary = None
    fp_summary = None
    try:
        mw.col.speedrun.setup_mcat()
        summary = mw.col.speedrun.import_question_bank()
        # Also import the linked first-principles memory cards (suspended); these
        # are the gating targets a missed question activates.
        try:
            fp_summary = mw.col.speedrun.import_first_principles()
        except FileNotFoundError:
            fp_summary = None
    except FileNotFoundError:
        summary = None
    finally:
        mw.progress.finish()
    if summary is None:
        showInfo(tr.speedrun_import_bank_empty(), parent=mw, title=tr.speedrun_menu())
        return mw.col.speedrun.has_question_bank()
    mw.reset()

    lines = [
        tr.speedrun_import_bank_complete(
            imported_count=summary.imported, skipped_count=summary.skipped_existing
        ),
        tr.speedrun_import_bank_synced(),
    ]
    if summary.imported:
        sources = ", ".join(
            f"{origin} ({count})" for origin, count in sorted(summary.by_origin.items())
        )
        topics = ", ".join(
            f"{topic} ({count})" for topic, count in sorted(summary.by_topic.items())
        )
        lines.append("")
        lines.append(tr.speedrun_import_bank_breakdown(sources=sources, topics=topics))
    if fp_summary is not None and (fp_summary.imported or fp_summary.total):
        lines.append("")
        lines.append(
            tr.speedrun_import_first_principles(
                imported_count=fp_summary.imported,
                skipped_count=fp_summary.skipped_existing,
            )
        )
    showInfo("\n".join(lines), parent=mw, title=tr.speedrun_menu())
    return mw.col.speedrun.has_question_bank()


def set_ai_enabled(mw: aqt.main.AnkiQt, enabled: bool) -> None:
    """Persist the AI opt-in (synced) and confirm with a tooltip."""
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return
    mw.col.speedrun.set_ai_enabled(enabled)
    tooltip(
        tr.speedrun_ai_enabled_on() if enabled else tr.speedrun_ai_enabled_off(),
        parent=mw,
    )


def run_generate_card_variants(mw: aqt.main.AnkiQt) -> None:
    """Generate grounded, reworded flashcard variants from the first-principles
    memory cards using the configured AI provider.

    Gated three ways: the AI master switch must be on, an OpenAI key + library
    must be available (otherwise we explain how to set them up — AI stays a
    clean no-op without them), and the question bank / first-principles cards
    must be imported. Runs in the background so the network calls never freeze
    the UI; each accepted variant is written as a suspended, synced Basic note
    excluded from the Memory score.
    """
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return
    if not mw.col.speedrun.ai_enabled():
        showInfo(tr.speedrun_ai_disabled_body(), parent=mw, title=tr.speedrun_menu())
        return
    if not ensure_bank_imported(mw):
        return

    from anki.speedrun_rephrase import OpenAIProvider, generate_card_variants
    from aqt.operations import QueryOp

    if not OpenAIProvider.available():
        showInfo(tr.speedrun_ai_unavailable_body(), parent=mw, title=tr.speedrun_menu())
        return

    note_ids = mw.col.speedrun.first_principles_note_ids()
    if not note_ids:
        showInfo(tr.speedrun_ai_no_sources(), parent=mw, title=tr.speedrun_menu())
        return

    provider = OpenAIProvider()

    def on_success(summary: Any) -> None:
        mw.reset()
        lines = [
            tr.speedrun_ai_generate_complete(
                written_count=summary.written,
                considered_count=summary.considered,
                blocked_count=summary.blocked,
            )
        ]
        # Surface the buckets the headline count hides, so a run that silently
        # errored on every call (or found everything already generated) is not
        # mistaken for a no-op.
        if summary.skipped_existing:
            lines.append(
                tr.speedrun_ai_generate_skipped_note(
                    skipped_count=summary.skipped_existing
                )
            )
        if summary.provider_errors:
            lines.append(
                tr.speedrun_ai_generate_error_note(
                    error_count=summary.provider_errors,
                    first_error=summary.first_error or "unknown",
                )
            )
        showInfo("\n".join(lines), parent=mw, title=tr.speedrun_menu())

    QueryOp(
        parent=mw,
        op=lambda col: generate_card_variants(col, note_ids, provider=provider),
        success=on_success,
    ).with_progress(tr.speedrun_ai_generate_running()).run_in_background()


def run_cleanup_card_variants(mw: aqt.main.AnkiQt) -> None:
    """Delete every AI-generated flashcard variant (the inverse of generate).

    Not gated on the AI switch, since a user who turned AI off still needs a way
    to remove what it produced. Only notes tagged ``bank::ai-generated`` are
    touched — the source first-principles cards and the question bank are left
    alone — and the removal is a single, undoable, syncing operation.
    """
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return

    from anki.notes import NoteId
    from anki.speedrun_rephrase import ai_variant_note_ids
    from aqt.operations import CollectionOp

    note_ids = ai_variant_note_ids(mw.col)
    if not note_ids:
        showInfo(tr.speedrun_ai_cleanup_none(), parent=mw, title=tr.speedrun_menu())
        return
    if not askUser(
        tr.speedrun_ai_cleanup_confirm(count=len(note_ids)),
        parent=mw,
        title=tr.speedrun_menu(),
    ):
        return

    removed = len(note_ids)

    def on_success(_changes: Any) -> None:
        tooltip(tr.speedrun_ai_cleanup_complete(count=removed), parent=mw)

    def _cleanup(col: Any) -> OpChanges:
        changes = col.remove_notes([NoteId(nid) for nid in note_ids])
        # Purge the orphaned bank::/variant-of::/variantuid:: tags so a later
        # regenerate is not wrongly skipped as "already existing".
        col.tags.clear_unused_tags()
        return changes

    CollectionOp(parent=mw, op=_cleanup).success(on_success).run_in_background()


def ensure_bank_imported(mw: aqt.main.AnkiQt) -> bool:
    """Gate practice on the question bank being imported first.

    Returns True if served questions are available (either already imported or
    imported just now). If the bank is missing, prompt the student to import it
    and run the import inline; returns False only if they decline or it fails.
    """
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return False
    # Clean up any leftover synthetic demo notes so practice only ever uses the
    # imported bank, even in collections provisioned by an older build.
    mw.col.speedrun.purge_demo_data()
    if mw.col.speedrun.has_question_bank():
        # Collections that imported the bank before linked memory cards existed
        # would have nothing to activate; top them up idempotently so the gating
        # loop works. Cheap no-op once present.
        if not mw.col.speedrun.has_first_principles():
            try:
                mw.col.speedrun.import_first_principles()
            except FileNotFoundError:
                pass
        # Back-fill precise gates:: links for collections imported before gated
        # linkage existed (once per collection; cheap no-op afterwards).
        mw.col.speedrun.ensure_gates_linked()
        return True
    if not askUser(
        tr.speedrun_bank_required_body(),
        parent=mw,
        title=tr.speedrun_bank_required_title(),
        defaultno=False,
    ):
        return False
    return run_import_bank(mw)


def open_study(mw: aqt.main.AnkiQt) -> None:
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return
    if not ensure_bank_imported(mw):
        return
    from aqt.speedrun.study import SpeedrunStudyDialog

    SpeedrunStudyDialog(mw)


#: How many held-out questions to present per eval sitting. The pool is large
#: (~1 in N of the bank); a random sample of this size gives the Performance
#: validation harness enough spread across difficulty for a stable AUC without
#: forcing the grader to sit through the whole held-out split.
_HELDOUT_EVAL_BATCH = 60


def open_heldout_study(mw: aqt.main.AnkiQt) -> None:
    """Answer a random batch of held-out questions, auto-graded into revlog.

    Held-out questions are never served by normal study (they are the evaluation
    split), so this is the only in-app way to produce the real held-out answers
    ``just speedrun-validate`` reads for the Performance accuracy/AUC metric. No
    cards are activated (MODE_HELDOUT), so answering them can't leak into the
    served-pool models.
    """
    import random

    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return
    if not ensure_bank_imported(mw):
        return
    note_ids = mw.col.speedrun.heldout_question_note_ids()
    if not note_ids:
        showInfo(
            "No held-out questions found. Import the question bank first "
            "(Tools ▸ Speedrun ▸ Import question bank).",
            parent=mw,
            title=tr.speedrun_menu(),
        )
        return
    if len(note_ids) > _HELDOUT_EVAL_BATCH:
        note_ids = random.sample(note_ids, _HELDOUT_EVAL_BATCH)

    from aqt.speedrun.study import MODE_HELDOUT, SpeedrunStudyDialog

    SpeedrunStudyDialog(
        mw,
        note_ids=note_ids,
        mode=MODE_HELDOUT,
        title="Held-out questions (eval)",
        allow_sweep=False,
    )


def run_import_paraphrase(mw: aqt.main.AnkiQt) -> None:
    """Import the eval-only paraphrase transfer set as native notes.

    Creates 30 memory *lessons* (``Speedrun::Paraphrase::Lessons``) and their 60
    reworded *questions* (``Speedrun::Paraphrase::Questions``). Idempotent, so
    re-running only adds what's missing.
    """
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return
    mw.progress.start(label="Importing paraphrase set…", immediate=True)
    summary = None
    try:
        summary = mw.col.speedrun.import_paraphrase_set()
    except FileNotFoundError:
        summary = None
    finally:
        mw.progress.finish()
    if summary is None:
        showInfo(
            "Paraphrase set data not found.", parent=mw, title=tr.speedrun_menu()
        )
        return
    mw.reset()
    showInfo(
        "Imported paraphrase set: "
        f"{summary.lessons_added} lessons + {summary.questions_added} reworded "
        f"questions ({summary.skipped_existing} already present).\n\n"
        "Next: study the lessons in 'Speedrun::Paraphrase::Lessons' (normal "
        "reviewer) to build recall, answer the reworded questions via "
        "Tools ▸ Speedrun ▸ Study paraphrase questions (eval), then run "
        "'just speedrun-paraphrase --collection <copy>'.",
        parent=mw,
        title=tr.speedrun_menu(),
    )


def open_paraphrase_study(mw: aqt.main.AnkiQt) -> None:
    """Answer the reworded paraphrase questions, auto-graded into revlog.

    Feeds the reworded-accuracy side of the paraphrase transfer test. Like the
    held-out surface it activates no cards (MODE_HELDOUT)."""
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return
    if not mw.col.speedrun.has_paraphrase_set():
        showInfo(
            "The paraphrase set isn't imported yet. Run "
            "Tools ▸ Speedrun ▸ Import paraphrase set (eval) first.",
            parent=mw,
            title=tr.speedrun_menu(),
        )
        return
    note_ids = mw.col.speedrun.paraphrase_question_note_ids()
    if not note_ids:
        showInfo(
            "No paraphrase questions found.", parent=mw, title=tr.speedrun_menu()
        )
        return

    from aqt.speedrun.study import MODE_HELDOUT, SpeedrunStudyDialog

    SpeedrunStudyDialog(
        mw,
        note_ids=note_ids,
        mode=MODE_HELDOUT,
        title="Paraphrase questions (eval)",
        allow_sweep=False,
    )


def fsrs_enabled(mw: aqt.main.AnkiQt) -> bool:
    """Whether FSRS scheduling is on. The Memory model is built entirely on FSRS
    retrievability, so with FSRS off no card ever gains the memory state it
    reads and the dashboard can only abstain."""
    return bool(mw.col.get_config("fsrs"))


def open_dashboard(mw: aqt.main.AnkiQt) -> None:
    if not mw.col:
        showInfo(tr.speedrun_setup_no_collection(), parent=mw)
        return
    if not fsrs_enabled(mw):
        # Warn but still open, so the empty/abstaining state is explained rather
        # than looking broken.
        showInfo(
            tr.speedrun_dashboard_fsrs_off(),
            parent=mw,
            title=tr.speedrun_dashboard_title(),
        )
    MemoryDashboardDialog(mw)


class MemoryDashboardDialog(QDialog):
    """Hosts the Svelte ``speedrun-dashboard`` page in a webview.

    The dialog lives in its own window, so it can be open while the user reviews
    in the main window. It listens for operations that change FSRS state and
    re-fetches the Memory snapshot, so it stays live instead of showing stale
    numbers until manually refreshed.
    """

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
        self.web.set_bridge_command(self._on_bridge_cmd, self)
        self.web.load_sveltekit_page("speedrun-dashboard")
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.web)
        self.setLayout(layout)
        gui_hooks.operation_did_execute.append(self._on_operation_did_execute)
        self.show()

    def _on_bridge_cmd(self, cmd: str) -> Any:
        if cmd == "reset-profile":
            self._reset_profile()
        return False

    def _reset_profile(self) -> None:
        """Confirm, then clear the learner's progress (keeping imported content)
        and refresh the dashboard + home so the fresh state shows immediately."""
        if not askUser(
            tr.speedrun_reset_profile_confirm(),
            parent=self,
            title=tr.speedrun_reset_profile_action(),
            defaultno=True,
        ):
            return
        summary = self.mw.col.speedrun.reset_profile()
        # The reset uses direct backend ops (which don't fire the aqt operation
        # hook), so reload the snapshot explicitly here.
        self.web.load_sveltekit_page("speedrun-dashboard")
        home = self.mw.speedrun_home
        if home is not None and self.mw.state == "speedrun":
            home.refresh()
        tooltip(
            tr.speedrun_reset_profile_done(
                resuspended_count=summary.cards_resuspended,
                forgotten_count=summary.cards_forgotten,
            ),
            parent=self,
        )

    def _on_operation_did_execute(
        self, changes: OpChanges, handler: object | None
    ) -> None:
        # Reviews and card activation change the FSRS state the Memory model
        # reads; reload so the dashboard reflects a just-finished session.
        if changes.study_queues or changes.card:
            self.web.load_sveltekit_page("speedrun-dashboard")

    def reject(self) -> None:
        gui_hooks.operation_did_execute.remove(self._on_operation_did_execute)
        saveGeom(self, self.TITLE)
        self.web.cleanup()
        self.web = None  # type: ignore[assignment]
        QDialog.reject(self)
