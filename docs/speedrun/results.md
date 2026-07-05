# Speedrun results report (honest — includes what didn't work)

This is the spine the final numbers get pasted into. Reference **demo/synthetic**
numbers below prove each harness runs; **TODO** markers are where real
studied-deck / device numbers must replace them before hand-in. Demo numbers are
clearly labelled SYNTHETIC and must never be presented as real learner data.

> **Real data source.** Runs below marked REAL use the author's own studied
> collection (profile `hello`): 4,996 cards, spanning 2026-07-02 → 2026-07-05,
> plus a **60-question held-out eval pass** and a **paraphrase pass** (30 lessons
> studied + 60 reworded questions answered) done in-app on 2026-07-05 via
> Tools ▸ Speedrun. All harnesses were run against a read-only copy so the live
> collection was never mutated. The dataset is real but small, so some metrics
> carry wide uncertainty — flagged inline.

## 1. Memory calibration (held-out) — `just speedrun-validate`

- **REAL (2026-07-05, profile `hello`, after held-out pass):** held-out test
  n=38 — **Brier 0.198**, **log loss 0.528**, **ECE 0.310**; best recalibrator
  = **isotonic** (Brier → 0.001; Platt → 0.021). Caveat: still a small split, so
  the recalibrated Brier in particular is optimistic on n=38; treat point
  estimates as indicative. Reliability CSV: `artifacts/memory_reliability.csv`.
- Reference (SYNTHETIC): Brier ≈ 0.179, log loss ≈ 0.531, ECE ≈ 0.087; best
  recalibrator = Platt (Brier → 0.170).

## 2. Performance (held-out questions) — `just speedrun-validate`

- **REAL (2026-07-05):** **n=60, accuracy 0.517, ROC-AUC 0.671** (chance 0.5,
  **beats_chance=True**) on the `pool::heldout` questions answered in-app. The
  Performance 2PL predictions — fit only on served answers — rank the held-out
  outcomes better than chance, i.e. the model generalizes to unseen questions.
  AUC is modest given n=60 and a first study pass; more held-out answers tighten
  it. Full run: `artifacts/validation_report.json`.
- Reference (SYNTHETIC): n=400, accuracy ≈ 0.77, AUC ≈ 0.82 (> 0.5 chance).

## 3. Paraphrase gap — `just speedrun-paraphrase`

- **REAL (2026-07-05):** n=19 cards with both signals — mean card **recall
  0.953** vs mean **reworded accuracy 0.763**, **gap +0.190**
  [per-card −0.29..+0.97]; verdict **MEANINGFUL GAP — performance ≠ memory
  (bridge exists)**. Per-topic gaps vary honestly (orgo +0.79, gen-chem/sociology
  +0.24, biochem −0.13), i.e. the two models diverge rather than the performance
  score echoing recall. Caveat: recall was measured right after studying the
  lessons, so recall runs high; the *direction and size* of the gap are the
  point. Full run: `artifacts/paraphrase_report.json`.
- Reference (SYNTHETIC, penalty 0.18): recall 0.769 vs reworded 0.589,
  **gap +0.180** (performance ≠ memory). Null control (penalty 0): gap ≈ 0.

## 4. Study-feature ablation (interleaving, 15%) — `just speedrun-ablation`

- **RUN (2026-07-05, SIMULATION, transfer 0.25, 100 events, 50 seeds):**
  full **0.964** [0.927, 0.998] > ablation **0.855** [0.802, 0.905] > plain
  **0.760** [0.708, 0.833]; Δ(full−ablation) = **+0.109**, Δ(ablation−plain) =
  **+0.095**; hypothesis **SUPPORTED**. Sweep shows an honest **null at
  transfer=0**. (Simulation harness — deliberately not learner data.)

## 5. Readiness — `docs/speedrun/models.md`

- Method + range documented (Monte Carlo, 80% interval, abstention, approximate
  concordance).
- TODO: (bonus) compare to any real practice-test scores.

## 6. Benchmark (50k deck, §10) — `just bench`

- **REAL (2026-07-05, generated 50,000-card deck, 1000 iterations):**

  | Action | n | p50 ms | p95 ms | worst ms | §10 budget | result |
  |---|---|---|---|---|---|---|
  | Button press ack (`answerCard`) | 1000 | 0.3 | 0.5 | 8.2 | p95<50 | PASS |
  | Next card appears (queue) | 1000 | 0.2 | 0.2 | 27.3 | p95<100 | PASS |
  | Dashboard first load (cold) | 1 | 420.8 | — | — | p95<1000 | PASS |
  | Dashboard refresh (warm) | 200 | 368.9 | 482.4 | 875.1 | p95<500 | PASS |

  **OVERALL: PASS.** The 50k deck is synthetic by design — this is an
  engine-scale latency test, not a learner-data claim. Dashboard RPCs abstain on
  the synthetic deck (no real memory state); the numbers are pure latency.

## 7. Leakage — `tools/speedrun/rephrase_eval.py --mock`

- **REAL (2026-07-05):** leakage scan ran as part of `just speedrun-validate
  --collection` — report at `docs/speedrun/artifacts/leakage_report.txt`.

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
- Memory, Performance held-out, and the paraphrase gap are now all REAL (studied
  in-app 2026-07-05); the ablation is a labelled simulation and robustness/sync
  device passes remain the honest TODOs.
