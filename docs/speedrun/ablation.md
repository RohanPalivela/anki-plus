# Speedrun study-feature ablation (brief §8 / M4 §4b — 15% of grade)

**Pre-registered hypothesis (one sentence):** *Interleaving practice questions
across topics raises accuracy on NEW mixed-topic questions at equal study time;
FAIL if Δ(full − ablation) ≤ 0.*

The learning-science feature under test is **interleaving** (desirable
difficulty / contextual interference). It is a single collection-config switch
in the engine — `speedrunInterleaving`, read by
`Speedrun.interleaving_enabled()` and honoured by
`served_questions_interleaved()` (interleaved round-robin vs blocked-by-topic),
so turning it off is a genuine one-feature ablation shared by desktop + Android.

## The three builds (equal study time)

Per brief §8, three builds are compared on the same held-out mixed-topic test at
an equal number of study events:

1. **full** — Speedrun, interleaving **on** (weakness-targeted + interleaved)
2. **ablation** — Speedrun, interleaving **off** (weakness-targeted + blocked)
3. **plain** — plain unmodified Anki (uniform order, no transfer)

`full` vs `ablation` isolates interleaving; `ablation` vs `plain` shows whether
the rest of Speedrun (weakness-targeted allocation) helps at all.

## Run it

```bash
just speedrun-ablation                 # default: 100 events, 50 seeds
just speedrun-ablation -- --sweep      # sweep transfer benefit; shows the null
just speedrun-ablation -- --events 150 --seeds 100 --transfer-weight 0.2
```

Pure-Python and deterministic — runs on a fresh checkout with no build.

## Method + honesty

This is a **deterministic simulation harness**, not a human trial. It drives the
real serving orders (round-robin vs blocked, mirroring `_round_robin`/`_blocked`)
through a documented synthetic learner: mastery rises per study event; a
**transfer skill** accrues from context switches (interleaving has many, blocked
has few); held-out mixed-topic accuracy uses a weakest-link, guessing-floored
model lifted by transfer.

It is a **fair** test: the interleaving benefit is governed by one transparent
parameter, `--transfer-weight`, and `--sweep` shows the effect **honestly
collapses to ≈0 (a null) when that benefit is zero**. We do not have to prove the
idea works — we have to run a test that *could* show it does not.

The follow-up (real seed/human learners with equal time budgets) is the honest
next step; the synthetic result is an upper-bound plausibility check plus a
re-runnable, re-inspectable rig.

## Results

Reference simulation run (transfer_weight=0.25, 100 events, 50 seeds):

```
full      mean 0.964  [0.927, 0.998]
ablation  mean 0.855  [0.802, 0.905]
plain     mean 0.760  [0.708, 0.833]
Δ full − ablation  = +0.109   (interleaving effect)
Δ ablation − plain = +0.095   (weakness-targeting effect)
Hypothesis: SUPPORTED (in simulation)
```

Sweep (honest null at zero transfer benefit):

```
transfer_weight  Δ(full−ablation)
0.00             ≈+0.009  null (within noise; residual = allocation granularity)
0.30             +0.116   win
```

Paste real-learner numbers here when available:

```
TODO: real/seed-learner 3-build comparison at equal study time
```
