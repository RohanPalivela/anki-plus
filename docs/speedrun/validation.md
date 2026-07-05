# Speedrun held-out validation (M4 §4a / brief §9)

The grade is capped at 60% without held-out testing. This harness produces the
re-runnable evidence: memory calibration, performance held-out accuracy/AUC, and
a leakage report.

## Run it

```bash
just speedrun-validate -- --demo                     # synthetic; runs with no build
just speedrun-validate -- --collection ~/col.anki2   # real review history
```

Artifacts are written to `docs/speedrun/artifacts/`:
`validation_report.json`, `memory_reliability.csv`, `memory_reliability.png`
(if matplotlib is present), and `leakage_report.txt`.

## What it reports

**Memory (calibration on a held-out split).** Predictions are split
train/test (`--split random|time`); the test split's **Brier score, log loss,
ECE** and a **reliability table** are reported. **Platt** and **isotonic**
recalibrators are fit on train and applied to test, and the harness reports
before/after and picks whichever most reduces Brier. When it says 80%, the
student should recall ~80% of the time — the reliability diagram shows how close
we are.

**Performance (held-out questions).** On `pool::heldout` questions the harness
reports **accuracy and ROC-AUC** vs the 0.5 chance baseline, using the mirrored
2PL prediction (`predict_performance`) with a MAP ability estimate fit from
served responses.

**Leakage.** Invokes `tools/speedrun/rephrase_eval.py --mock` and saves its
output; leaked held-out data zeroes the affected score, so this must read clean.

## Design

- All metrics live in `pylib/anki/speedrun_validation.py` — **pure stdlib**, no
  numpy/scipy/sklearn, so `--demo` and the unit tests
  (`pylib/tests/test_speedrun_validation.py`) run without a compiled backend.
- `--demo` injects a deliberate over-confidence miscalibration so recalibration
  visibly helps — it is **not** a pre-cooked perfect result. Demo numbers are
  clearly labelled SYNTHETIC.
- Real-collection extraction (per-card retrievability vs last-review outcome;
  per-question 2PL on held-out) is documented inline and refined at integration
  against a studied deck.

## Results

Paste the latest `--collection` run here (deck, date, commit):

```
TODO: paste real held-out numbers once run on a studied collection
```

Reference demo run (synthetic, sanity-check that the pipeline works):

```
MEMORY: Brier≈0.179 logloss≈0.531 ECE≈0.087; best recalibrator platt (Brier→0.170)
PERFORMANCE: n=400 accuracy≈0.77 AUC≈0.82 (beats chance)
```
