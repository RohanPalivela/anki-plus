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
3. **Recap** — a short capped batch of *different* served questions testing the
   SAME MATERIAL: scoped to the fine-grained ``concept::`` slugs practised in
   Phase 1 (Phase-1 items excluded), i.e. "same concepts, different phrasing".
   Concept-less questions fall back to the studied topics so recap is never
   empty. Recap NEVER unlocks new cards: unlike Practice, a wrong recap answer
   does not offer the miss-reason chooser and triggers no activation — it only
   reveals the explanation. We collect per-concept accuracy for a (deferred)
   transfer score, then return home with a refreshed Memory snapshot + summary.

Empty phases are skipped with a short note. The recap is treated purely as
feedback/measurement: it never feeds the Memory score (Memory reads FSRS state
of activated cards, which this controller does not touch), never activates
cards, and its transfer score is scaffolded but not surfaced yet (M-future).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import aqt
import aqt.main
from anki.cards import Card
from anki.decks import DeckId
from anki.notes import NoteId
from anki.speedrun import (
    FLASHCARDS_DECK_NAME,
    SESSION_SCOPE_CONFIG_KEY,
    SESSION_STATE_CONFIG_KEY,
    RecapScore,
    compute_recap_score,
)
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
        self,
        mw: aqt.main.AnkiQt,
        *,
        state: dict | None = None,
        scope_topic: str | None = None,
        scope_concept: str | None = None,
    ) -> None:
        self.mw = mw
        self.caps = mw.col.speedrun.session_caps()

        # Optional curriculum scope: a chosen topic and/or concept restricts
        # Practice (and, transitively via practised concepts, Recap) to that
        # material. Persisted in the session state so a paused scoped session
        # resumes scoped. When both are None this is the top-level smart Start,
        # which targets weak/under-covered concepts (see ``_enter_phase1``).
        self.scope_topic = scope_topic
        self.scope_concept = scope_concept

        # 0 = idle/fresh, 1/2/3 = current phase.
        self._phase = 0
        self._dialog: SpeedrunStudyDialog | None = None
        self._phase2_hooks_connected = False
        self._finished = False
        self._paused = False
        # Whether this session has already auto-run its coverage sweep (once per
        # session, at the Practice→Flashcards hand-off). Persisted so a resumed
        # session doesn't sweep again on every Start.
        self._swept = False

        # The two question phases persist both their (fixed) question list and
        # the resume position; the flashcard phase persists only the phase (its
        # position is owned by Anki's scheduler).
        self.practice_ids: list[NoteId] = []
        self.practice_index = 0
        self.recap_ids: list[NoteId] = []
        self.recap_index = 0

        # Aggregate tracking for the closing summary.
        self.studied_topics: set[str] = set()
        # Fine-grained concepts practised in Phase 1 — the concrete material the
        # student just studied. Recap is scoped to these so it re-tests the same
        # material with different phrasing (see ``_enter_phase3``).
        self.practiced_concepts: set[str] = set()
        self.missed_topics: set[str] = set()
        self.practice_shown: set[int] = set()
        self.practice_answered = 0
        self.practice_correct = 0
        self.recap_answered = 0
        self.recap_correct = 0
        # Per-concept recap tallies (concept slug -> count; "" = no-concept
        # bucket) persisted for the deferred, off-UI recap transfer score.
        self.recap_concept_answered: dict[str, int] = {}
        self.recap_concept_correct: dict[str, int] = {}
        self.flashcards_reviewed = 0
        self.activated_total = 0

        if state:
            self._load_state(state)

    # Persistence
    ##########################################################################

    def _load_state(self, state: dict) -> None:
        self._phase = int(state.get("phase", 0))
        # A persisted scope wins over a freshly requested one so a resumed
        # session keeps the scope it was started with.
        self.scope_topic = state.get("scope_topic") or self.scope_topic
        self.scope_concept = state.get("scope_concept") or self.scope_concept
        self.practice_ids = [NoteId(x) for x in state.get("practice_ids", [])]
        self.practice_index = int(state.get("practice_index", 0))
        self.recap_ids = [NoteId(x) for x in state.get("recap_ids", [])]
        self.recap_index = int(state.get("recap_index", 0))
        self.studied_topics = set(state.get("studied_topics", []))
        self.practiced_concepts = set(state.get("practiced_concepts", []))
        self.missed_topics = set(state.get("missed_topics", []))
        self.practice_shown = {int(x) for x in state.get("practice_shown", [])}
        self.practice_answered = int(state.get("practice_answered", 0))
        self.practice_correct = int(state.get("practice_correct", 0))
        self.recap_answered = int(state.get("recap_answered", 0))
        self.recap_correct = int(state.get("recap_correct", 0))
        self.recap_concept_answered = {
            str(k): int(v)
            for k, v in dict(state.get("recap_concept_answered", {})).items()
        }
        self.recap_concept_correct = {
            str(k): int(v)
            for k, v in dict(state.get("recap_concept_correct", {})).items()
        }
        self.flashcards_reviewed = int(state.get("flashcards_reviewed", 0))
        self.activated_total = int(state.get("activated_total", 0))
        self._swept = bool(state.get("swept", False))

    def _save_state(self) -> None:
        self.mw.col.set_config(
            _STATE_KEY,
            {
                "phase": self._phase,
                "scope_topic": self.scope_topic,
                "scope_concept": self.scope_concept,
                "practice_ids": [int(x) for x in self.practice_ids],
                "practice_index": self.practice_index,
                "recap_ids": [int(x) for x in self.recap_ids],
                "recap_index": self.recap_index,
                "studied_topics": sorted(self.studied_topics),
                "practiced_concepts": sorted(self.practiced_concepts),
                "missed_topics": sorted(self.missed_topics),
                "practice_shown": sorted(self.practice_shown),
                "practice_answered": self.practice_answered,
                "practice_correct": self.practice_correct,
                "recap_answered": self.recap_answered,
                "recap_correct": self.recap_correct,
                "recap_concept_answered": self.recap_concept_answered,
                "recap_concept_correct": self.recap_concept_correct,
                "flashcards_reviewed": self.flashcards_reviewed,
                "activated_total": self.activated_total,
                "swept": self._swept,
            },
        )

    def _clear_state(self) -> None:
        if self.mw.col.get_config(_STATE_KEY, None) is not None:
            self.mw.col.remove_config(_STATE_KEY)

    def _prune_stale_questions(self) -> None:
        """Drop persisted question ids that no longer resolve to a served
        question, rebasing the resume indices onto the survivors.

        A paused session persists ``practice_ids``/``recap_ids`` in the synced
        ``speedrunSessionState``. If those ids become dangling — the bank was
        re-imported with fresh note ids, or the state arrived from another
        device whose ids differ — feeding them to the study dialog crashes it
        with "No such note" (fetched via ``get_note`` in ``_load_question``).
        We prune the dangling ids; if the entire persisted batch is stale (a
        cross-collection state), we discard the session so ``start`` re-enters
        phase 1 fresh rather than resuming dead progress. Fresh sessions (no
        persisted ids) are left untouched — the phase builders make their own
        lists.
        """
        if not self.practice_ids and not self.recap_ids:
            return
        valid = set(self.mw.col.speedrun.served_question_note_ids())
        practice, practice_index = self._prune_list(
            self.practice_ids, self.practice_index, valid
        )
        recap, recap_index = self._prune_list(self.recap_ids, self.recap_index, valid)
        if not practice and not recap:
            # Nothing survived: reset to a clean slate so start() re-enters
            # phase 1 with a freshly served batch.
            self._phase = 0
            self.practice_ids = []
            self.practice_index = 0
            self.recap_ids = []
            self.recap_index = 0
            self.studied_topics = set()
            self.practiced_concepts = set()
            self.missed_topics = set()
            self.practice_shown = set()
            self.practice_answered = 0
            self.practice_correct = 0
            self.recap_answered = 0
            self.recap_correct = 0
            self.recap_concept_answered = {}
            self.recap_concept_correct = {}
            self.flashcards_reviewed = 0
            self.activated_total = 0
            self._swept = False
            return
        self.practice_ids = practice
        self.practice_index = practice_index
        self.recap_ids = recap
        self.recap_index = recap_index

    @staticmethod
    def _prune_list(
        ids: list[NoteId], index: int, valid: set[NoteId]
    ) -> tuple[list[NoteId], int]:
        """Keep only ids in ``valid``, rebasing ``index`` onto the surviving
        prefix (shifted left by however many removed ids preceded it)."""
        kept: list[NoteId] = []
        new_index = 0
        for i, nid in enumerate(ids):
            if nid in valid:
                if i < index:
                    new_index += 1
                kept.append(nid)
        return kept, new_index

    # Lifecycle
    ##########################################################################

    def start(self) -> None:
        if not self.mw.col:
            return
        if getattr(self.mw, "_speedrun_session", None) is not None:
            # A session is already running; ignore re-entry.
            return
        self.mw._speedrun_session = self  # type: ignore[attr-defined]
        # Guard a resumed session against question ids that no longer resolve
        # (bank re-imported with fresh ids, or state synced from another
        # device). Must run before we dispatch into a phase — a stale id would
        # otherwise crash the study dialog on load.
        self._prune_stale_questions()
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
            self.practice_ids = self._build_practice_ids()
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

    def _build_practice_ids(self) -> list[NoteId]:
        """The (capped) Phase-1 question list, honouring any curriculum scope.

        * Concept-scoped: that concept's served questions, falling back to the
          topic when the concept is too sparse to fill the batch.
        * Topic-scoped: that topic's served questions.
        * Unscoped (top-level Start): bias toward weak/under-covered concepts
          (with a concept-less topic fallback), and if nothing qualifies just
          serve the whole pool. ``unseen_first`` keeps a fresh batch each run.
        """
        speedrun = self.mw.col.speedrun
        cap = self.caps.practice
        if self.scope_concept:
            topics = {self.scope_topic} if self.scope_topic else None
            ids = speedrun.served_questions_interleaved(
                topics=topics, concepts={self.scope_concept}, unseen_first=True
            )
            if not ids and self.scope_topic:
                # Concept too sparse: fall back to the whole topic.
                ids = speedrun.served_questions_interleaved(
                    topics={self.scope_topic}, unseen_first=True
                )
            return list(ids[:cap])
        if self.scope_topic:
            return list(
                speedrun.served_questions_interleaved(
                    topics={self.scope_topic}, unseen_first=True
                )[:cap]
            )
        # Unscoped smart Start: target weak/under-covered concepts first.
        weak = set(speedrun.weak_concepts())
        if weak:
            ids = speedrun.served_questions_interleaved(
                concepts=weak, unseen_first=True
            )
            if ids:
                return list(ids[:cap])
        return list(speedrun.served_questions_interleaved(unseen_first=True)[:cap])

    def _on_phase1_finish(self, result: StudyPhaseResult) -> None:
        self._dialog = None
        self.studied_topics |= result.involved_topics
        self.practiced_concepts |= result.involved_concepts
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
        # Auto-run the coverage sweep once per session at the Practice→Flashcards
        # hand-off, so activation coverage grows across all blueprint topics
        # without the student ever pressing "Run sweep". Newly re-activated cards
        # then become reviewable in this very phase.
        self._maybe_run_coverage_sweep()
        deck_id = self.mw.col.decks.id_for_name(FLASHCARDS_DECK_NAME)
        if deck_id is None:
            self._goto(self._enter_phase3)
            return
        self._connect_phase2_hooks()
        set_current_deck(parent=self.mw, deck_id=DeckId(deck_id)).success(
            lambda _: self._begin_flashcard_review()
        ).run_in_background()

    def _maybe_run_coverage_sweep(self) -> None:
        """Run the coverage sweep exactly once per session (idempotent on
        resume). Uses the configured default sample size and folds the count
        into the session tally so the summary reflects it.

        Skipped for a scoped (topic/concept) session: the sweep re-activates a
        spread across ALL blueprint topics, which would pull unrelated cards
        into a focused concept session's flashcard phase. In a scoped session
        the concept's own cards were already activated by the Phase-1 misses.
        """
        if self._swept:
            return
        if self.scope_topic or self.scope_concept:
            self._swept = True
            return
        try:
            resp = self.mw.col.speedrun.run_coverage_sweep()
        except Exception:
            # A sweep failure must never block the flashcard phase. Leave
            # `_swept` False and unpersisted so a transient failure retries on
            # the next entry/launch instead of being skipped forever (parity
            # with Android's maybeRunCoverageSweep).
            return
        self._swept = True
        self.activated_total += len(resp.activated_card_ids)
        self._save_state()

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
                # Recap tests the SAME MATERIAL as Phase 1: scope to the
                # concepts just practised (distinct questions of those concepts
                # = "same material, different phrasing"), excluding the exact
                # Phase-1 items. Concept-less questions fall back to the studied
                # topics inside served_questions_interleaved, so recap is never
                # empty when concept tags are sparse. unseen_first keeps the
                # transfer check on fresh questions rather than replaying the
                # same front-of-pool items each run.
                #
                # NOTE: true paraphrase VARIANTS (rewording the very same item)
                # are a future content enhancement; concept-matched distinct
                # items are today's best "same material, different phrasing".
                recap_ids = self.mw.col.speedrun.served_questions_interleaved(
                    topics=self.studied_topics,
                    concepts=self.practiced_concepts or None,
                    exclude=set(self.practice_shown),
                    unseen_first=True,
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
        # Recap re-tests the SAME material and MUST NOT activate/unlock cards, so
        # nothing is folded into ``activated_total`` here (the study surface also
        # never offers the miss chooser in recap, so result.activated_total is 0).
        # Accumulate per-concept tallies for the deferred recap transfer score.
        for concept, count in result.concept_answered.items():
            self.recap_concept_answered[concept] = (
                self.recap_concept_answered.get(concept, 0) + count
            )
        for concept, count in result.concept_correct.items():
            self.recap_concept_correct[concept] = (
                self.recap_concept_correct.get(concept, 0) + count
            )
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
        # TODO(M-future): once recap scoring is un-deferred, surface
        # ``self.recap_score()`` here (overall transfer % + weakest concepts).
        # Deliberately NOT shown yet — see ``recap_score``.
        tooltip("<br>".join(lines), period=7000, parent=self.mw)

    def recap_score(self) -> RecapScore:
        """Compute the (deferred, off-UI) recap transfer score from the
        per-concept tallies collected this session.

        HOOK ONLY — intentionally not surfaced anywhere yet (the user asked to
        defer scoring until the recap-selection / no-activation changes are in
        place). The data plumbing is complete: this returns the overall + per
        -concept recap accuracy so a future milestone (M-future) can display it
        in the session summary / home / dashboard without more plumbing. See
        :func:`anki.speedrun.compute_recap_score` for the (simple, upgradeable)
        formula.
        """
        return compute_recap_score(
            self.recap_concept_answered, self.recap_concept_correct
        )


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
    # A pending curriculum scope only applies to a FRESH session; a paused
    # session always resumes with the scope it was started with. Consume (clear)
    # the pending scope either way so it never lingers onto a later Start.
    scope = mw.col.get_config(SESSION_SCOPE_CONFIG_KEY, None)
    if scope is not None:
        mw.col.remove_config(SESSION_SCOPE_CONFIG_KEY)
    scope_topic = scope_concept = None
    if state is None and isinstance(scope, dict):
        scope_topic = scope.get("topic") or None
        scope_concept = scope.get("concept") or None
    SpeedrunSession(
        mw, state=state, scope_topic=scope_topic, scope_concept=scope_concept
    ).start()
