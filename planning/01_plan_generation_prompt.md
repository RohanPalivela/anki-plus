# PROMPT — Generate the Technical Implementation Plan for the Anki → MCAT "Speedrun" Fork

## Your role

You are a **senior engineer + technical planner** working inside the `anki-plus`
repository (a fork of Anki). Your job in this task is to **produce a detailed,
buildable technical implementation plan** — **NOT to write production code**. The
plan you produce will be (a) reviewed by another agent, (b) approved by a human,
and (c) then handed to an agent team that will implement it. Optimize the plan so
that a competent agent team can execute it with minimal further discovery.

## Hard rule for THIS task

- **Do NOT modify any source code, protobuf, ftl, or build files.** The ONLY file
  you may create/write is the plan itself at:
  **`planning/Anki_Implementation_Plan.md`**
- You MAY read anything, run read-only searches, and run **non-mutating** inspection
  commands. Do not run builds that mutate the tree beyond `out/` if avoidable; prefer
  `cargo check`-level reasoning over full builds. Never leave the repo dirty outside
  the plan file.

## Required reading (read these first, in order)

1. `Anki_Plan.md` — the **product + milestone spec** (the source of truth for WHAT to
   build: M0–M4, contracts, acceptance criteria, and the SPOV3 product thesis).
   Treat its "Agent operating rules", "Locked decisions", and "Open decisions to
   confirm with the user" as binding.
2. `CLAUDE.md` (a.k.a. `AGENTS.md`) — build/run/test/lint commands (all via `just`),
   conventions (Rust error handling, dependency rules, ftl, protobuf/IPC), and the
   "do this before completing a task" gate (`just check`).
3. `CODEBASE_PRIMER.md` — architecture map, cross-language codegen boundaries, the
   "Where Do I Make This Change?" playbook, testing matrix, and pitfalls.
4. Skim these repo docs for accuracy on the cross-language contract:
   `docs/architecture.md`, `docs/language_bridge.md`, `docs/protobuf.md`,
   `docs/build.md`, `docs/e2e-testing.md`, `proto/README.md`.

## Verified codebase anchors (use these; verify and expand them yourself)

These were confirmed to exist and are the highest-leverage starting points. The plan
must reference concrete paths/symbols like these (find more as needed — do not rely
solely on this list).

**Marquee engine change — question-gated card activation (M1 1a):**

- Atomic, undo-safe suspend/unsuspend already exists:
  `rslib/src/scheduler/bury_and_suspend.rs` →
  `Collection::unbury_or_unsuspend_cards(&mut self, cids: &[CardId]) -> Result<OpOutput<()>>`,
  which wraps work in `self.transact(Op::UnburyUnsuspend, |col| …)`. **Reuse this
  pattern** so activation is atomic + undoable; do not reinvent state transitions.
- Review-queue construction + ordering lives under
  `rslib/src/scheduler/queue/builder/` (`mod.rs`, `gathering.rs`, `sorting.rs`,
  `burying.rs`, `intersperser.rs`). The activated-card queue + `value = topic_weight ×
  weakness` ordering belongs here (or in a sibling module that feeds it).
- Scheduler protobuf + service: `proto/anki/scheduler.proto`,
  `rslib/src/scheduler/service/mod.rs`. Card-state protos: `proto/anki/cards.proto`.

**Memory model (M1 1b) — FSRS retrievability R(t):**

- `rslib/src/stats/graphs/retrievability.rs` already computes per-card retrievability
  via `fsrs.current_retrievability_seconds(state.into(), elapsed_seconds, decay)` using
  `card.memory_state`. Reuse this to expose per-card `R(t)` and aggregate to per-topic
  mastery. FSRS plumbing lives under `rslib/src/scheduler/fsrs/`.

**Native-object data model (questions/tags/reviews):**

