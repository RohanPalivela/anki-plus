# PROMPT — Build the Anki → MCAT "Speedrun" Fork (Phase 1: M0 + M1)

> This is the **combined build prompt** (Step 3). It fuses the Step-1 planning prompt
> (`planning/01_plan_generation_prompt.md`) with the reviewer-approved implementation
> plan (`planning/Anki_Implementation_Plan.md`) and the human-locked decisions, and
> hands the result to an implementation agent/team.

## Your role

You are a senior engineer **implementing** features in the `anki-plus` repository
(`/Users/rohanpalivela/Documents/Github/AlphaAI/anki-plus`), a fork of Anki being turned
into a desktop + Android MCAT study app ("Speedrun"). Build to spec, with tests, keeping
the fork mergeable. You are on the branch `speedrun/m0-m1`.

## Authoritative specs (read first, in order)

1. **`planning/Anki_Implementation_Plan.md`** — THE implementation plan: exact file paths,
   protobuf/RPC signatures, Rust/Python/TS wiring, native data model, the task DAG, the
   agent-team split + interface hand-off contracts (C1–C5), the risk register, and the
   per-gate exit criteria. **Follow it.** Where it cites a path, verify before editing.
2. `planning/01_plan_generation_prompt.md` — context, verified codebase anchors, operating
   rules, and hard constraints.
3. `Anki_Plan.md` — product spec, acceptance criteria, locked decisions, SPOV3 thesis.
4. `CLAUDE.md` and `CODEBASE_PRIMER.md` — build/run/test/lint commands (all via `just`),
   conventions (Rust errors via `AnkiError`/`snafu`; deps in root `Cargo.toml` +
   `dep.workspace = true`; ftl placement; protobuf naming/optionals), and the
   "Where Do I Make This Change?" playbook.

## Scope for THIS phase — M0 + M1 ONLY (do not start M2–M4)

- **M0 — Foundation (GATE, do first):** prove the toolchain end-to-end. Build (`just build`)
  and run; land a **trivial Rust change visible in the desktop UI** (proves the
  Rust→protobuf→Python loop). Treat the full AnkiDroid build as a documented follow-up
  (R1, top risk), not a blocker for this phase.
- **M1 1a — Marquee Rust engine change (DEEPEST PRIORITY; the central grading gate):**
  question-gated card activation + activated-card **value ordering** (`value = topic_weight
  × weakness`) + **coverage sweep**, via a NEW `proto/anki/speedrun.proto`
  (`SpeedrunService` + paired empty `BackendSpeedrunService`) and a NEW `rslib/src/speedrun/`
  module. Reuse `Collection::unbury_or_unsuspend_cards` + `transact(Op::…)` from
  `rslib/src/scheduler/bury_and_suspend.rs` (bump `unsuspend_or_unbury_searched_cards` to
  `pub(crate)`). Implement the required **≥3 Rust unit tests + 1 Python RPC test**.
- **M1 1b — Memory model:** per-card FSRS retrievability (reuse
  `current_retrievability_seconds` from `rslib/src/stats/graphs/retrievability.rs`) →
  stability-weighted per-topic mastery over **activated** cards; expose with a range +
  abstention rule (D-6). The mastery helper (`rslib/src/speedrun/mastery.rs`) is shared with
  value ordering (DAG task **T3a** — create it once).
- **M1 1c — Installs:** desktop installer sanity check; Android scaffolding/notes only.

## Human-LOCKED decisions (use these defaults; do NOT re-ask)

- **D-1 Sync hosting:** self-hosted sync server.
- **D-2 Engine scope:** land the full gating engine; if time-pressured, **value ordering is
  the first landable slice**, then activation, then sweep.
- **D-2a Card↔question linkage:** shared `topic::<name>` tags by default + optional
  `gates::<note_id>` for precision.
- **D-2b Miss-reasons that activate:** ONLY `knowledge-gap` + `missing-context` activate;
  `misunderstanding` + `careless` are no-ops.
- **D-2c Sweep cadence:** triggerable + an every-~5-sessions default; `sample_size ≈ 1–2
  cards/topic`, config-tunable; `sample_size == 0` (proto3 default) ⇒ use the configured
  default, clamp to ≥ 1 (never "sweep nothing").
- **D-3 MCAT blueprint:** encode the official AAMC content outline (sections + topic
  weights) as collection config; allow override by a provided file.
- **D-4 Content source:** one openly-licensed MCAT deck + one source text (M2; document
  provenance). _(Not needed this phase.)_
