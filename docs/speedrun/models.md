# Speedrun model descriptions (Memory / Performance / Readiness)

One page each. Every model reports an honest range and follows a **give-up
(abstention) rule** — it emits no number when the evidence is too thin. All three
are Rust RPCs (`rslib/src/speedrun/`), shared by desktop + Android.

---

## Memory — "P(recall) now"

**Question it answers:** how likely is the student to recall each activated card
right now, aggregated to per-topic mastery.

**Inputs:** FSRS state (stability) of **activated (unsuspended)** flashcards;
days since last review. Question cards are excluded.

**Method:** per-card retrievability from the FSRS power forgetting curve
`R(t) = (1 + FACTOR·t/S)^decay` (so `R(S)=0.9`), aggregated to a per-topic mean
over the cards carrying each `topic::` tag. Not-yet-activated topics are treated
as unknown/low.

> **Honesty note (contract deviation):** the code aggregates an **unweighted**
> mean retrievability, not the "stability-weighted" mean named in the original
> plan/proto — deliberately, so low-stability Again/Hard cards stay visible
> (rationale in `mastery.rs`). Docs describe what the code does.

**Output:** overall mastery point estimate + an 80% range (normal approx on the
recalled proportion).

**Give-up rule:** abstain when `graded_count < 5` **or** `coverage < 0.10`
(`MEMORY_MIN_GRADED`, `MEMORY_MIN_COVERAGE` in `service.rs`).

**Validation:** reliability diagram + Brier/log loss on held-out reviews; see
`docs/speedrun/validation.md`.

---

## Performance — "P(correct on a NEW question)"

**Question it answers:** how likely is the student to get a fresh exam-style
question right — measuring transfer, not card memory.

**Inputs:** per-question difficulty `b` and discrimination `a` (note fields),
per-topic mastery (mean **and weakest-link min**), response time (engagement),
and a fitted ability `θ`.

**Method:** 2PL IRT with a 4-option guessing floor
`P = c + (1−c)·σ(a(θ−b))`, `c = 0.25` (`GUESSING_FLOOR_C`). The ability offset
blends topic masteries `0.5·min + 0.5·mean` (`WEAKEST_LINK_WEIGHT = 0.5`) scaled
into logits (`MASTERY_LOGIT_SCALE = 2.0`), which is what stops it collapsing into
the Memory model (validated by the paraphrase test). `θ` is a MAP grid estimate
over `[−4, 4]` with an `N(0,1)` prior; its standard error feeds readiness.
Held-out (`pool::heldout`) questions are never used to fit.

**Output:** `predict_performance(question, mastery) → P(correct)`;
`estimate_theta(responses) → (θ, se)`.

**Give-up rule:** abstain when `graded < 5` **or** `coverage < 0.10`
(`PERF_MIN_GRADED`, `PERF_MIN_COVERAGE`).

**Validation:** held-out accuracy/AUC + the 30×2 paraphrase gap; see
`validation.md` and `paraphrase.md`.

---

## Readiness — "projected MCAT score + range"

**Question it answers:** the projected scaled MCAT score with an honest interval.

**Inputs:** the `θ` posterior, the MCAT blueprint topic weights, per-topic
mastery, and coverage.

**Method:** Monte Carlo (`READINESS_SIMS = 2000`, fixed seed). Each sim samples
`θ ~ N(θ, se)`, allocates `EXAM_ITEMS = 230` questions by blueprint weight,
draws Bernoulli outcomes from `P(correct)`, and sums to a raw score. Uncovered
topics use a low prior (`UNCOVERED_PRIOR_MASTERY = 0.2`, `SD = 0.15`) which
widens the interval. Raw is mapped to the **472–528** MCAT scale via a monotonic
`CONCORDANCE` curve (documented **approximation**, not official AAMC data).

**Output:** median + **80% interval** (10th–90th pct), coverage %, confidence
(`coverage × width factor`, `CONFIDENCE_WIDTH_REF = 40`), and top reasons.
Progress is surfaced as rising practice-question performance over time.

**Give-up rule:** abstain when `graded < 10` **or** `coverage < 0.5` **or**
interval `width > 30` (config-overridable
`DEFAULT_READINESS_MIN_GRADED / MIN_COVERAGE / MAX_WIDTH` in `readiness.rs`).
No score is emitted without evidence — faking one is an automatic fail.

**Validation:** method + range documented; bonus comparison to real practice
tests if available.