- Notes/notetypes: `rslib/src/notes/`, `rslib/src/notetype/`, `proto/anki/notes.proto`,
  `proto/anki/notetypes.proto`. Tags: `rslib/src/tags/`, `rslib/src/storage/tag/`,
  `proto/anki/tags.proto`. Reviews/`revlog`: `rslib/src/revlog/`,
  `rslib/src/scheduler/answering/`. The `SpeedrunQuestion` note type, `topic::` /
  `pool::` / `miss::` / `gates::` tags, and the `Speedrun::Questions` deck must all be
  **native Anki objects** (notes/tags/cards/revlog) — never a side SQLite table.

**RPC plumbing / cross-language wiring:**

- Adding a Rust-implemented RPC: declare it in the relevant `proto/anki/<area>.proto`
  (a non-`FrontendService` service) → implement in `rslib/src/<area>/service.rs` →
  full build (`just check`) to regenerate bindings → call from Python via
  `col._backend.<rpc>()` wrapped in a `pylib/anki/<area>.py` helper, and/or from TS via
  `@generated/backend`. Services are wired in `rslib/src/services.rs`.
- Python-implemented RPCs go in `FrontendService` (`proto/anki/frontend.proto`),
  handled in `qt/aqt/` (often routed via `qt/aqt/mediasrv.py`).

**Sync (reuse, don't rewrite):** `rslib/src/sync/`. The plan must reuse existing
object-based sync; native objects sync "for free".

**Android:** `proto/anki/ankidroid.proto` and AnkiDroid sharing the Rust backend
(separate repo / build) — the plan must describe how the shared engine reaches Android
and what (if anything) needs to change here vs. in AnkiDroid.

## Operating rules for the plan

- **Respect the LOCKED decisions** (do not re-litigate): MCAT only (scale 472–528, 4
  sections 118–132); Android-only mobile via AnkiDroid (iOS out of scope); all custom
  data stored as **native Anki objects**.
- **Ask-don't-guess, but you cannot reach the human.** Therefore: whenever a decision
  is ambiguous, **do not silently pick a default**. Instead record it in the plan's
  **"Open Decisions for Human Approval"** section with: the question, **your recommended
  option (clearly marked)**, the rationale, and the blast radius (what downstream work
  depends on it). You MUST address **every one of the 10 open decisions** listed at the
  bottom of `Anki_Plan.md`, plus any new ambiguities you discover.
- **Keep the fork mergeable:** prefer additive changes (new modules, new RPCs, new
  notetypes/tags) over rewriting upstream functions. For every upstream file the plan
  proposes to modify, note it and give a merge-difficulty estimate (low/med/high).
- **Honor the non-negotiables:** undo-safety + collection-integrity for every engine
  change; never emit a readiness number without evidence (point + range + coverage +
  confidence + abstention); performance budgets (button ack p95 < 50ms; next card p95
  < 100ms; dashboard load p95 < 1s / refresh < 500ms; no UI freeze > 100ms; report on a
  50k-card deck).

## Required structure & content of `planning/Anki_Implementation_Plan.md`

Write in clear markdown. Be concrete: cite **exact file paths** and **proposed symbol
names**, give **protobuf message/RPC signatures**, and define **native data-model
objects** precisely. The plan must be detailed enough to implement without re-deriving
the architecture.

1. **Executive summary** — what we're building (SPOV3: practice-questions gate card
   activation), the central gate (a real Rust-core change), and the milestone arc.
2. **Architecture & data model** —
   - The native-object schema: `SpeedrunQuestion` notetype (fields: `stem`, `options`,
     `correct`, `explanation`, `source`, `difficulty_b`, `discrimination_a`), deck
     layout (`Speedrun::Questions`), tag taxonomy (`topic::<name>`, `pool::served|heldout`,
     `miss::knowledge-gap|missing-context|misunderstanding|careless`, optional
     `gates::<note_id>`), and how a question-answer maps to a `revlog` review
     (`Again`=incorrect, `Good`=correct) + miss-reason tag.
   - How each derived quantity is computed from native objects (served/heldout =
     tag filter; coverage = tag query; leakage = tag/text scan; activation =
     deterministic function of missed question → linked cards).
