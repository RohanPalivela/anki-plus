# Anki → MCAT "Speedrun" — Technical Implementation Plan

> **Status:** Draft for review → human approval → agent-team execution.
> **Scope of this document:** a buildable, file-level technical plan. It does **not** contain production code.
> **Source of truth for _what_ to build:** `Anki_Plan.md`. **Source of truth for _how the repo works_:** `CODEBASE_PRIMER.md`, `CLAUDE.md`/`AGENTS.md`, `docs/architecture.md`, `docs/language_bridge.md`, `docs/protobuf.md`.
> **Locked decisions (not re-litigated):** MCAT only (scale 472–528; 4 sections 118–132); Android-only mobile via AnkiDroid (iOS out of scope); all custom data stored as **native Anki objects** (notes/notetypes/cards/tags/`revlog`/config) — never a side SQLite table.

## Table of contents

1. [Executive summary](#1-executive-summary)
2. [Architecture & native data model](#2-architecture--native-data-model)
3. [Per-milestone, per-feature work breakdown](#3-per-milestone-per-feature-work-breakdown)
   - [M0 — Foundation](#m0--foundation)
   - [M1 — Engine change + Memory model + installs](#m1--engine-change--memory-model--installs)
     - [M1 1a — Question-Gated Card Activation (marquee Rust change)](#m1-1a--question-gated-card-activation-the-marquee-rust-engine-change)
     - [M1 1b — Memory model](#m1-1b--memory-model-running)
     - [M1 1c — Installs + Android](#m1-1c--installs--android)
   - [M2 — AI + sync + question/response pipeline](#m2--ai--sync--questionresponse-pipeline)
   - [M3 — Performance + Readiness models](#m3--performance--readiness-models)
   - [M4 — Validation, ablation, packaging, hand-in](#m4--validation-ablation-packaging-hand-in)
4. [Dependency-ordered task DAG](#4-dependency-ordered-task-dag)
5. [Suggested agent/team assignment & hand-off contracts](#5-suggested-agentteam-assignment--hand-off-contracts)
6. [Risk register](#6-risk-register)
7. [Open Decisions for Human Approval](#7-open-decisions-for-human-approval)
8. [Validation / exit criteria per gate](#8-validation--exit-criteria-per-gate)

---

## 1. Executive summary

**What we are building.** "Speedrun" is a fork of Anki for the **MCAT** that inverts the normal flashcard loop. Instead of cards-first, **practice questions drive the flashcards (SPOV3)**: every flashcard starts **suspended**; a student answers practice questions first, and only when they _miss_ a question **and classify the miss as a memory problem** do the linked flashcards get **activated** (unsuspended) into the FSRS review queue. The app produces three honest, evidence-backed predictions — **Memory**, **Performance**, **Readiness** — each with a point estimate, an uncertainty range, a coverage figure, a confidence label, and an explicit **abstention** rule.

**The central gate (a real Rust-core change).** The marquee deliverable is **Question-Gated Card Activation** (§M1 1a): a new Rust engine capability that (a) resolves a missed question's linked cards and **atomically, undo-safely unsuspends** them only for qualifying miss-reasons, (b) re-orders the activated-card review queue by **value = `topic_weight × weakness`**, and (c) runs a **coverage sweep** that re-activates a spread across all blueprint topics. It is implemented in Rust (not Python) because state transitions must be atomic + undo-safe, fast on 50k-card decks, and **shared with Android** through the same backend. It explicitly **does not** touch FSRS intervals/due dates — it governs _activation + ordering_, not spacing.

**Why native objects.** Questions are modeled as a `SpeedrunQuestion` notetype; topic/pool/miss labels are **tags**; a question attempt is a native **`revlog`** review; flashcards are normal cards that begin suspended. Consequently the served/held-out split is a tag filter, coverage is a tag query, the leakage check is a tag/text scan, and activation is a deterministic function of (missed question → linked cards). **Anki's existing object-based sync then carries all of it to Android for free** — no sync rewrite.

**Milestone arc.**

- **M0 Foundation** — fork, build desktop from source, land a trivial Rust→protobuf→UI change, get the shared engine running on Android.
- **M1** — the gating engine change (1a), the Memory model from FSRS retrievability (1b), desktop installer + Android review (1c). **No AI.**
- **M2** — grounded AI card generation with an eval gate + leakage check + AI-off path (2a); the question-first study surface with miss-reason classification → gated activation + coverage sweep (2b); reuse Anki sync for two-way offline reconcile + Android scores (2c).
- **M3** — Performance model (`P(correct)` on a _new_ question, with a real memory→performance gap) (3a) and Readiness model (Monte-Carlo exam simulation → scaled score + interval + abstention + progress surface) (3b).
- **M4** — held-out validation of all three models, a learning-science ablation (interleaving), robustness/packaging/benchmark, and hand-in deliverables.

**Critical path:** **M0 → M1 1a → (everything else).** Nearly all study, scoring, and UI work depends on the gating engine and the native data model landing first.

---

## 2. Architecture & native data model

### 2.1 Layer map (verified against the tree)

| Layer       | Language             | Location                                      | Role in Speedrun                                                        |
| ----------- | -------------------- | --------------------------------------------- | ----------------------------------------------------------------------- |
| Core        | Rust                 | `rslib/`                                      | Gating engine, value-ordering, mastery/score computation, sync (reused) |
| Bridge      | Rust/PyO3            | `pylib/rsbridge/`                             | Exposes Rust API to Python                                              |
| Py lib      | Python               | `pylib/anki/`                                 | Helpers wrapping `col._backend.*`; question/AI orchestration on desktop |
| Desktop GUI | Python/PyQt          | `qt/aqt/`                                     | Reviewer, dashboards, AI flow, installer                                |
| Web UI      | Svelte/TS            | `ts/`                                         | Question-first surface, three-score dashboard, progress view            |
| IPC schema  | Protobuf             | `proto/anki/`                                 | Cross-language contract; codegen into `out/`                            |
| Android     | Kotlin + shared Rust | **AnkiDroid repo (external)** + `rslib/` here | Consumes the same backend + new RPCs                                    |

**RPC routing rule (binding):** an RPC declared in `FrontendService` (`proto/anki/frontend.proto`) is implemented in **Python** (handled in `qt/aqt/`, usually via `qt/aqt/mediasrv.py`); an RPC in **any other** service is implemented in **Rust** (`rslib/src/<area>/service.rs`). Services are auto-discovered from the descriptor pool in `rslib/proto_gen/src/lib.rs::get_services`, which **requires every `FooService` to have a paired `BackendFooService`** (may be empty) — `proto/**/*.proto` is auto-globbed by `build/configure/src/web.rs`, so a new proto file is picked up without hand-editing a file list. **`.proto` edits require a full `just check`/`just build`** to regenerate bindings in `out/` (a `cargo check` alone will not see them).

### 2.2 Native-object schema (the heart of the design)

All custom state is a native Anki object so that **sync, undo, and stats come for free**.

**(a) `SpeedrunQuestion` notetype** — a normal note type (provisioned at runtime; see Decision D-13). Fields, in order:

| Field              | Purpose                                                 | Used by                |
| ------------------ | ------------------------------------------------------- | ---------------------- |
| `stem`             | question prompt                                         | question surface (2b)  |
| `options`          | answer options (one per line / JSON list)               | question surface       |
| `correct`          | correct option key                                      | grading → `revlog`     |
| `explanation`      | post-answer rationale                                   | shown on miss          |
| `source`           | named origin (chapter/page); empty ⇒ AI output rejected | AI eval/leakage (2a)   |
| `difficulty_b`     | IRT difficulty `b`                                      | Performance model (3a) |
| `discrimination_a` | IRT discrimination `a`                                  | Performance model (3a) |

Its single card template lives in a dedicated deck **`Speedrun::Questions`**. (Reference notetype-construction primitives: `rslib/src/notetype/stock.rs` → `empty_stock`, `Notetype::add_field`, `Notetype::add_template`; Python clone path: `pylib/anki/models.py`, `pylib/anki/stdmodels.py`.)

**(b) Tag taxonomy** (tags are on notes; they sync — `proto/anki/tags.proto`, `rslib/src/tags/`):

- `topic::<name>` — one per blueprint topic the item tests. Applied to **both** question notes and the flashcard notes they can activate. This shared tag is the **default question↔card link**.
- `pool::served` | `pool::heldout` — the evaluation split (held-out is reserved for M4; never served).
- `miss::knowledge-gap` | `miss::missing-context` | `miss::misunderstanding` | `miss::careless` — the miss-reason. Only `knowledge-gap` and `missing-context` trigger activation.
- `gates::<note_id>` _(optional, precise linkage)_ — explicit "this question can activate flashcard note `<note_id>`" reference, for when shared-topic linkage is too coarse (Decision D-2a).

**(c) Question attempt → `revlog` review.** Answering a served question is a **native review** (`rslib/src/scheduler/answering/`, `rslib/src/revlog/`): **`Again`=incorrect, `Good`=correct**, with response time and timestamp captured in the `revlog` row (`RevlogEntry.taken_millis`, `RevlogEntry.id` = ms timestamp). This syncs automatically.

**(d) Miss-reason persistence (discovered nuance → Decision D-11).** Per-attempt **correctness + response time + timestamp is already native** in `revlog` (`RevlogEntry`, `rslib/src/revlog/mod.rs:36`) and syncs automatically — so attempt _history_ needs no new store. The only thing lacking a native home is the **per-event miss-reason** label: `revlog` rows carry no tag/freeform field, and tags attach to **notes**, not to individual review events. We therefore keep the **latest** reason as a queryable `miss::<reason>` **tag on the question note** (coverage / "why-missed" become a normal tag search). This is sufficient for the gating acceptance criteria, because activation consumes the _current_ miss event's reason **directly from the RPC argument** and never reads reason history back from storage. **`card.custom_data` is deliberately NOT used for this:** `validate_custom_data` (`rslib/src/storage/card/data.rs:135-149`) requires a JSON **object** with **keys ≤ 8 bytes** and **total serialized < 100 bytes**, a budget already **shared with FSRS** (which writes keys there on reviewed cards) — it cannot hold per-attempt records. _If_ full per-event reason history is later required, the proven native fit is one **`SpeedrunMissEvent` note per miss** (fields `question_nid`, `reason`, `ts`): note fields are arbitrary-length and sync natively, so history scales without a side table (see D-11).

**(e) Flashcards** stay normal cards but **start suspended** (`CardQueue::Suspended`). Memory reads FSRS state (`Card.memory_state`) from **activated** cards only.

**(f) MCAT blueprint** (topic list + per-section/topic weights) — stored natively as collection config (extend `StringKey`/JSON config in `rslib/src/config/string.rs`, persisted via `Collection::set_config`) so it syncs to Android. Feeds value-ordering, coverage, and readiness simulation. (Source/licensing = Decision D-3 / D-16.)

### 2.3 How each derived quantity is computed from native objects

| Quantity                   | Definition (native)                                                                                     | Mechanism                                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Served / held-out          | `pool::served` vs `pool::heldout` tag filter                                                            | `SearchNode::Tag` (enum def `rslib/src/search/parser.rs:96`; constructor helpers in `rslib/src/search/builder.rs`) |
| Linked cards of a question | flashcard notes sharing the question's `topic::*` tags and/or its `gates::<nid>` refs                   | search: `note:<flashcard-types> (tag:topic::X or nid:…)`                                                           |
| Coverage (SPOV3)           | fraction of blueprint topics _exercised by questions_ (answered correctly, or missed→activated→studied) | tag query over question `revlog` + blueprint config                                                                |
| Leakage                    | any `pool::heldout` item or near-duplicate present in AI generation/training inputs                     | tag/text scan (2a)                                                                                                 |
| Activation                 | deterministic fn(missed question, miss-reason) → linked card ids unsuspended (qualifying reasons only)  | **M1 1a RPC**                                                                                                      |
| Memory (per card)          | FSRS `R(t)` from `Card.memory_state`                                                                    | `fsrs.current_retrievability_seconds(...)` (1b)                                                                    |
| Topic mastery              | stability-weighted mean `R(t)` over activated cards with `topic::X`                                     | aggregation (1b)                                                                                                   |

---

## 3. Per-milestone, per-feature work breakdown

> Conventions below: **(exists)** = path verified in the tree; **(NEW)** = file/symbol to be created. "Merge risk" rates how hard a change is to keep mergeable against upstream Anki (low = new/additive file; med = additive insertion into an upstream file; high = rewriting upstream logic).

### M0 — Foundation

**Objective.** Prove the full toolchain end-to-end before any features: a clean from-source build of desktop, a trivial Rust→protobuf→Python→UI change, and the same Rust engine running on Android.

**Acceptance criteria (testable).**

- A1. Fresh-machine build succeeds: `just build` then `just run` launches Anki; web pages serve at `http://localhost:40000/_anki/pages/`.
- A2. `just check` passes (format + build + lint + tests) on the untouched fork.
- A3. A trivial new Rust value is visible in the desktop UI (proves Rust→protobuf→Python loop).
- A4. An AnkiDroid build that links this fork's Rust backend launches and opens a deck.

**Files to create/modify.**

- `proto/`: add **one** trivial RPC to an existing service, e.g. `rpc SpeedrunPing(generic.Empty) returns (generic.String);` in **`proto/anki/scheduler.proto`** (exists) under `SchedulerService` (Rust-implemented). _Alternative:_ introduce **`proto/anki/speedrun.proto` (NEW)** with `SpeedrunService`/`BackendSpeedrunService` now to validate new-service wiring early (recommended, ties into D-15).
- `rslib/`: implement in **`rslib/src/scheduler/service/mod.rs`** (exists) — return a constant string. If using a new service, add **`rslib/src/speedrun/service.rs` (NEW)** + `mod speedrun;` in `rslib/src/lib.rs` (exists, upstream — merge risk low: single `mod` line).
- `pylib/`: wrap in **`pylib/anki/scheduler/base.py`** (exists) or **`pylib/anki/speedrun.py` (NEW)** as `col.speedrun.ping()`.
- `qt/aqt/`: surface the value in the deck browser, e.g. a label in **`qt/aqt/deckbrowser.py`** (exists; merge risk med) or a toolbar item in **`qt/aqt/toolbar.py`** (exists).
- `README.md` (exists): state **MCAT** at the top; credit Anki; AGPL-3.0-or-later.
- Android: in the **AnkiDroid repo** (external), point its Rust backend dependency at this fork; no change needed _here_ beyond ensuring `rslib` builds for Android targets.

**Protobuf changes.** One RPC, no storage messages. Rust-implemented (not in `FrontendService`).

**Rust approach.** Trivial; the point is to exercise codegen. Run a full `just check` so bindings regenerate in `out/`.

**Python + Qt wiring.** Add a helper method; call it from a Qt screen and render the string.

**TS/Svelte UI.** None required for M0 (the trivial value can surface in Qt). Optionally echo via an existing graphs route to also prove the TS path.

**Expected result.** The constant Rust string appears in the desktop UI; Android opens a deck on the shared engine.

**Testing procedure.**

- Rust: a unit test asserting the ping string in `rslib/src/scheduler/service/mod.rs` (or `rslib/src/speedrun/service.rs`) → `just test-rust`.
- Python: assert `col.speedrun.ping()` (or equivalent) returns the value in `pylib/tests/` (NEW test file) → `just test-py`.
- Full gate: `just check`.
- Manual: `just run`, observe value; AnkiDroid debug build opens a deck (A4).

**Perf / undo / integrity.** N/A (read-only constant).

---

### M1 — Engine change + Memory model + installs

#### M1 1a — Question-Gated Card Activation (the marquee Rust engine change)

> This is the central grading gate and gets the deepest treatment. It comprises **three** capabilities: **(1) gated activation**, **(2) activated-card value ordering**, **(3) coverage sweep**. Per `Anki_Plan.md`, if a smaller first slice is needed, **land the value-ordering (points-at-stake) first** (see Decision D-2).

##### Objective & acceptance criteria

- **Objective.** Move card activation out of "all cards available" into "cards are off until a missed, memory-classified question turns them on," with a deterministic, atomic, undo-safe Rust implementation, plus a value-ordered activated-card queue and a periodic coverage sweep — **without altering FSRS intervals/due dates**.
- **AC-1.** Flashcards default to **suspended**; a suspended card never appears in the review queue (already guaranteed by gathering — see below).
- **AC-2.** `ActivateCardsForMiss(question_id, miss_reason)` unsuspends the question's linked cards **iff** `miss_reason ∈ {KNOWLEDGE_GAP, MISSING_CONTEXT}`; it is a **no-op** for `MISUNDERSTANDING`/`CARELESS` and returns an empty `activated_card_ids`.
- **AC-3.** Activation is **atomic + undoable** (single undo step) and passes a collection-integrity check; re-running on already-active cards is idempotent.
- **AC-4.** The review queue is built **only from activated (non-suspended) cards** and, in Speedrun mode, ordered by **`value = topic_weight × weakness`** (`weakness = 1 − topic_mastery`), highest value first; ties broken deterministically.
- **AC-5.** `RunCoverageSweep(sample_size)` re-activates a spread **across all topics** (not concentrated in one), and is atomic + undoable. (`sample_size == 0`, the proto3 default, means "use configured default", not "sweep nothing"; clamped to ≥ 1.)
- **AC-6.** FSRS intervals/due dates are unchanged by activation/sweep/ordering (verified by comparing card scheduling fields before/after).

##### Why this belongs in Rust (the required 1-page argument, condensed)

1. **Atomic, undo-safe state transitions.** Activation flips `CardQueue::Suspended → restored queue` for a _set_ of cards. Anki's transactional undo lives in the Rust core: `Collection::transact(Op, |col| …) -> OpOutput<…>` with per-table undo (`rslib/src/ops.rs`, `rslib/src/undo/`). The existing, verified primitive `Collection::unbury_or_unsuspend_cards(&[CardId]) -> Result<OpOutput<()>>` already wraps the exact transition in `self.transact(Op::UnburyUnsuspend, …)` (`rslib/src/scheduler/bury_and_suspend.rs:64`). Doing this in Python could not participate in the same atomic undo entry and would risk partial writes.
2. **Performance on 50k cards.** Gathering/ordering touches the queue builder and DB-side iteration (`rslib/src/scheduler/queue/builder/`, `rslib/src/storage/card/due_cards.sql`). Native Rust + SQLite keeps the "next card p95 < 100ms" budget; a Python loop over 50k cards cannot.
3. **Shared by Android.** Android (AnkiDroid) talks to the **same** Rust backend (`proto/anki/ankidroid.proto`, `rslib/src/ankidroid/`). Putting gating in Rust means Android gets it for free via the new RPCs; a Python/Qt implementation would not reach Android.

##### Files to create/modify

**proto/**

- **`proto/anki/speedrun.proto` (NEW)** — recommended dedicated service file (additive ⇒ low merge risk; auto-globbed). Declares `SpeedrunService` + empty `BackendSpeedrunService`. _Alternative (D-15):_ add the RPCs to `proto/anki/scheduler.proto` (exists) under `SchedulerService` (merge risk med — edits an upstream file).
- Reuse existing message types where possible: `anki/cards.proto` `CardIds`, `anki/collection.proto` `OpChanges`/`OpChangesWithCount`.

**rslib/** (new module, additive ⇒ low merge risk except where noted)

- **`rslib/src/speedrun/mod.rs` (NEW)** — module root; `pub(crate)` API.
- **`rslib/src/speedrun/activation.rs` (NEW)** — `Collection::activate_cards_for_miss(question_nid: NoteId, reason: MissReason) -> Result<OpOutput<Vec<CardId>>>`; resolves linked cards then delegates to the suspend/undo primitive.
- **`rslib/src/speedrun/linkage.rs` (NEW)** — `Collection::linked_card_ids_for_question(question_nid) -> Result<Vec<CardId>>` using `SearchNode::Tag { tag, mode }` (enum defined in `rslib/src/search/parser.rs:96`; builder/constructor helpers in `rslib/src/search/builder.rs`) + optional `gates::` parse.
- **`rslib/src/speedrun/sweep.rs` (NEW)** — `Collection::run_coverage_sweep(sample_size: u32) -> Result<OpOutput<Vec<CardId>>>`; stratified sample across `topic::*`.
- **`rslib/src/speedrun/mastery.rs` (NEW — delivered by the shared task T3a; consumed by BOTH value-ordering (T5) and Memory (T7))** — `topic_mastery_map()` and `topic_weakness_map()` helpers. Created once by T3a; **not** re-created in 1b/T7. (Resolves the double-ownership flagged in review.)
- **`rslib/src/speedrun/blueprint.rs` (NEW)** — load/parse blueprint weights from config.
- **`rslib/src/speedrun/value_order.rs` (NEW)** — pure function `card_value(topic_weight, weakness) -> f32` + a comparator.
- **`rslib/src/scheduler/queue/builder/speedrun_value.rs` (NEW)** — a post-gather sorter. The value **cannot** be computed at the `build` site: `QueueBuilder::build(mut self, learn_ahead_secs)` (`builder/mod.rs:186`) has **no `Collection` handle**, and `DueCard`/`NewCard` (`builder/mod.rs:33-60`) carry **no tags/topic**. Real data flow: (1) `QueueBuilder::new(col, …)` (`builder/mod.rs:128`, has `col`) loads the blueprint `topic_weight` map + the `topic→weakness` map (via the shared mastery helper, T3a) into a new `Context` field; (2) `gather_cards(&mut self, col)` (`gathering.rs:14`, has `col`) batch-loads `topic::` tags for all gathered `note_id`s and fills a new `speedrun_values: HashMap<CardId, f32>` field on `QueueBuilder`; (3) `build` calls `speedrun_value::sort_by_value(&mut self.review, &mut self.new, &self.speedrun_values)` when `BoolKey::SpeedrunOrdering` is set. **Upstream `builder/mod.rs` edits = two lines** (`mod speedrun_value;` + the one guarded call) **plus** the new `speedrun_values`/`Context` fields (merge risk med; the comparison logic lives in this NEW sibling module).
- **`rslib/src/speedrun/service.rs` (NEW)** — `impl crate::services::SpeedrunService for Collection` (mirrors `rslib/src/scheduler/service/mod.rs` style).
- **`rslib/src/lib.rs`** (exists, upstream) — add `mod speedrun;` (merge risk low: one line).
- **`rslib/src/ops.rs`** (exists, upstream) — add `Op::ActivateForMiss` and `Op::CoverageSweep` variants + `describe()` arms returning `tr.speedrun_activate_for_miss()` / `tr.speedrun_coverage_sweep()` (merge risk low–med: enum additions; new `speedrun-*` ftl strings).

**pylib/**

- **`pylib/anki/speedrun.py` (NEW)** — `Speedrun` helper exposing `activate_cards_for_miss(...)`, `run_coverage_sweep(...)`, wrapping `col._backend.*`.
- **`pylib/anki/collection.py`** (exists, upstream) — instantiate `self.speedrun = Speedrun(self)` next to existing sub-objects (merge risk low: one attribute).

**ftl/**

- **`ftl/core/speedrun.ftl` (NEW)** — undo-label strings keyed **`speedrun-activate-for-miss`** / **`speedrun-coverage-sweep`** (→ `tr.speedrun_activate_for_miss()` / `tr.speedrun_coverage_sweep()`), referenced by `Op::describe`. **Naming rule:** `actions-*` keys live in `ftl/core/actions.ftl` (they generate `tr.actions_*`), so a `speedrun.ftl` file must use a `speedrun-*` prefix. _Alternative:_ add the two strings to `ftl/core/actions.ftl` to match the `actions_*` pattern used by other `Op`s — pick one and stay consistent.

**qt/aqt/** — wiring only here; full UX in 2b.

- **`qt/aqt/reviewer.py`** (exists) — minimal hook so a missed question can call activation during dev (full flow in 2b).

**tests/** (see testing procedure).

##### Protobuf changes (signatures)

```proto
// proto/anki/speedrun.proto  (NEW)
syntax = "proto3";
package anki.speedrun;
import "anki/generic.proto";
import "anki/cards.proto";
import "anki/collection.proto";

service SpeedrunService {
  // Unsuspends a missed question's linked cards iff the reason qualifies.
  rpc ActivateCardsForMiss(ActivateCardsForMissRequest) returns (ActivateCardsResponse);
  // Re-activates a spread of cards across all blueprint topics.
  rpc RunCoverageSweep(RunCoverageSweepRequest) returns (ActivateCardsResponse);
}
service BackendSpeedrunService {}   // required pairing (may stay empty)

enum MissReason {
  MISS_REASON_UNSPECIFIED = 0;   // treated as no-op (safety)
  KNOWLEDGE_GAP = 1;             // activates
  MISSING_CONTEXT = 2;           // activates
  MISUNDERSTANDING = 3;          // no-op
  CARELESS = 4;                  // no-op
}

message ActivateCardsForMissRequest {
  int64 question_note_id = 1;
  MissReason miss_reason = 2;
}
message RunCoverageSweepRequest {
  // proto3 trap: an omitted uint32 reads back as 0. The impl MUST treat
  // sample_size == 0 as "use the configured default" (≈1–2 cards/topic, D-2c),
  // never "sweep nothing", and clamp to a minimum of 1.
  uint32 sample_size = 1;
}

message ActivateCardsResponse {
  collection.OpChanges changes = 1;
  repeated int64 activated_card_ids = 2;
}
```

**Routing decision.** Both RPCs are **Rust-implemented** (not in `FrontendService`) → live in `SpeedrunService`. They return `collection.OpChanges` so the Qt/TS layers can trigger the standard post-op refresh (mirrors how `BuryOrSuspendCards` returns `OpChangesWithCount`).

##### Rust implementation approach (detailed)

**(1) Gated activation** — reuse, don't reinvent:

```
fn activate_cards_for_miss(&mut self, qnid, reason) -> Result<OpOutput<Vec<CardId>>> {
    if !matches!(reason, KnowledgeGap | MissingContext) {
        // no-op: return an OpOutput with empty changes (Op::SkipUndo) + empty vec
    }
    self.transact(Op::ActivateForMiss, |col| {
        let cids = col.linked_card_ids_for_question(qnid)?;       // search by topic::/gates::
        let to_activate = col.filter_suspended(&cids)?;           // idempotent
        col.unsuspend_or_unbury_searched_cards(                    // reuse (bump fn → pub(crate))
            col.all_cards_for_ids(&to_activate, false)?            // already pub(crate)
        )?;
        Ok(to_activate)
    })
}
```

- The inner unsuspend reuses the **verified** path in `rslib/src/scheduler/bury_and_suspend.rs` (`unsuspend_or_unbury_searched_cards` / `Card::restore_queue_after_bury_or_suspend`). **Visibility:** `unsuspend_or_unbury_searched_cards` is a **private `fn`** today (`bury_and_suspend.rs:53`) and must be bumped to **`pub(crate)`** for `rslib/src/speedrun/` to call it; `all_cards_for_ids` (`rslib/src/search/mod.rs:269`) and `Card::restore_queue_after_bury_or_suspend` (`bury_and_suspend.rs:18`) are **already `pub(crate)`**. We add a thin `Op::ActivateForMiss` so the undo entry reads correctly, rather than borrowing `Op::UnburyUnsuspend`.
- **Idempotency:** `restore_queue_after_bury_or_suspend` already returns `false` for non-suspended cards, so re-activation is a clean no-op.

**(2) Activated-card value ordering** — post-gather sort (chosen over a new SQL `ReviewCardOrder` variant because `topic_weight × weakness` depends on per-topic aggregates that are awkward in SQL). **The value is computed where `col` is available, not at the `build` site:**

- Suspended cards are **already excluded** from gathering: `rslib/src/storage/card/due_cards.sql` selects `WHERE … queue = ? AND due <= ?` (the `?` is a positive queue id; `CardQueue::Suspended` is never matched). So "queue only from activated cards" needs **no** change — it falls out of suspension.
- **Why not at `build`:** `QueueBuilder::build(mut self, learn_ahead_secs)` (`builder/mod.rs:186`) gets no `Collection`, and `DueCard`/`NewCard` (`builder/mod.rs:33-60`) expose only `id`/`note_id`/`due`/deck/`reps` — **no tags**. So topic resolution must happen earlier.
- **Step A (in `QueueBuilder::new`, `builder/mod.rs:128`, has `col`):** when `BoolKey::SpeedrunOrdering` is set, load the blueprint `topic_weight` map (D-3/D-17) and the `topic→weakness` map (`weakness = 1 − topic_mastery`, from the shared mastery helper `mastery.rs`, T3a) and stash both in `Context`.
- **Step B (in `gather_cards(&mut self, col)`, `gathering.rs:14`, has `col`):** after gathering, batch-load the `topic::` tags for all gathered `note_id`s (one query), compute each card's `value`, and store it in a new `speedrun_values: HashMap<CardId, f32>` field on the builder.
- **Step C (in `build`):** behind the flag, call `pub(super) fn sort_by_value(review: &mut Vec<DueCard>, new: &mut Vec<NewCard>, values: &HashMap<CardId, f32>)` before `merge_day_learning`/`merge_new`, sorting **descending** by `values[&card.id]` with a **stable** tiebreak (preserve prior gather order, then `card.id`).
- **Multi-topic reduction rule (defined):** a card may carry several `topic::` tags. Use `value = max over the card's topics t of ( topic_weight(t) × weakness(t) )` — surface the card for its single most-valuable/weakest topic. (Documented alternative: `max(weakness) × max(topic_weight)` taken independently; we choose per-topic-product-then-max because it never over-credits a card whose heavy-weight topic is already mastered.) Cards with no `topic::` tag get `value = 0` (sorted last).
- This leaves upstream sort code (`builder/sorting.rs`, `storage/card/mod.rs::review_order_sql`) untouched.

**(3) Coverage sweep** — `run_coverage_sweep(sample_size)`:

```
self.transact(Op::CoverageSweep, |col| {
    let by_topic = col.suspended_cards_grouped_by_topic()?;   // topic:: tag → Vec<CardId>
    let picks = stratified_sample(&by_topic, sample_size);    // round-robin across topics
    col.unsuspend_or_unbury_searched_cards(col.all_cards_for_ids(&picks, false)?)?;
    Ok(picks)
})
```

- Stratification = round-robin across topics so no single topic dominates (satisfies AC-5).
- **proto3 default handling:** `sample_size == 0` (field omitted) ⇒ substitute the configured default (≈1–2 cards/topic, D-2c) and clamp to a minimum of 1 — it must **never** mean "sweep nothing."

**Op/undo additions.** Add `Op::ActivateForMiss`, `Op::CoverageSweep` to `rslib/src/ops.rs` with `describe()` arms returning the new `tr.speedrun_activate_for_miss()` / `tr.speedrun_coverage_sweep()` strings (defined in `ftl/core/speedrun.ftl` with `speedrun-*` keys; see ftl naming note above). No new undoable record types are needed — card-queue changes already have undo support via `update_card_inner`.

##### Python + Qt wiring

- `pylib/anki/speedrun.py`: `activate_cards_for_miss(question_note_id, miss_reason) -> OpChanges` and `run_coverage_sweep(sample_size) -> OpChanges`, returning the protobuf `OpChanges` (typed alias) so callers can hand it to `aqt`'s op machinery.
- For 1a the Qt surface is minimal (a dev trigger); the real miss-classification UX lands in **2b**.

##### TS/Svelte UI

- None required for 1a beyond what 2b builds. The RPCs are also callable from TS via `@generated/backend` once bindings regenerate.

##### Expected result

With Speedrun mode on, a fresh MCAT deck shows **zero** due cards. Missing a question and choosing `knowledge-gap`/`missing-context` makes exactly the linked cards reviewable, ordered by value; choosing `misunderstanding`/`careless` changes nothing. A sweep surfaces a cross-topic spread. Undo reverts any of these in one step; FSRS scheduling fields are untouched.

##### Testing procedure — **≥3 Rust unit tests + 1 Python RPC test (required)**

Rust (`#[cfg(test)] mod test` in `rslib/src/speedrun/activation.rs` / `sweep.rs`; pattern mirrors `bury_and_suspend.rs::test::unbury`) → `just test-rust`:

1. **`activates_only_qualifying_reasons`** — build a question note + linked suspended cards; assert activation for `KnowledgeGap` and `MissingContext`; assert **no-op** (empty result, cards stay `Suspended`) for `Misunderstanding` and `Careless`. (covers AC-2)
2. **`queue_excludes_suspended_and_orders_by_value`** — with two topics of differing `topic_weight` and differing mastery, assert the queue contains only activated cards and that higher `topic_weight × weakness` cards come first (`build_queues`/`get_queued_cards`). (covers AC-1, AC-4)
3. **`sweep_spreads_across_topics`** — many suspended cards across N topics; `run_coverage_sweep(k)`; assert the reactivated set touches ≥ min(N, k) distinct topics (no single-topic concentration). (covers AC-5)
4. **`activation_is_undoable_and_preserves_scheduling`** _(integrity/undo, also a required artifact)_ — snapshot scheduling fields; activate; `col.undo()`; assert cards return to `Suspended` and FSRS fields unchanged; run the collection-integrity check. (covers AC-3, AC-6)

Python (`pylib/tests/test_speedrun.py` (NEW)) → `just test-py`:
5. **`test_activate_cards_for_miss_rpc`** — via `col.speedrun.activate_cards_for_miss(...)`: qualifying reason returns the expected `activated_card_ids` and unsuspends them; non-qualifying returns empty and leaves them suspended. (covers the "1 Python-side RPC test" requirement)

Optional e2e (`ts/tests/e2e/` (NEW)) → `just test-e2e`: miss → classify → activated card becomes reviewable (can be deferred to 2b).
Full gate: `just check`.

##### Perf / undo / integrity notes

- **Perf:** activation is O(linked cards) via one search + one batched update inside a single transaction; sweep is O(sample_size). Value sort is O(n log n) over gathered cards only (≤ daily limits), not the whole 50k. Benchmark in M4 (`just bench`, NEW) must report next-card p95 < 100ms and button-ack p95 < 50ms on a 50k deck.
- **Undo:** every mutation goes through `transact(Op::…)`; one user action = one undo entry.
- **Integrity:** reuse `unsuspend_or_unbury_searched_cards` (no raw SQL queue writes); after ops, `Collection`'s standard integrity check must pass.

##### Required artifacts (per `Anki_Plan.md`)

- The diff (the NEW `rslib/src/speedrun/` module + proto + guarded builder call).
- Undo + integrity proof (test #4 above + a manual `Check Database`).
- The 1-page "why this belongs in Rust" (§ above).
- Upstream-files-touched list with merge notes:
  - `rslib/src/lib.rs` — 1 `mod speedrun;` line (low).
  - `rslib/src/ops.rs` — 2 `Op` variants + `describe()` arms (low–med).
  - `rslib/src/scheduler/bury_and_suspend.rs` — **bump `unsuspend_or_unbury_searched_cards` from private `fn` to `pub(crate)`** so `rslib/src/speedrun/` can reuse it (`bury_and_suspend.rs:53`; one visibility keyword, low). `all_cards_for_ids` / `restore_queue_after_bury_or_suspend` are already `pub(crate)` — no change.
  - `rslib/src/scheduler/queue/builder/mod.rs` — **2 lines** (`mod speedrun_value;` + 1 guarded `sort_by_value` call) **plus** a `speedrun_values` field on the builder and topic-weight/weakness fields on `Context` (med).
  - `rslib/src/config/bool.rs` — 1 `BoolKey::SpeedrunOrdering` key (low).
  - `pylib/anki/collection.py` — 1 `self.speedrun = …` attribute (low).
  - `proto/anki/scheduler.proto` — _only if D-15 picks the non-dedicated option_ (else untouched).

---

#### M1 1b — Memory model running

**Objective & acceptance.** Expose per-card FSRS retrievability `R(t)=(1+t/(9S))^(−1)` and aggregate to **per-topic mastery**; display an honest Memory score **with a range** and an **abstention** ("give-up") rule when data is thin. **AC:** Memory shows for a deck with a numeric range; not-yet-activated topics read as unknown/low; below a stated data threshold the score abstains.

**Files to create/modify.**

- `proto/`: add to **`proto/anki/speedrun.proto` (NEW)** — `rpc GetMemoryScore(GetMemoryScoreRequest) returns (MemoryScoreResponse);` with per-topic mastery + overall range + abstain flag.
- `rslib/`: **consumes `rslib/src/speedrun/mastery.rs` (created once by T3a — NOT re-created here)** — that shared helper reuses the verified FSRS call from `rslib/src/stats/graphs/retrievability.rs` (`FSRS::new(None)`, `fsrs.current_retrievability_seconds(state.into(), elapsed_seconds, card.decay.unwrap_or(FSRS5_DEFAULT_DECAY))`, gated on `card.memory_state`) and aggregates **stability-weighted mean** `R(t)` over **activated** cards carrying each `topic::` tag, marking topics with no activated cards as unknown. 1b builds `get_memory_score` in `service.rs` on top of it. FSRS plumbing reference: `rslib/src/scheduler/fsrs/`.
- `rslib/src/speedrun/service.rs` (NEW) — implement `get_memory_score`.
- `pylib/anki/speedrun.py` (NEW) — `get_memory_score()` helper.
- `ts/`: **`ts/routes/speedrun/dashboard/` (NEW)** Svelte route renders the Memory card (point + range + abstention badge); shared widgets in **`ts/lib/components/` (NEW components)**.
- `ftl/`: add Memory/abstention strings to `ftl/core/speedrun.ftl` (NEW).

**Protobuf / routing.** Rust-implemented (`SpeedrunService`), since Android must show Memory too.

**Rust approach.** Pure read over cards; no mutation, no transaction. Range = e.g. mastery ± a function of sample size / stability spread (exact width tied to Decision D-6). Abstention when activated-card count for the topic/deck < N (D-6).

**Python+Qt wiring.** `col.speedrun.get_memory_score()`; render in the dashboard webview hosted by `qt/aqt/mediasrv.py` page registration.

**TS/Svelte UI.** Memory tile with point estimate, range, coverage, "insufficient data → abstaining" state.

**Expected result.** A believable Memory % with a range that widens on thin data and abstains below threshold.

**Testing.** Rust unit tests for aggregation (known `memory_state` → expected mastery; empty-topic → unknown; abstain branch) → `just test-rust`. TS component test for the abstention rendering → `just test-ts`. `just check`.

**Perf/undo/integrity.** Read-only (no undo). Cache per refresh to keep dashboard refresh p95 < 500ms on 50k cards.

---

#### M1 1c — Installs + Android

**Objective & acceptance.** Desktop installer runs on a clean machine; Android app builds and runs a real review session on the **shared engine** (two-way sync not yet required). **AC:** installer launches clean; Android reviews the MCAT deck.

**Files to create/modify.**

- Desktop: **`qt/installer/`** (exists) — Briefcase templates `mac-template/`, `linux-template/`, `windows-template/`; app metadata in `qt/installer/app/`. Update branding/name to "Speedrun"; ensure it bundles with **AI off** by default. Merge risk low (config/templates).
- Android: **AnkiDroid repo (external)** — bump its backend to this fork's `rslib`; no Speedrun UI required yet. _In-repo:_ ensure `rslib` compiles for Android targets and the new `SpeedrunService` RPCs are exposed through the generated backend that AnkiDroid consumes.

**Expected result.** A double-click desktop install; an APK that opens and reviews a deck via the shared Rust core.

**Testing.** Manual install smoke test per OS; Android debug build opens + reviews. Rust/bindings covered by `just check`.

**M1 exit (Definition of Done).** Gating Rust change merged with its tests + undo/integrity (cards suspended by default; only qualifying misses activate; queue from activated cards, value-ordered); Memory score with range; desktop installer clean; Android reviews the shared deck.

---

### M2 — AI + sync + question/response pipeline

#### M2 2a — Grounded AI card generation (with eval gate)

**Objective & acceptance.** Generate/rephrase flashcards **from a named source**; every generated card stores its `source` (no source ⇒ rejected). A **pre-registered eval** runs before any card reaches a user; a **leakage check** is clean; and the app **still scores with AI off**.
**AC:** eval numbers + baseline table produced; leakage clean; AI-off path produces all three scores; cards lacking a traceable source are blocked.

**Files to create/modify.** _(AI is desktop-only Python; Android uses the AI-off path — see D-12.)_

- `pylib/`: **`pylib/anki/speedrun/ai.py` (NEW)** — generator client (provider per D-5); attaches `source`; rejects sourceless output. Creates normal cards via `col.add_note` / `pylib/anki/notes.py` with `topic::`/`pool::served` tags.
- **`pylib/anki/speedrun/eval.py` (NEW)** — gold-set harness: load 50 Q&A pairs, generate 50 cards from one real source, classify each (correct-and-useful / wrong / correct-but-bad-teaching), report accuracy + wrong-answer rate, enforce a **pre-set pass cutoff**, and **block** failing cards.
- **`pylib/anki/speedrun/leakage.py` (NEW)** — scans generation/training inputs for any `pool::heldout` item or near-duplicate (text similarity); reports clean/dirty.
- **`pylib/anki/speedrun/baseline.py` (NEW)** — keyword/vector-search baseline for the head-to-head retrieval comparison.
- `qt/aqt/`: **`qt/aqt/speedrun/ai_dialog.py` (NEW)** — desktop UI to pick a source, run generation, and view the eval/baseline/leakage report; routed via `qt/aqt/mediasrv.py`.
- Data: gold set + source text under **`extra/` (NEW, gitignored)** per repo convention so checks ignore it (licensing per D-4/D-16).
- `proto/`: **none** (AI runs in Python; if a thin status RPC is wanted it goes in `FrontendService`).

**Routing.** AI is Python-side; no Rust RPC. Any UI hook is `FrontendService` (Python-implemented).

**Expected result.** A reproducible report (accuracy, wrong-answer rate, baseline win, leakage clean); only passing, sourced cards are added; disabling AI still yields all three scores.

**Testing.** Python tests for: source-required rejection; eval cutoff blocks failing cards; leakage detects a planted held-out item; AI-off still computes scores (`pylib/tests/`, `qt/tests/`) → `just test-py`. `just check`.

**Perf/undo/integrity.** Generated cards are normal notes (undo via `Op::AddNote`). No engine changes.

---

#### M2 2b — Question-first study loop (the SPOV3 core surface)

**Objective & acceptance.** The primary study experience: serve `pool::served` questions interleaved across topics; grading writes a `revlog` review; on a miss, classify _why_ and call gated activation; expose a triggerable coverage sweep; never serve `pool::heldout`.
**AC:** loop records miss-reasons and gated activation unsuspends the right cards (and only for qualifying misses); periodic sweep re-activates across topics; reviews write to `revlog`; held-out is never served.

**Files to create/modify.**

- **Serving mechanism (D-14).** _Recommended:_ reuse Anki's **filtered deck** machinery to build an interleaved `Speedrun::Questions` session from `note:SpeedrunQuestion tag:pool::served`, random/interleaved order (`rslib/src/scheduler/filtered/` (exists), `qt/aqt/filtered_deck.py`). Answering reuses the normal `answer_card` path (`rslib/src/scheduler/answering/`), so the `revlog` write is native. _Alternative:_ a custom `SpeedrunService` RPC that returns the next served question + records the attempt.
- `ts/`: **`ts/routes/speedrun/session/` (NEW)** — question UI (stem/options, submit, post-answer explanation). On incorrect, show a **miss-reason chooser** (4 buttons, mirroring grade buttons). Reuse reviewer primitives from `ts/reviewer/` (exists) and components in `ts/lib/components/`.
- `qt/aqt/`: **`qt/aqt/speedrun/session.py` (NEW)** or extend **`qt/aqt/reviewer.py`** (exists) to host the question webview; on miss-classify, call `col.speedrun.activate_cards_for_miss(...)`; expose a "Run sweep" action calling `col.speedrun.run_coverage_sweep(...)`.
- `pylib/`: `pylib/anki/speedrun.py` (NEW) — `record_miss_reason(question_nid, reason)` that sets the latest `miss::<reason>` note tag (via `col.tags`/`AddNoteTags`, first clearing any prior `miss::*` on that note), then triggers activation (per D-11; **no `custom_data`** — attempt history is read from `revlog`).
- `proto/`: reuse the 1a `SpeedrunService` RPCs; optionally add `rpc NextServedQuestion(...)` if not using filtered decks (D-14).
- `ftl/`: miss-reason labels + session strings in `ftl/core/speedrun.ftl` (NEW).

**Routing.** Activation/sweep = Rust (`SpeedrunService`, from 1a). Session UI orchestration = TS + Python; any Python-only hook = `FrontendService`.

**Expected result.** Students answer interleaved served questions; misses prompt a why-classification; qualifying misses unsuspend linked cards; a sweep re-activates a cross-topic spread; all attempts land in `revlog`.

**Testing.** Reuse 1a Rust/Python tests for gating; add a Python test that a recorded miss sets the latest `miss::<reason>` note tag (replacing any prior one) and activates the linked cards; e2e (`ts/tests/e2e/` NEW) for the full miss→classify→activate→reviewable flow → `just test-e2e`. Assert held-out is never served. `just check`.

**Perf/undo/integrity.** Each answer = one `revlog` write (existing undo `Op::AnswerCard`); activation = one `Op::ActivateForMiss`. Button-ack p95 < 50ms, next-card p95 < 100ms.

---

#### M2 2c — Sync (reuse, don't rewrite) + Android scores

**Objective & acceptance.** Forked desktop + Android sync via Anki's existing sync; two-way + offline reviews merge with none lost/double-counted; document the conflict rule; Android shows the three scores with ranges + give-up rule.
**AC:** Android↔desktop offline reviews reconcile correctly on reconnect; conflict rule documented; Android displays the three scores.

**Files to create/modify.**

- **Reuse** `rslib/src/sync/` (exists) entirely — native objects (notes/cards/tags/`revlog`/config) sync for free. **No sync rewrite.** Desktop entry points already exist: `pylib/anki/collection.py::sync_collection`/`full_upload_or_download`, `qt/aqt/sync.py`.
- Hosting (D-1): if self-hosted, document `rslib/src/sync/http_server/` usage (`docs/syncserver/` exists).
- Android (external repo): render the three scores by calling the Rust score RPCs (`SpeedrunService.GetMemoryScore` + the 3a/3b RPCs).
- Docs: a short note in `README.md`/`docs/` stating the merge/conflict rule (incremental sync merges `revlog` from both devices; on a **schema** conflict Anki forces a one-way full sync — state which direction and why).

**Routing.** Sync is core Rust (unchanged). Score RPCs (Rust) give Android parity.

**Expected result.** Review offline on phone and desktop; reconnect; all reviews present exactly once; both devices show consistent scores.

**Testing.** `rslib/src/sync/collection/tests.rs` (exists) covers core sync; add a scenario test for offline-both-then-reconcile reconciliation. Manual two-device test. `just check`.

**Perf/undo/integrity.** Sync integrity is upstream-guaranteed; our additions are plain objects.

**M2 exit (DoD).** AI eval + baseline + leakage clean + AI-off scoring; question-first loop with miss-reason → gated activation; periodic sweep; reviews sync; two-way offline reconcile.

---

### M3 — Performance + Readiness models

> Scaffold both early against **mock inputs**; fit on real `revlog` here. **Placement decision (D-12):** to keep Android parity (2c requires Android to show all three scores), implement the score computations in **Rust** (`SpeedrunService`), with a **Python reference implementation** used only for the M4 offline validation/eval harness.

#### M3 3a — Performance model — `P(correct on a NEW question)`

**Objective & acceptance.** Predict probability of answering a _new_ question correctly from features, with a **non-trivial memory→performance gap** (must not collapse to Memory). **AC:** beats chance on held-out responses; a measurable gap vs. the Memory model exists (validated by the paraphrase test in 4b).

**Inputs per response.** mean topic mastery over the question's `topic::` tags; **min topic mastery (weakest link)**; question `difficulty_b`; `discrimination_a`; response time (from `revlog`).

**Model.** 2PL IRT with a 4-option guessing floor: `P = c + (1−c)/(1+e^(−a(θ−b)))`, `c ≈ 0.25` — or an equivalent logistic/GBM over the features above (choice = D-7). Estimate ability `θ` and keep its standard error for M4 uncertainty.

**Interface (proposed symbols).** `predict_performance(question, mastery_vector) -> f32` and `estimate_theta(responses) -> (theta, se)`.

**Files to create/modify.**

- `proto/`: `proto/anki/speedrun.proto` (NEW) — `rpc PredictPerformance(...) returns (...)`, `rpc EstimateAbility(...) returns (AbilityResponse{theta, se})`.
- `rslib/`: **`rslib/src/speedrun/performance.rs` (NEW)** — IRT/logistic eval + `θ` estimation over `revlog` (read via storage). Reuse `revlog` access (`rslib/src/revlog/`) and mastery from `mastery.rs`.
- `pylib/`: **`pylib/anki/speedrun/models_ref.py` (NEW)** — numpy/scipy reference for validation only.
- `ts/`: Performance tile in `ts/routes/speedrun/dashboard/` (NEW).

**Routing.** Rust (`SpeedrunService`) for parity; Python reference is offline-only.

**Expected result.** Calibrated-ish `P(correct)` per question; `θ,se` per student.

**Testing.** Rust unit tests: guessing floor (`P ≥ c`), monotonic in `θ−b`, weakest-link feature changes output independent of mean mastery (proves the gap) → `just test-rust`. Python parity test vs. reference → `just test-py`. `just check`.

**Perf/undo/integrity.** Read-only over `revlog`; cache per refresh.

#### M3 3b — Readiness model — projected exam score + range

**Objective & acceptance.** Simulate the MCAT via the blueprint and emit a scaled score with an interval, coverage, confidence, last-updated, top reasons, and an abstention rule; plus a **progress/motivation** surface showing rising practice-question performance. **AC:** produces a calibrated-looking interval and **correctly abstains** on thin data.

**Method.** Monte Carlo: repeatedly (1) sample `θ` from its posterior, (2) sample questions per blueprint topic weights, (3) sample Bernoulli outcomes from `P(correct)` → raw-score distribution. Uncovered topics use a low/prior mastery and **widen** uncertainty (ties confidence to coverage). **Raw→scaled** via a stated monotonic concordance curve (D-8) to 472–528. **Output:** median + **80% interval** (e.g., 508, 503–512), coverage %, confidence, last-updated, top reasons. **Abstention (D-6):** emit no score when graded responses < N **or** coverage < X% **or** interval width > a threshold.

**Files to create/modify.**

- `proto/`: `proto/anki/speedrun.proto` (NEW) — `rpc GetReadiness(...) returns (ReadinessResponse{median, low, high, coverage, confidence, updated_at, reasons, abstained})`.
- `rslib/`: **`rslib/src/speedrun/readiness.rs` (NEW)** — Monte-Carlo simulation + concordance mapping + abstention; reads blueprint from `blueprint.rs`, `θ` from `performance.rs`.
- `pylib/`: extend `models_ref.py` (NEW) with a readiness reference for validation.
- `ts/`: **`ts/routes/speedrun/dashboard/` (NEW)** Readiness tile (score + 80% interval + coverage + confidence + reasons + abstain state) and **`ts/routes/speedrun/progress/` (NEW)** progress view (rising practice-question performance over time — the burnout countermeasure).
- `ftl/`: readiness/progress strings in `ftl/core/speedrun.ftl` (NEW).

**Routing.** Rust (`SpeedrunService`) for parity.

**Expected result.** A headline projected score with an honest interval that widens with low coverage and abstains on thin data; a motivating progress curve.

**Testing.** Rust tests: abstains below N/coverage/width thresholds; interval widens as coverage drops; concordance is monotonic → `just test-rust`. Python parity for the simulation → `just test-py`. TS tile/abstention rendering → `just test-ts`. `just check`.

**Perf/undo/integrity.** Read-only; cap MC iterations so dashboard load p95 < 1s / refresh < 500ms on 50k cards.

**M3 exit (DoD).** Performance beats chance with a real memory→performance gap; readiness emits an interval + abstains correctly.

---

### M4 — Validation, ablation, packaging, hand-in

#### M4 4a — Model validation (held-out, re-runnable)

**Objective & acceptance.** Validate all three on held-out data, re-runnably. **AC:** Memory reliability diagram + Brier/log-loss on held-out reviews (with Platt/isotonic recalibration if needed); Performance accuracy/AUC on `pool::heldout` questions + the **paraphrase test** (30 cards → 2 reworded Qs each; report the card-recall vs. reworded-accuracy **gap**); Readiness method + range documented (bonus: compare to real practice-test scores).

**Files.** **`pylib/anki/speedrun/validation.py` (NEW)** (train/test split of `revlog`; metrics; recalibration); reports to `extra/` (gitignored). Reuse `models_ref.py`. Held-out split = `pool::heldout` tag filter.

**Testing.** Python tests that metrics compute and the held-out split excludes served items → `just test-py`. `just check`.

#### M4 4b — Study-feature ablation (15% of grade)

**Objective & acceptance.** Pick one learning-science feature (recommend **interleaving** — natively supported by serving interleaved across topics) and pre-register the metric + failure condition in one sentence (D-9). Compare **3 builds at equal study time**: full app / feature-off / plain unmodified Anki. Report a range and **null results honestly**.
**AC:** ablation reported including nulls.

**Files.** **`pylib/anki/speedrun/ablation.py` (NEW)** harness; an "interleaving off" config flag (`BoolKey`, NEW) to produce the feature-off build; document the pre-registration in the hand-in. Test "learners" per D-10.

**Testing.** Python harness test → `just test-py`.

#### M4 4c — Robustness + packaging

**Objective & acceptance.** Crash test (kill each app mid-review 20× → zero corrupted collections); offline AI degrades cleanly while scoring continues; one-command benchmark on the 50k deck; sync-conflict test; package desktop installer + signed Android APK, both runnable **AI-off**.
**AC:** zero corruption across 20 kills/app; bench prints p50/p95/worst per action; documented correct sync winner; both apps packaged + AI-off.

**Files.**

- **`just bench` recipe (NEW)** in `justfile` (exists) → **`tools/bench/ (NEW)`** or a Rust bench in `rslib/` printing p50/p95/worst for: button ack, next card, dashboard load/refresh on a generated 50k deck. (Maps the brief's `make bench` to the repo's `just` convention — see D-15/notes.)
- Crash/offline/sync-conflict scripts under `extra/` or `qt/tests/` (NEW).
- Packaging: `qt/installer/` (exists) for desktop; Android signing in the external repo.

**Testing.** Run `just bench`; crash loop; offline-mode score check; two-device same-card conflict. `just check`.

#### M4 4d — Hand-in

**Objective & acceptance.** Public AGPL fork (exam stated; build instructions for both apps; architecture overview; Rust-change note; files-touched list); 3–5 min demo video (review session, Rust change in action, Android→desktop sync, three scores with ranges, AI features, test results); one-page model descriptions (Memory/Performance/Readiness incl. give-up rule); brainlift (confirm format); honest results report incl. what failed.

**Files.** `README.md` (exists) + **`docs/speedrun/` (NEW)** for the architecture overview, Rust-change note, files-touched list, and one-page model descriptions. Demo video + brainlift are external artifacts.

**M4 exit (DoD).** All three validated on held-out data; ablation reported (incl. nulls); both apps packaged, crash-safe, benchmarked; deliverables submitted.

---

## 4. Dependency-ordered task DAG

Tasks are `Tn`. **Critical path is bold.** "∥" = can run in parallel once dependencies are met.

| Task    | Description                                                                                                                                            | Depends on      | Parallelizable with |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------- | ------------------- |
| **T0**  | **M0: fork, build desktop, trivial Rust→UI RPC, Android engine build**                                                                                 | —               | —                   |
| T1      | Provision `SpeedrunQuestion` notetype + `Speedrun::Questions` deck + tag taxonomy helpers (D-13)                                                       | T0              | T2, T3              |
| T2      | Blueprint config schema + loader (`blueprint.rs`) (D-3)                                                                                                | T0              | T1, T3              |
| T3      | `proto/anki/speedrun.proto` + `SpeedrunService`/`BackendSpeedrunService` skeleton + bindings (D-15)                                                    | T0              | T1, T2              |
| **T3a** | **Shared topic mastery/weakness map helper (`rslib/src/speedrun/mastery.rs` → `topic_mastery_map`, `topic_weakness_map`); sole owner of `mastery.rs`** | T1, T3          | T4                  |
| **T4**  | **M1 1a: gated activation + linkage + `Op::ActivateForMiss` (+ Rust/Python tests)**                                                                    | T1, T3          | T5, T3a             |
| **T5**  | **M1 1a: activated-card value ordering (`speedrun_value.rs` + 2-line builder edit); consumes T3a weakness map**                                        | T2, T3, **T3a** | T4                  |
| T6      | M1 1a: coverage sweep + `Op::CoverageSweep` (+ test)                                                                                                   | T4              | —                   |
| T7      | M1 1b: Memory model (`GetMemoryScore`; consumes `mastery.rs` from T3a)                                                                                 | T1, T3, **T3a** | T5, T6              |
| T8      | M1 1b/1c: dashboard skeleton (Memory tile) + abstention UI                                                                                             | T7              | T9                  |
| T9      | M1 1c: desktop installer branding/AI-off + Android review build                                                                                        | T0              | T8                  |
| T10     | M2 2b: question-first session UI + miss-reason classifier + `record_miss_reason` (D-11/D-14)                                                           | T4, T6          | T11, T12            |
| T11     | M2 2a: AI generator + eval + leakage + baseline + AI-off (D-4/D-5)                                                                                     | T1              | T10, T12            |
| T12     | M2 2c: sync verification (offline reconcile) + conflict doc + hosting (D-1)                                                                            | T0              | T10, T11            |
| T13     | M3 3a: Performance model (`performance.rs`, RPCs) + Python reference (D-7/D-12)                                                                        | T7, T10         | T14                 |
| T14     | M3 3b: Readiness model (`readiness.rs`, MC + concordance + abstention) + progress view (D-6/D-8)                                                       | T2, T13         | —                   |
| T15     | M4 4a: held-out validation (Memory/Performance/Readiness)                                                                                              | T7, T13, T14    | T16                 |
| T16     | M4 4b: interleaving ablation (3 builds, equal time) (D-9/D-10)                                                                                         | T10, T14        | T15                 |
| T17     | M4 4c: robustness + `just bench` + sync-conflict + packaging                                                                                           | T9, T12, T14    | T15, T16            |
| T18     | M4 4d: hand-in (docs, demo, model one-pagers, brainlift)                                                                                               | T15, T16, T17   | —                   |

**Parallel-vs-sequential summary.** After **T0**, prep tracks **T1/T2/T3** fan out; the shared mastery helper **T3a** (depends on T1/T3) then feeds **both** value-ordering **T5** and Memory **T7** — `mastery.rs` is owned solely by T3a, so neither T5 nor T7 re-creates it. The marquee converges on **T4** (activation) and **T5** (value ordering, gated by T3a). AI (T11) and sync (T12) parallelize against the study chain **T4 → T6 → T10**. Models (T13→T14) gate validation/ablation (T15/T16). **Critical path (longest chain): T0 → T3 → T4 → T6 → T10 → T13 → T14 → (T15/T16/T17) → T18.** The mastery branch **T3 → T3a → T7 → T13** and value-ordering **T5** (both gated by T3a) run in parallel and re-join at **T13** (which depends on both T7 and T10), but sit off the longest chain.

---

## 5. Suggested agent/team assignment & hand-off contracts

| Agent                  | Owns                                                            | Tasks                             | Primary dirs                                                                                                                                                      |
| ---------------------- | --------------------------------------------------------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Engine/Rust**        | mastery helper, gating, ordering, sweep, performance, readiness | **T3a**, T4, T5, T6, T7, T13, T14 | `rslib/src/speedrun/`, `rslib/src/scheduler/queue/builder/`, `rslib/src/scheduler/bury_and_suspend.rs` (visibility bump), `rslib/src/ops.rs`, `rslib/src/config/` |
| **Schema/Models-data** | notetype, deck, tags, blueprint config                          | T1, T2                            | `rslib/src/notetype/`, `pylib/anki/models.py`, `pylib/anki/stdmodels.py`, `rslib/src/config/string.rs`                                                            |
| **Protobuf/IPC**       | proto + bindings + service wiring                               | T3 (+ reviews all proto edits)    | `proto/anki/speedrun.proto`, `rslib/proto*/`                                                                                                                      |
| **Python/Qt**          | helpers, reviewer/session hosting, AI, installer                | T9, T10 (Qt half), T11            | `pylib/anki/speedrun*`, `qt/aqt/speedrun/`, `qt/aqt/reviewer.py`, `qt/installer/`                                                                                 |
| **TS/UI**              | question surface, dashboards, progress                          | T8, T10 (web half), T14 (UI)      | `ts/routes/speedrun/`, `ts/lib/components/`, `ts/reviewer/`                                                                                                       |
| **Sync/Android**       | sync verification, conflict doc, Android parity                 | T12, T9 (Android)                 | `rslib/src/sync/`, `qt/aqt/sync.py`, AnkiDroid repo                                                                                                               |
| **Validation/QA**      | eval, validation, ablation, bench, packaging                    | T15, T16, T17, T18                | `pylib/anki/speedrun/{eval,validation,ablation}.py`, `tools/bench/`, `qt/installer/`                                                                              |

**Hand-off contracts (the stable interfaces between agents).**

- **C1 — Data model (Schema → everyone).** Field order of `SpeedrunQuestion`; exact tag strings (`topic::`, `pool::served|heldout`, `miss::{knowledge-gap,missing-context,misunderstanding,careless}`, `gates::<nid>`); deck name `Speedrun::Questions`. Frozen before T4.
- **C2 — Gating RPC (Engine ↔ Protobuf ↔ Python/TS).** `ActivateCardsForMiss`, `RunCoverageSweep`, `MissReason` enum, `ActivateCardsResponse` (returns `OpChanges` + ids). Frozen by T3; consumed by T10.
- **C3 — Score RPCs (Engine ↔ UI ↔ Android).** `GetMemoryScore`, `PredictPerformance`/`EstimateAbility`, `GetReadiness` (median/low/high/coverage/confidence/updated_at/reasons/abstained). Consumed by T8/T14 and Android (T12).
- **C4 — Blueprint config (Schema ↔ Engine).** JSON shape for topic list + section/topic weights in collection config. Consumed by T5 (ordering), T7 (coverage), T14 (readiness).
- **C5 — Miss persistence (Python ↔ Engine).** `record_miss_reason` writes only the latest `miss::<reason>` **note tag** (no `custom_data`), then calls C2; per-attempt correctness/time history is read from `revlog`. Defined by D-11.

---

## 6. Risk register

| #                                          | Risk                                                                                                                                                                                                                                                                                 | Likelihood × Impact | Mitigation / concrete steps                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **R1 (TOP — biggest integration unknown)** | **Android consumption of `SpeedrunService`.** AnkiDroid is a **separate repo**; the new RPCs reach Android only after its backend is regenerated _and_ Kotlin glue + UI are written there. Almost none of this lives in this repo, so it is the least-controllable part of the plan. | High × High         | **Owner: Sync/Android agent.** Concrete steps: (1) pin AnkiDroid's `rsdroid`/backend dependency to this fork's `rslib` commit; (2) regenerate AnkiDroid's protobuf/JNI backend so `SpeedrunService` (+ the score RPCs) are exposed to Kotlin; (3) smoke-test each new RPC over JNI from Kotlin against a sample collection; (4) build the Kotlin question surface + three-score tiles; (5) add a CI job that periodically builds AnkiDroid against the fork to catch drift. **Mitigations:** keep ALL scoring in Rust (D-12) so Android needs only thin UI; **freeze the C2/C3 RPC contracts at T3** so Kotlin work proceeds in parallel; gate Android UI behind a feature flag so a lagging port never blocks the desktop track. |
| R2                                         | **Touching the queue builder** (`builder/mod.rs`) breaks scheduling/burying                                                                                                                                                                                                          | Med × High          | Keep comparison logic in NEW `speedrun_value.rs`; upstream edit is **2 lines** (`mod speedrun_value;` + one guarded `sort_by_value` call) behind `BoolKey::SpeedrunOrdering`, plus added builder/`Context` fields; rely on suspended-exclusion in `due_cards.sql` (no SQL edits); regression-test against the existing `builder/mod.rs` tests.                                                                                                                                                                                                                                                                                                                                                                                    |
| R3                                         | **FSRS coupling** — accidentally altering intervals/due dates                                                                                                                                                                                                                        | Med × High          | Activation/sweep only flip `queue`; never call state updaters. AC-6 test compares scheduling fields before/after; reuse read-only `current_retrievability_seconds` for mastery.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| R4                                         | **Undo/integrity regressions**                                                                                                                                                                                                                                                       | Med × High          | Every mutation via `transact(Op::…)`; reuse `unsuspend_or_unbury_searched_cards` (bumped to `pub(crate)`); required undo+integrity test (1a #4) + manual Check Database.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| R5                                         | **New-service wiring** (`SpeedrunService`) not picked up                                                                                                                                                                                                                             | Low × Med           | Provide paired empty `BackendSpeedrunService` (required by `get_services`); full `just check` to regenerate; validate the trivial RPC early in M0 (T0/T3).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| R6                                         | **AI eval gating** lets bad cards through / leakage                                                                                                                                                                                                                                  | Med × High          | Pre-register cutoff; block on fail; leakage scan on every run; planted-held-out test (4a); AI-off path keeps scoring.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| R7                                         | **Miss-reason has no native _per-event_ home** on `revlog`                                                                                                                                                                                                                           | Med × Med           | D-11: keep the **latest** reason as a queryable `miss::<reason>` note tag (sufficient for the gating ACs); correctness/time history is already native in `revlog`; **not** `card.custom_data` (100-byte / 8-byte-key cap shared with FSRS); adopt `SpeedrunMissEvent` notes only if full per-event history is later required.                                                                                                                                                                                                                                                                                                                                                                                                     |
| R8                                         | **Blueprint/content licensing** (AAMC outline, MCAT decks)                                                                                                                                                                                                                           | Med × High          | D-3/D-4/D-16: confirm sources; keep generation inputs in gitignored `extra/`; document provenance.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| R9                                         | **Readiness over-confidence** (uncalibrated interval)                                                                                                                                                                                                                                | Med × High          | Coverage-tied widening + abstention (D-6); held-out calibration + recalibration in 4a.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| R10                                        | **`.proto` change forgotten full-build** ⇒ stale bindings                                                                                                                                                                                                                            | Med × Low           | Convention reminder; CI runs `just check`; never trust `cargo check` for proto edits.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| R11                                        | **Performance model collapses into Memory**                                                                                                                                                                                                                                          | Med × High          | Weakest-link + difficulty features; explicit gap test (3a) + paraphrase test (4b).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |

---

## 7. Open Decisions for Human Approval

> Each item: **question**, **recommendation (marked)**, **rationale**, **impact/blast radius**. Items 1–10 are the `Anki_Plan.md` open decisions; D-11…D-17 are ambiguities surfaced during codebase verification. Locked items (MCAT, Android-only, native-object storage) are settled and excluded.

**D-1 — Sync hosting (AnkiWeb vs self-hosted).** **Recommend: self-hosted sync server** (`rslib/src/sync/http_server/`, `docs/syncserver/`). _Rationale:_ full control + reliable **offline demos** for M2/M4. _Impact:_ config + ops only; code path identical (sync is reused). Blocks T12 demo setup.

**D-2 — Rust engine change scope (full gating vs points-at-stake first).** **Recommend: land the full gating engine** (activation + value-ordering + sweep), but **sequence value-ordering (T5) as the first landable slice** if time-pressured, per the brief. _Rationale:_ value-ordering is self-contained and demoable; activation/sweep layer cleanly on top. _Impact:_ defines M1 1a scope; gates T4/T5/T6.

- **D-2a — Card↔question linkage (`topic::` tags vs explicit `gates::`).** **Recommend: shared `topic::` tags as default, with optional `gates::<nid>`** for precision. _Rationale:_ tags require no extra authoring and already drive coverage; `gates::` is opt-in where topic linkage is too coarse. _Impact:_ `linkage.rs` (T4), authoring workflow.
- **D-2b — Which miss-reasons activate.** **Recommend: confirm `knowledge-gap` + `missing-context` activate; `misunderstanding` + `careless` do not** (matches SPOV3). _Impact:_ the core gating rule (AC-2); hard-coded in `activation.rs`.
- **D-2c — Sweep cadence (how often / how large).** **Recommend: a triggerable sweep plus an every-N-sessions default (e.g., N=5) with `sample_size ≈ 1–2 cards/topic`,** tunable in config. _Rationale:_ anti-tunneling without flooding the queue. _Impact:_ `sweep.rs` + session scheduling (T6/T10).

**D-3 — MCAT blueprint source.** **Recommend: encode the official AAMC content outline** (sections + topic weights) as collection config; allow override via a provided file. _Rationale:_ authoritative; feeds ordering/coverage/readiness. _Impact:_ `blueprint.rs`/config schema (T2); licensing (D-16).

**D-4 — Card/content source (deck + AI source text).** **Recommend: pick one openly-licensed MCAT deck + one source text** for the M2 eval; document provenance. _Rationale:_ licensing safety for an AGPL public fork. _Impact:_ T11 inputs; held-out split.

**D-5 — AI provider (model/API; offline required?).** **Recommend: a hosted API for generation (desktop-only) with AI strictly optional**; no offline/local generation requirement (Android uses AI-off). _Rationale:_ keeps cost bounded and Android simple. _Impact:_ `ai.py` (T11); AI-off guarantee.

**D-6 — Abstention thresholds (N graded responses, coverage %, max interval width).** **Recommend starting values: N = 30 graded responses, coverage ≥ 60%, abstain if 80% interval width > 16 scaled points** — tune on real data in 4a. _Rationale:_ avoids early over-claiming. _Impact:_ `mastery.rs`/`readiness.rs` (T7/T14); validation (T15).

**D-7 — Performance model choice (2PL IRT vs logistic/GBM; guessing floor).** **Recommend: 2PL IRT with guessing floor c = 0.25** (4-option MCQ). _Rationale:_ interpretable `θ`/`b`/`a`, natural `se` for readiness uncertainty, preserves the memory→performance gap. _Impact:_ `performance.rs` (T13); 3a tests.

**D-8 — Raw→scaled mapping (concordance data).** **Recommend: a documented monotonic concordance curve approximating AAMC raw→scaled (472–528)**, stated as an approximation. _Rationale:_ exact tables may be restricted; monotonic curve is defensible. _Impact:_ `readiness.rs` (T14); validation.

**D-9 — Ablation feature + pre-registered metric.** **Recommend: interleaving**; pre-registered metric: _"held-out question accuracy after equal study time is higher for interleaved than blocked; failure if Δ ≤ 0."_ _Rationale:_ natively supported (interleaved serving); clean on/off flag. _Impact:_ `ablation.py` + config flag (T16).

**D-10 — Test "learners" (real vs synthetic; min n).** **Recommend: a synthetic/seed response set (simulated students) with n ≥ 20 profiles** for repeatability, plus any available real attempts as a bonus. _Rationale:_ deterministic, privacy-safe, re-runnable. _Impact:_ 4a/4b harness (T15/T16).

**D-11 — Miss-reason persistence (discovered).** `revlog` rows carry no tag/freeform field; tags attach to notes. **Recommend: store only the latest reason as a queryable `miss::<reason>` note tag.** This satisfies every gating acceptance criterion, because activation consumes the _current_ miss event's reason directly from the RPC argument and never reads reason history back. Per-attempt **correctness / time / timestamp history already lives natively in `revlog`** (`RevlogEntry`, `rslib/src/revlog/mod.rs:36`), so no extra store is needed for it. **`card.custom_data` cannot serve here:** `validate_custom_data` (`rslib/src/storage/card/data.rs:135-149`) caps it to a JSON object, keys ≤ 8 bytes, total < 100 bytes, shared with FSRS keys. _If_ full **per-event miss-reason history** is later deemed required, the proven native fit is one **`SpeedrunMissEvent` note per miss** (fields `question_nid`, `reason`, `ts`; arbitrary-length note fields; syncs natively; unbounded history without a side table). _Impact:_ `record_miss_reason` (C5/T10), coverage queries. **Question for the human:** is latest-reason-only acceptable, or is full per-event reason history required (→ adopt `SpeedrunMissEvent`)?

**D-12 — Where the score models live (discovered).** Android (2c) must show all three scores, but Android cannot run Python. **Recommend: implement Memory/Performance/Readiness computations in Rust (`SpeedrunService`); keep a Python reference only for offline validation.** _Rationale:_ Android parity + one source of truth; Python stays for eval/AI. _Impact:_ T7/T13/T14 placement; more Rust effort (accepted).

**D-13 — `SpeedrunQuestion` notetype provisioning (discovered).** Adding a **stock** notetype touches the `StockNotetype` proto enum and the order-sensitive `all_stock_notetypes` list (high merge risk; `stock.rs` comment warns order must match the enum). **Recommend: provision `SpeedrunQuestion` at runtime via a helper/migration (additive, low merge risk)** rather than as a new stock kind. _Impact:_ T1; keeps the fork mergeable.

**D-14 — Question serving mechanism (discovered).** **Recommend: reuse Anki filtered-deck machinery** to serve interleaved `pool::served` questions (answers flow through the native `answer_card`/`revlog` path), instead of a bespoke queue. _Rationale:_ maximal reuse, native `revlog`, less new engine code. _Impact:_ T10; fallback is a `NextServedQuestion` RPC.

**D-15 — New proto file vs extending `scheduler.proto` (discovered).** **Recommend: a dedicated `proto/anki/speedrun.proto` + `SpeedrunService`/`BackendSpeedrunService`** (additive, auto-globbed, low merge risk) over editing upstream `scheduler.proto`. _Impact:_ T3; also affects the `just bench` recipe naming (map brief's `make bench` → `just bench`).

**D-16 — Licensing of MCAT content & AAMC blueprint (discovered).** **Recommend: legal confirmation before shipping any AAMC-derived weights or third-party deck content** in a public AGPL fork; keep raw inputs in gitignored `extra/`. _Impact:_ D-3/D-4 deliverables; hand-in (T18).

**D-17 — `topic_weight` source for value ordering (discovered).** **Recommend: source `topic_weight` from the same blueprint config (D-3)** so ordering, coverage, and readiness share one source of truth. _Impact:_ `value_order.rs` (T5), `blueprint.rs` (T2).

---

## 8. Validation / exit criteria per gate

Each gate must pass `just check` (format + build + lint + all tests) as the final step, plus the gate-specific demos below.

**M0 — Foundation.**

- [ ] Clean from-source build (`just build` → `just run`) on a fresh machine; web pages serve at `:40000`.
- [ ] Trivial Rust value visible in the desktop UI (Rust→protobuf→Python loop proven).
- [ ] AnkiDroid build on the shared engine opens a deck.
- _Demo:_ launch desktop showing the new value; open a deck on Android.

**M1 — Engine change + Memory + installs.**

- [ ] Gating Rust change merged with **≥3 Rust unit tests + 1 Python RPC test** green (`just test-rust`, `just test-py`).
- [ ] Cards suspended by default; only `knowledge-gap`/`missing-context` misses activate; queue built from activated cards, **value-ordered**; FSRS intervals unchanged (AC-6 test).
- [ ] Undo reverts activation/sweep in one step; collection-integrity check passes.
- [ ] Memory score displays with a range; abstains below threshold.
- [ ] Desktop installer runs clean; Android reviews the shared deck.
- _Artifacts:_ diff, undo+integrity proof, "why Rust" one-pager, upstream-files-touched + merge notes.

**M2 — AI + sync + question loop.**

- [ ] AI eval report (accuracy + wrong-answer rate) meets the pre-set cutoff; failing cards blocked; baseline head-to-head table produced; leakage scan clean (planted held-out detected in test).
- [ ] AI-off path still produces all three scores.
- [ ] Question-first loop: served-only questions interleaved; misses classified; gated activation unsuspends the right cards (only qualifying misses); reviews written to `revlog`.
- [ ] Triggerable coverage sweep re-activates across topics.
- [ ] Two-way offline reviews reconcile on reconnect (none lost/double-counted); conflict rule documented; Android shows the three scores.
- _Demo:_ miss→classify→activate; sweep; offline-both-then-sync reconcile.

**M3 — Performance + Readiness.**

- [ ] Performance model beats chance on held-out responses; a measurable memory→performance gap exists (weakest-link/difficulty features).
- [ ] Readiness emits median + 80% interval + coverage + confidence + reasons; widens with low coverage; **abstains** below N/coverage/width thresholds.
- [ ] Progress view shows rising practice-question performance.
- _Demo:_ dashboard with three scores + ranges; force abstention on thin data.

**M4 — Validation, ablation, packaging, hand-in.**

- [ ] Memory: reliability diagram + Brier/log-loss on held-out reviews (recalibrated if needed).
- [ ] Performance: accuracy/AUC on `pool::heldout` + paraphrase-test gap reported.
- [ ] Readiness: method + range documented (bonus: vs. real practice tests).
- [ ] Ablation: 3 builds at equal study time, pre-registered metric, **nulls reported honestly**.
- [ ] Robustness: 20× mid-review kills/app → zero corruption; offline degrades cleanly while scoring.
- [ ] `just bench` prints p50/p95/worst per action on the 50k deck within budget (button ack p95 < 50ms; next card p95 < 100ms; dashboard load p95 < 1s / refresh < 500ms; no UI freeze > 100ms).
- [ ] Sync-conflict test: same card on both devices offline → documented correct winner.
- [ ] Desktop installer + signed Android APK, both run AI-off.
- [ ] Hand-in: public AGPL fork (exam stated, build instructions, architecture overview, Rust-change note, files-touched list); 3–5 min demo video; one-page model descriptions (incl. give-up rule); brainlift; honest results report incl. failures.

---

### Appendix — verified anchor index (high-leverage, confirmed in tree)

- Suspend/unsuspend + undo: `rslib/src/scheduler/bury_and_suspend.rs` (`unbury_or_unsuspend_cards` → `transact(Op::UnburyUnsuspend, …)`, `unsuspend_or_unbury_searched_cards`, `Card::restore_queue_after_bury_or_suspend`).
- Queue build/order: `rslib/src/scheduler/queue/builder/{mod,gathering,sorting,burying,intersperser,sized_chain}.rs`; suspended-exclusion via `rslib/src/storage/card/due_cards.sql`; order subclauses in `rslib/src/storage/card/mod.rs` (`ReviewOrderSubclause`, `review_order_sql`).
- Undo/op machinery: `rslib/src/ops.rs` (`Op`, `OpOutput`, `OpChanges`), `rslib/src/undo/`.
- FSRS retrievability: `rslib/src/stats/graphs/retrievability.rs` (`current_retrievability_seconds`, `Card.memory_state`); FSRS plumbing `rslib/src/scheduler/fsrs/`.
- Answering/revlog: `rslib/src/scheduler/answering/mod.rs` (`answer_card`, `add_revlog_entry_undoable`, `CardAnswer.custom_data`), `rslib/src/revlog/mod.rs`.
- Notes/notetypes/tags: `rslib/src/notes/`, `rslib/src/notetype/stock.rs`, `rslib/src/tags/`; `pylib/anki/{notes,models,tags}.py`, `pylib/anki/stdmodels.py`.
- Search (linkage): `SearchNode` **enum defined in `rslib/src/search/parser.rs`** (`Tag { tag, mode }` at `:96`, `Notetype`, `DeckIdWithChildren`, `NotetypeId`); constructor/builder helpers in `rslib/src/search/builder.rs`.
- Protos: `proto/anki/{scheduler,cards,notes,notetypes,tags,frontend,ankidroid,stats,collection,generic}.proto`; `Card.custom_data` in `cards.proto`.
- Service wiring/codegen: `rslib/src/services.rs`, `rslib/proto_gen/src/lib.rs` (`get_services`), `rslib/proto/{build.rs,rust.rs,python.rs,typescript.rs}`, `build/configure/src/web.rs` (`glob!["proto/**/*.proto"]`).
- Config: `rslib/src/config/{bool,string,number}.rs`.
- Sync (reuse): `rslib/src/sync/**`; `pylib/anki/collection.py` (`sync_collection`, `full_upload_or_download`); `qt/aqt/sync.py`.
- Desktop GUI: `qt/aqt/{reviewer,mediasrv,deckbrowser,overview,main}.py`; installer `qt/installer/{mac,linux,windows}-template/`.
- Web: `ts/reviewer/`, `ts/routes/`, `ts/lib/components/`; e2e `ts/tests/e2e/`.
- i18n: `ftl/core/*.ftl`, `ftl/qt/`.
- **NEW (to be created):** `proto/anki/speedrun.proto`; `rslib/src/speedrun/{mod,activation,linkage,sweep,mastery,blueprint,value_order,performance,readiness,service}.rs`; `rslib/src/scheduler/queue/builder/speedrun_value.rs`; `pylib/anki/speedrun.py` + `pylib/anki/speedrun/{ai,eval,leakage,baseline,validation,ablation,models_ref}.py`; `qt/aqt/speedrun/`; `ts/routes/speedrun/{session,dashboard,progress}/`; `ftl/core/speedrun.ftl`; `pylib/tests/test_speedrun.py`; `tools/bench/` + `just bench` recipe; `docs/speedrun/`.
