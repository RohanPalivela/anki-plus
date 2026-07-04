# Speedrun — Sunday Finish Plan (Plan of Record)

> **Deadline:** Sunday 10:59 PM CT.
> **Status when written:** the app is largely built. The remaining work is
> almost entirely **M4 — evidence, validation, robustness, packaging, and
> hand-in artifacts** — plus a few correctness/wiring gaps.
> **Guiding principle:** _Sunday is won or lost on evidence and packaging, not
> features._ Clear the grade **hard caps** first, then land the ablation (15%)
> and paraphrase test, then produce the hand-in bundle.

This file is the single source of truth for the final push. It is derived from a
four-front reconnaissance of all three repos against `Anki_Plan.md` (M1–M4) and
`Anki-Android/instructions.md` (§6 deadlines, §7 challenges, §11 grading, §12
hand-in).

---

## 1. Ground truth — what is already DONE

| Area | State |
| :--- | :--- |
| Rust engine change (question-gated activation + coverage sweep + value-ordered queue `value = topic_weight × weakness`) | **Done.** 10 Rust unit tests + 1 Python-calling-RPC test. `rslib/src/speedrun/`, `proto/anki/speedrun.proto`, `rslib/src/scheduler/queue/builder/speedrun_value.rs`. |
| Memory / Performance / Readiness models (Rust RPCs, abstention rules) | **Done** as code. `mastery.rs`, `performance.rs`, `readiness.rs`, `synthetic.rs`. |
| AI rephrase + eval harness + retrieval baseline + leakage logic | **Done.** `pylib/anki/speedrun_rephrase.py`, `tools/speedrun/rephrase_eval.py`. |
| Question-first study loop + dashboard (desktop + Android) | **Done.** `qt/aqt/speedrun/`, `ts/routes/speedrun-*`, Android `com.ichi2.anki.speedrun.*`. |
| Shared engine on Android (backend AAR built, Speedrun RPCs codegen'd, `local_backend=true`) | **Done.** `Anki-Android-Backend` (fork clone in `anki/`), `tools/android-setup`/`android-run`. |
| Two-way sync + conflict rule | **Done** in engine: Rust test `speedrun_state_syncs_across_devices_offline` + `docs/speedrun/sync.md`. |
| README (MCAT stated, build instructions) | **Done.** |

## 2. Grade hard caps that dominate sequencing (from §11)

- **No held-out testing → 60% max** → needs validation harness.
- **No re-runnable test setup → 60% max** → needs `just bench` + re-runnable evals.
- **Either app won't run on a clean device → 50% max** → needs packaged installer + signed APK verified on clean device.
- **No phone companion that shares engine and syncs → 70% max** → engine done; needs sync **proof** (7b + recordings).
- **Leaked test data → that score is zero** → run leakage, capture clean report.
- **Made-up / misleading readiness numbers → automatic fail** → keep abstention honest; never demo synthetic as real.

## 3. Grading weights (for prioritization)

Rust change 20% · Score accuracy + honest uncertainty 20% · Study feature
(learning science) 15% · AI checking + safety 15% · Fair re-runnable tests 12% ·
Two apps one engine + sync 10% · Useful product + UX 8%.

---

## 4. Workstreams (owners = subagents / branches)

Each workstream is developed on its own branch (`sunday/<slug>`) in an isolated
worktree, then merged into `speedrun-sunday`. Integration build + full test pass
happens on `speedrun-sunday` (needs the existing `out/` build artifacts).

### P0 — remove hard caps (highest priority)

**WS1 — Held-out validation harness** _(removes 60% cap; feeds Score accuracy 20% + Fair tests 12%)_
- New re-runnable harness (e.g. `pylib/anki/speedrun/validation.py` + `just` recipe).
- **Memory:** train/test split of review history → reliability diagram + **Brier or log loss** on held-out reviews; apply Platt/isotonic if miscalibrated.
- **Performance:** accuracy/AUC on `pool::heldout` questions.
- Capture a committed **leakage clean report** (run `tools/speedrun/rephrase_eval.py`, save output).
- **Acceptance:** one command regenerates metrics + plots; numbers written to a results file.

**WS2 — One-command benchmark + 50k deck** _(removes 60% cap; §7h, §10)_
- `just bench` (or `make bench`) loads a **50k-card** deck and prints **p50 / p95 / worst** for: button ack, next card, dashboard load, dashboard refresh.
- Add a 50k-deck generator.
- **Acceptance:** single command prints the table; numbers compared to §10 budgets.

**WS3 — Packaging + clean-device verification** _(removes 50% cap; §12)_
- Desktop installer build path documented + built (`tools/build-installer`).
- **Signed** Android release APK (`assembleRelease` + keystore) — wire signing + doc.
- Runbook to verify both install & launch on a clean machine/emulator.
- **Acceptance:** installer + signed APK produced; clean-device runbook complete.

**WS4 — Sync proof (7b)** _(protects 70% cap; §7b)_
- Runbook + scripts for: 10 offline reviews on phone + 10 different on desktop → reconcile → 20 kept, none doubled; then same-card offline on both → document rule.
- Align the "winner" wording: reviews **keep both revlog rows**; mutable objects last-writer-wins by mod time (per `sync.md`).
- Capture `cargo test -p anki --lib speedrun_state_syncs_across_devices_offline` output.
- **Acceptance:** 7b runbook + conflict write-up + captured test log (recordings are human-run).

### P1 — heaviest single-weight gaps

**WS5 — Study-feature ablation** _(15% of grade; §8, §4b)_
- Pre-register the metric in one sentence (interleaving: "mixing related topics raises accuracy on new mixed-topic questions at equal study time; fail if Δ ≤ 0").
- Add an **interleaving-off** flag/build.
- 3 builds at equal study time: full app / feature-off / plain unmodified Anki. Report a range and **honest nulls**.
- **Acceptance:** re-runnable experiment harness + results writeup (nulls OK).

**WS6 — Paraphrase test** _(§7d; proves Performance ≠ Memory)_
- 30 cards → 2 reworded exam-style Qs each; compare card recall vs reworded accuracy; **report the gap**.
- **Acceptance:** re-runnable script + reported gap number.

### P2 — correctness / robustness

**WS7 — Correctness + robustness**
- Wire **weakest-link** for multi-topic questions in the RPC path (currently only exercised in a unit test; production passes a single mastery).
- Add readiness Rust integration test + width-based abstention test; add one AI-off "all three scores" test.
- **Crash test:** kill mid-review 20× on both platforms → zero corruption; **offline-degradation** test (AI off, scores still work).
- **Acceptance:** tests pass; crash/offline scripts + logs.

### P3 — hand-in bundle

**WS8 — Hand-in docs + demo**
- "Why this belongs in Rust" one-pager + **files-touched + merge-difficulty** list.
- Three one-page **model descriptions** (Memory / Performance / Readiness, incl. give-up rule).
- Architecture overview (Speedrun-specific), **honest results report** (incl. what failed).
- **Brainlift** (confirm format).
- 3–5 min **demo video** script + recording checklist.
- Housekeeping: refresh stale `docs/speedrun/android.md`; bump `Anki-Android/gradle/libs.versions.toml` backend pin; sync backend `anki/` to fork HEAD before final demo.

---

## 5. Cross-cutting correctness flags (decide + fix)

- **Memory contract mismatch:** code uses an **unweighted** mean retrievability
  (documented rationale in `mastery.rs`), but `Anki_Plan.md` and
  `speedrun.proto` say "stability-weighted." Update the docs/proto comment to
  match the code (recommended) or revert — do not leave both.
- **M3 demo hygiene:** Performance/Readiness leave abstention only with enough
  data. For the demo, study a real session (or import the bank) — never present
  `seed_synthetic_responses` data as real. Keep the `synthetic` badge honest.

## 6. Decisions (defaults locked; override anytime)

- **Sync hosting:** AnkiWeb account (fastest; docs already point there).
- **Signed APK:** fresh self-signed release keystore.
- **Ablation feature:** interleaving (already implemented; needs off-flag).

## 7. Branching / version-control strategy

- `main` stays the safe baseline in all three repos.
- Integration branch: **`speedrun-sunday`** (this plan lands here first).
- Each workstream: **`sunday/<slug>`** in an isolated git worktree; merged into
  `speedrun-sunday` after review. Nothing merges to `main` until the integration
  branch builds + passes `just check`.
- If a workstream breaks, discard its branch — `main` and other branches are
  unaffected.

## 8. Suggested sequence

- **Sat:** WS1–WS4 (P0) in parallel; start WS5.
- **Sun AM:** finish WS5 + WS6 + WS7.
- **Sun PM:** WS8 (docs, results, demo video), final packaging + clean-device
  verification, submit before 10:59 PM CT.