3. **Per-milestone, per-feature work breakdown** — for **M0, M1 (incl. 1a/1b/1c), M2
   (2a/2b/2c), M3 (3a/3b), M4 (4a/4b/4c/4d)**. For **each** feature provide:
   - **Objective & acceptance criteria** (lift from `Anki_Plan.md`; make them testable).
   - **Files to create/modify** — exact paths, split into `proto/`, `rslib/`, `pylib/`,
     `qt/aqt/`, `ts/`, `ftl/`, build, tests. Mark upstream-modified files + merge risk.
   - **Protobuf changes** — message definitions + RPC signatures, which service, and the
     Rust-vs-Python routing decision (per the `FrontendService` rule).
   - **Rust implementation approach** — modules/functions to add, and which existing
     primitives to reuse (e.g. `transact(Op::…)`, `unbury_or_unsuspend_cards`,
     `current_retrievability_seconds`). Note `Op`/undo additions needed.
   - **Python + Qt wiring** — `pylib/anki/<area>.py` helper(s) and `qt/aqt/` flow.
   - **TS/Svelte UI** — routes/components under `ts/routes/` + `ts/lib/components/` for
     the question-first study surface, dashboards (three scores w/ ranges + abstention),
     and progress/motivation view.
   - **Expected result** — the observable behavior when done.
   - **Testing procedure** — specific tests + commands. For M1 1a explicitly plan the
     **≥3 Rust unit tests + 1 Python-side RPC test** required by `Anki_Plan.md`
     (activation only for `knowledge-gap`/`missing-context`; no-op for
     `misunderstanding`/`careless`; queue excludes suspended cards & orders activated
     ones by value; sweep spreads across topics). Map each to `just test-rust` /
     `just test-py` / `just test-ts` / `just test-e2e`.
   - **Perf / undo / integrity notes** — how it stays within budget and stays undo-safe.
   - **Required artifacts** (for M1 1a especially): the diff, undo + integrity proof, the
     1-page "why this belongs in Rust", and the upstream-files-touched + merge note.
4. **Dependency-ordered task DAG** — a numbered task list with explicit dependencies and
   a parallel-vs-sequential map, designed so an **agent team** can pick up independent
   tasks concurrently (call out the critical path: M0 → M1 1a → everything else).
5. **Suggested agent/team assignment** — how to split the DAG across parallel agents
   (e.g., engine/Rust agent, Python/Qt agent, TS/UI agent, models agent, sync/Android
   agent), with clear hand-off contracts (the protobuf/data-model interfaces).
6. **Risk register** — top risks (e.g., touching the queue builder, FSRS coupling,
   Android build, AI eval gating) with mitigations.
7. **Open Decisions for Human Approval** — consolidated, numbered, each with
   **Recommendation + rationale + impact**. Must cover all 10 from `Anki_Plan.md`
   (sync hosting; engine-change scope incl. 2a linkage / 2b miss-reasons / 2c sweep
   cadence; MCAT blueprint source; card/content source; AI provider; abstention
   thresholds; performance-model choice; raw→scaled mapping; ablation feature; test
   "learners") plus any new ones you surface.
8. **Validation/exit criteria per gate** — restate the Definition-of-Done per milestone
   and how we will demonstrate it.

## Quality bar & self-check (do before you finish)

- Verify every file path you cite actually exists (or is clearly marked "NEW").
- Ensure **every** milestone and **every** acceptance criterion in `Anki_Plan.md` is
  covered by at least one task.
- Ensure **every** one of the 10 open decisions is addressed in §7 with a recommendation.
- Give the **marquee Rust engine change (M1 1a)** the deepest detail — it is the central
  grading gate.
- Keep recommendations honest: if something is genuinely uncertain, say so and propose
  the smallest safe first slice (per `Anki_Plan.md`, the points-at-stake ordering is the
  first slice candidate for 1a).

## Output

Write the finished plan to **`planning/Anki_Implementation_Plan.md`** and end your run
with a short summary: the plan's section list, the critical path, and the count + titles
of open decisions needing human approval.
