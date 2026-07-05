# Speedrun results report (honest — includes what didn't work)

This is the spine the final numbers get pasted into. Reference **demo/synthetic**
numbers below prove each harness runs; **TODO** markers are where real
studied-deck / device numbers must replace them before hand-in. Demo numbers are
clearly labelled SYNTHETIC and must never be presented as real learner data.

## 1. Memory calibration (held-out) — `just speedrun-validate`

- Reference (SYNTHETIC): Brier ≈ 0.179, log loss ≈ 0.531, ECE ≈ 0.087; best
  recalibrator = Platt (Brier → 0.170).
- TODO: real held-out review split (deck, date, commit); reliability diagram PNG.

## 2. Performance (held-out questions) — `just speedrun-validate`

- Reference (SYNTHETIC): n=400, accuracy ≈ 0.77, AUC ≈ 0.82 (> 0.5 chance).
- TODO: AUC/accuracy on real `pool::heldout` answers.

## 3. Paraphrase gap — `just speedrun-paraphrase`

- Reference (SYNTHETIC, penalty 0.18): recall 0.769 vs reworded 0.589,
  **gap +0.180** (performance ≠ memory). Null control (penalty 0): gap ≈ 0.
- TODO: real card-recall vs reworded-accuracy gap on a studied deck.

## 4. Study-feature ablation (interleaving, 15%) — `just speedrun-ablation`

- Reference (SIMULATION, transfer 0.25): full 0.964 > ablation 0.855 > plain
  0.760; Δ(full−ablation) = +0.109, Δ(ablation−plain) = +0.095. Sweep shows an
  honest **null at transfer=0**.
- TODO: real/seed-learner 3-build comparison at equal study time (report nulls).

## 5. Readiness — `docs/speedrun/models.md`

- Method + range documented (Monte Carlo, 80% interval, abstention, approximate
  concordance).
- TODO: (bonus) compare to any real practice-test scores.

## 6. Benchmark (50k deck, §10) — `just bench`

- TODO: paste p50/p95/worst table for button ack / next card / dashboard
  load / refresh, with PASS/FAIL vs budgets.

## 7. Leakage — `tools/speedrun/rephrase_eval.py --mock`

- Reference: leakage scan runs and reports (see
  `docs/speedrun/artifacts/leakage_report.txt`).
- TODO: confirm "clean" on the final generation inputs.

## 8. Robustness (§7g)

- TODO: crash test (kill mid-review 20× each platform) → zero corrupted
  collections; offline degradation (AI off, scores still produced).

## 9. Sync (§7b)

- Engine proven: `speedrun_state_syncs_across_devices_offline` Rust test +
  `docs/speedrun/sync.md` conflict rule (revlog = keep both rows; mutable
  objects last-writer-wins by mod time).
- TODO: manual 10+10 offline reconcile + same-card conflict recording.

## What didn't work / limitations (be honest)

- M3 demo scores currently rely on `seed_synthetic_responses` unless a real
  session is studied — do not present synthetic as real.
- Readiness concordance is an **approximation**, not official AAMC data.
- Weakest-link is wired in the math but per-question multi-topic aggregation in
  the production RPC path is being finalized (WS7).
- Memory uses an unweighted (not stability-weighted) mean — a deliberate,
  documented deviation from the original plan wording.
- Real held-out numbers (memory/performance/paraphrase) require a studied deck;
  the harnesses are ready and re-runnable, the organic data is pending.