- **D-5 AI provider:** hosted API, desktop-only, AI strictly optional. _(Not this phase.)_
- **D-6 Abstention thresholds:** N = 30 graded responses, coverage ≥ 60%, abstain if the
  80% interval width > 16 scaled points (tune later).
- **D-7 Performance model:** 2PL IRT, guessing floor c = 0.25. _(M3.)_
- **D-8 Raw→scaled:** documented monotonic concordance approximation (472–528). _(M3.)_
- **D-9 Ablation:** interleaving; metric "held-out accuracy after equal study time higher
  for interleaved; fail if Δ ≤ 0." _(M4.)_
- **D-10 Test learners:** synthetic seed set, n ≥ 20 profiles (+ real attempts as bonus).
- **D-11 Miss-reason persistence:** latest reason as a `miss::<reason>` **note tag**
  (correctness/time history already native in `revlog`); `card.custom_data` is NOT usable
  (100-byte / 8-byte-key cap shared with FSRS). Adopt `SpeedrunMissEvent` notes ONLY if full
  per-event reason history is later required.
- **D-12 Where scores live:** implement Memory/Performance/Readiness in **Rust**
  (`SpeedrunService`) for Android parity; Python is a reference for offline validation only.
- **D-13 `SpeedrunQuestion` notetype:** provision at **runtime via a helper/migration**
  (additive, low merge risk), NOT as a stock-enum notetype.
- **D-14 Question serving:** reuse Anki filtered-deck machinery (answers flow through native
  `answer_card`/`revlog`); fallback is a `NextServedQuestion` RPC. _(M2.)_
- **D-15 Proto layout:** dedicated `proto/anki/speedrun.proto` + `SpeedrunService` /
  `BackendSpeedrunService` (additive, auto-globbed), NOT edits to `scheduler.proto`.
- **D-16 Licensing:** legal confirmation before shipping any AAMC-derived weights/deck
  content; keep raw inputs in a gitignored `extra/`.
- **D-17 `topic_weight` source:** from the same blueprint config as D-3 (one source of truth).

## Operating rules (non-negotiable)

- **M0 GATE FIRST.** Before writing feature code, confirm the build works (`just build`;
  `just check` if feasible). **If the toolchain/build fails for environment/network/sandbox
  reasons, STOP and report the exact error** — do not write feature code into a broken build.
- **Keep the fork mergeable:** additive changes (new proto file, new `rslib/src/speedrun/`
  module, runtime notetype helper). Minimize upstream edits; for every upstream file you
  touch, record it + a merge-difficulty note (low/med/high).
- **Native objects only** (notes/notetypes/tags/cards/`revlog`); never a side SQLite table.
- **Undo-safety & integrity:** every mutation through `transact(Op::…)`; add new `Op`
  variants as needed; activation/sweep must pass a collection-integrity check and be
  single-step undoable. **Do NOT alter FSRS intervals/due dates** — this governs
  activation + ordering only.
- **Protobuf discipline:** `.proto` edits require a **full build** (`just check` / `just
  build`) to regenerate bindings — never trust `cargo check` alone for proto changes.
  Guard proto3 default-value traps (unset = `0`/`""`).
- **i18n:** new strings as `speedrun-*` keys in `ftl/core/speedrun.ftl` (→ `tr.speedrun_*()`);
  `actions-*` keys belong in `actions.ftl`.
- **Tests:** implement and pass the M1 1a tests (≥3 Rust unit + 1 Python RPC). Run
  `just test-rust`, `just test-py`, and `just check`. Use `just fix-fmt` / `just fix-lint`.
- **ASK-DON'T-GUESS:** for any **NEW** ambiguity not covered by the plan or the locked
  decisions above — or anything destructive/scope-changing/new-dependency — **STOP and
  surface it** in your report rather than guessing. You cannot reach the human directly;
  escalate it up to the orchestrator.

## Deliverables / final report

- A working build and the M1 1a engine change with green tests.
- The required **artifacts**: the diff summary; proof undo works + the integrity check
  passes; a 1-page **"why this belongs in Rust"** (atomic state transitions; perf on 50k
  cards; shared by Android); and the **upstream-files-touched + merge-difficulty** list.
- A concise final report: what landed, exact test results (`just test-rust`/`test-py`/
  `check` output), files created/modified, what remains for M1 (1b/1c) and later milestones,
  and any ambiguities you had to surface.
