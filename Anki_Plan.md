# Anki Fork — Implementation Plan (for coding agents)

Milestone-structured plan to modify the Anki codebase into the "Speedrun" study app. This describes **what to build, the contracts, and acceptance criteria** — not where things live. Agents should map the codebase themselves to locate the right modules.

---

## Agent operating rules (read before coding)

- **Ask before deciding anything ambiguous.** If a requirement is underspecified, has more than one reasonable interpretation, or needs a non-obvious tradeoff, **STOP and ask the user — do not guess and do not silently pick a default.** This applies to schema choices not covered here, model hyperparameters that affect grading, scope changes, dependency additions, and anything destructive.
- **When you ask:** batch related questions together, state your **recommended** option, and wait for the answer before proceeding on that thread (keep working on unblocked threads).
- **Locked decisions — do NOT re-litigate:**
  - **Exam: MCAT only** — scale 472–528, four sections each 118–132. Build the whole app for MCAT.
  - **Mobile platform: Android only**, via AnkiDroid sharing the Rust engine. **iOS is out of scope.**
  - **Custom data is stored as native Anki objects** (notes/tags/cards/`revlog`) — never a side table.
- See **"Open decisions to confirm with the user"** at the bottom for the known ambiguities to resolve.

---

## Project context (read first)

- **Goal:** Fork Anki into a **desktop + Android** study app for the **MCAT** that produces three honest predictions — **Memory, Performance, Readiness** — each with an uncertainty range and an abstention rule.
- **Hard constraints:**
  - A **real change inside Anki's Rust core**, not just Python/Qt UI. This is the central gate.
  - **Desktop + Android share one engine** and sync. Reuse Anki's existing sync; do not rewrite it.
  - **Never emit a readiness number without evidence** (point estimate + range + coverage + confidence + abstention). Faking it is an automatic fail.
  - License: **AGPL-3.0-or-later**, credit Anki (some parts BSD-3).
- **Architecture (verify during mapping):** Rust backend (`rslib`) exposed to Python (`pylib`) via protobuf-defined RPC services; desktop UI in Qt (`aqt`); **Android via AnkiDroid** sharing the Rust backend. The v3 scheduler and FSRS live in the Rust backend.

### Product thesis the features must implement (SPOV3)

The whole app is the engineered version of one workflow: **practice-questions drive the flashcards, not the other way around.** Every feature below exists to serve this loop:

1. The student has already learned the material and imported concept-separated decks (external; not our job).
2. **All cards start suspended (disabled).** The default state is _off_.
3. The student does **practice questions first**. When they miss one, they **classify _why_** (like Anki's grade buttons):
   - `knowledge-gap` (didn't know the fact) → **activate** the linked cards
   - `missing-context` (knew the fact, didn't connect/apply it) → **activate** the linked cards
   - `misunderstanding` (reasoning error) → **do NOT activate** — this is not a memory problem
   - `careless` (slip) → **do NOT activate**
4. Activated cards then flow through normal FSRS review.
5. **Periodic full sweeps** re-surface a sample across _all_ topics to prevent info-tunneling / coverage holes.
6. Continue until topic coverage is complete or the readiness score hits target. **Progress is shown as rising practice-question performance** (the motivation loop that fights burnout/quitting).

Implication for the build: the marquee engine change is **question-gated card activation** (§M1 1a), questions are the primary study surface (§M2), and readiness is anchored to practice performance, not card counts.

### Cross-cutting rules for every change

1. **Keep the fork mergeable.** Prefer additive changes (new RPCs, new modules) over rewriting upstream functions. Track every upstream file touched.
2. **Store all custom data as native Anki objects** (note types, tags, cards, `revlog`) — **never** in a side SQLite table. Anki's sync is object-based; native objects sync for free, side tables do not.
3. **Undo-safety & no corruption:** any engine change must preserve undo and pass a collection-integrity check.
4. **Performance budgets** (report p50/p95/worst on a 50k-card deck): button ack p95 < 50ms; next card p95 < 100ms; dashboard load p95 < 1s, refresh < 500ms; no UI freeze > 100ms.

---

## Key design decision: questions and responses ARE cards and reviews

Do **not** invent a parallel data store for practice questions. Model them natively so sync, undo, and stats come for free:

- **Question item** → a Note of a dedicated note type `SpeedrunQuestion` with fields: `stem`, `options`, `correct`, `explanation`, `source` (named origin), `difficulty_b`, `discrimination_a`. Place its Card in a dedicated deck (e.g. `Speedrun::Questions`).
- **Topic + pool labels** → **tags**: `topic::<name>` (one per blueprint topic it tests) and `pool::served` or `pool::heldout`. Tags sync.
- **Question → flashcard linkage (required for SPOV3 gating)** → store which flashcards each question can activate. Use shared `topic::` tags as the default link, and/or an explicit `gates::<note_id>` reference on the question for precise linkage. This is what lets a missed question know which cards to unsuspend.
- **A student answering a question** → a normal **review** (`Again` = incorrect, `Good` = correct). The `revlog` captures correctness, response time, and timestamp, and syncs automatically.
- **Miss-reason** → recorded as a tag on the review/answer event (e.g. `miss::knowledge-gap`, `miss::missing-context`, `miss::misunderstanding`, `miss::careless`). Only `knowledge-gap` and `missing-context` trigger card activation.
- **Flashcards** stay normal cards but **start suspended by default**; **Memory** reads FSRS state from the cards that have been activated.

This makes the served-vs-held-out split a tag filter, coverage a tag query, the leakage check a tag/text scan, and card-activation a deterministic function of (missed question → linked cards).

---

## M0 — Foundation (do before anything else)

**Goal:** prove the full toolchain works end-to-end before writing features.

- Fork Anki (public, AGPL, credit Anki). State **MCAT** at top of README.
- Compile the Rust backend + desktop from source; app launches.
- Land a **trivial** Rust change that surfaces in the desktop UI (proves the Rust→protobuf→Python loop).
- Get the **same engine running on Android** via an AnkiDroid build sharing the Rust backend.

**Acceptance:** clean build from source on a fresh machine; trivial Rust value visible in UI; Android build launches and opens a deck.

---

## M1 — Wednesday: engine change + Memory model + installs (NO AI)

### 1a. The Rust engine change (the gatekeeper) — Question-Gated Card Activation

This is the SPOV3 mechanism in the engine: cards stay suspended until a _missed_ practice question activates the linked cards, and the review queue is built only from activated cards. Putting this in Rust (not Python) keeps activation atomic, undo-safe, fast on 50k cards, and shared with Android.

- **Protobuf:** add new RPC(s) + messages:
  - `ActivateCardsForMiss { question_id, miss_reason } -> { activated_card_ids }` — unsuspends the question's linked cards **only** when `miss_reason ∈ {knowledge-gap, missing-context}`; a no-op otherwise.
  - `RunCoverageSweep { sample_size } -> { reactivated_card_ids }` — re-activates a spread across all topics (the periodic full sweep).
  - Regenerate bindings.
- **Rust impl:**
  - Resolve a question's linked cards (via shared `topic::` tags and/or `gates::` references) and **unsuspend them atomically with full undo support**; never corrupt the collection.
  - Build the review queue **only from activated (unsuspended) cards**, ordered by **value = topic_weight × weakness** (`weakness = 1 − topic_mastery`; `topic_weight` from the MCAT blueprint) so the highest-impact activated cards surface first.
  - **Must not** alter FSRS intervals/due dates — this governs _activation + ordering_, not spacing.
- **Python binding:** expose the RPCs; wire the miss-reason flow (answer a question → classify miss → call `ActivateCardsForMiss`) and a scheduled/triggerable coverage sweep.
- **Tests:** **≥3 Rust unit tests** (activation only for the two qualifying miss-reasons; no-op for `misunderstanding`/`careless`; queue excludes suspended cards and orders activated ones correctly; sweep spreads across topics) **+ 1 test that calls a gating RPC from Python**.
- **Required artifacts:** the diff, proof undo works + integrity check passes, a 1-page "why this belongs in Rust" (atomic state transitions; perf on 50k cards; shared by Android), and a list of upstream files touched + merge-difficulty note.

> If a smaller first slice is needed, the **points-at-stake ordering** (value = topic_weight × weakness over activated cards) is the part to land first; activation gating + sweep layer on top. Confirm scope with the user before narrowing.

### 1b. Memory model running

- Wrap FSRS: expose per-card retrievability `R(t) = (1 + t/(9S))^(−1)` (t = days since last review, S = stability). This is the memory probability.
- Aggregate to **per-topic mastery** (stability-weighted mean over _activated_ cards carrying that `topic::` tag); treat not-yet-activated topics as unknown/low (feeds coverage + abstention).
- Surface an honest memory display with a **range** and the **give-up rule** (abstain below a stated data threshold).

### 1c. Installs + Android

- **Desktop installer** runs on a clean machine.
- **Android app** builds and runs a real review session on the **shared engine** (two-way sync not required yet).

**Acceptance:** Rust change passes all tests + undo/integrity; review loop runs on the MCAT deck; memory score shows with a range; desktop installer runs clean; Android app reviews the same deck.

---

## M2 — Friday: AI feature + sync + question/response pipeline

### 2a. AI card feature (grounded)

- Build AI that **generates/rephrases cards from a named source** (e.g., a textbook chapter). Each generated card stores its `source` reference; outputs with no traceable source are rejected.
- **Eval harness (runs before any card reaches a user):** build a **gold set of 50 Q&A pairs**; generate 50 cards from one real source; classify each as _correct-and-useful / wrong / correct-but-bad-teaching_; report **accuracy + wrong-answer rate**; set a **pass cutoff in advance** and **block** failing cards.
- **Baseline comparison:** show the AI **beats keyword or vector search** on a head-to-head set. _(This baseline is for card generation/retrieval, not the score models.)_
- **Leakage check:** a script that scans the generation/training inputs for any `pool::heldout` item or near-duplicate; must report clean.
- **AI-off path:** the app must still produce all three scores with AI disabled.

### 2b. Question-first study loop (the SPOV3 core surface)

This is the primary study experience, not a side checkpoint. Cards are suspended by default; questions drive everything.

- **Question session:** serve `pool::served` questions (interleaved across topics, not blocked — this is the desirable-difficulty win). Grading writes a normal review (correct/incorrect + time) to `revlog`.
- **Miss-reason classification:** on an incorrect answer, prompt the student to pick _why_ (`knowledge-gap` / `missing-context` / `misunderstanding` / `careless`) and tag the event. This is the SPOV3 "choose why you got it wrong" step.
- **Gated activation:** call `ActivateCardsForMiss` so only `knowledge-gap`/`missing-context` misses unsuspend the linked cards. Misunderstanding/careless misses surface the explanation but add no cards.
- **Periodic full sweep:** expose a scheduled/triggerable `RunCoverageSweep` so the student periodically re-activates a spread across all topics (anti-tunneling).
- **Served vs. held-out:** enforce the `pool::` split at serve time; **never serve `pool::heldout`** (kept for M4 evaluation).

### 2c. Sync (reuse, don't rewrite)

- Get the **forked desktop + Android build** syncing through Anki's existing sync (AnkiWeb or self-hosted sync server).
- **Two-way + offline:** review offline on each device, reconnect, all reviews merge with **none lost or double-counted**.
- **Document the conflict rule** (incremental sync merges `revlog` from both devices; on a schema conflict Anki forces a direction — state which and why).
- Android shows the **three scores with ranges + give-up rule**.

**Acceptance:** eval numbers + baseline table produced; leakage clean; AI-off still scores; question-first loop records miss-reasons and gated activation unsuspends the right cards (and only for qualifying misses); periodic sweep re-activates across topics; reviews write to `revlog`; Android↔desktop offline reviews reconcile correctly on reconnect.

---

## M3 — Saturday: Performance + Readiness models

> Scaffold both earlier against **mock inputs**; fit with real `revlog` responses here. The brief never names Saturday, but the dependency order requires it.

### 3a. Performance model — "P(correct on a NEW question)"

- **Inputs per response:** mean topic mastery over the question's `topic::` tags, **min topic mastery (weakest link)**, question difficulty `b`, discrimination `a`, response time.
- **Model:** 2PL IRT with a guessing floor for 4-option MCQ, `P = c + (1−c)/(1+e^(−a(θ−b)))`, c ≈ 0.25 — or an equivalent logistic/GBM classifier over the features above. Estimate student ability `θ` (keep its standard error for M4 uncertainty).
- **Critical:** it must **not** collapse to the memory model. The weakest-link + difficulty features create the memory→performance gap (validated by the paraphrase test in M4).
- **Interface:** `predict_performance(question, mastery_vector) -> P(correct)`; `estimate_theta(responses) -> (theta, se)`.

### 3b. Readiness model — "projected exam score + range"

- **Simulate the exam via the MCAT blueprint** (per-section topic weights). Monte Carlo: repeatedly (1) sample `θ` from its posterior, (2) sample questions per topic weights, (3) sample Bernoulli outcomes from `P(correct)` → distribution of raw scores.
- For **uncovered topics**, use a low/prior mastery and widen uncertainty (this ties confidence to coverage).
- **Raw → scaled mapping:** convert to MCAT scale (472–528) via a stated monotonic concordance curve.
- **Output:** median + **80% interval** (e.g., 508, 503–512), coverage %, confidence, last-updated, top reasons.
- **Coverage (SPOV3 sense):** fraction of blueprint topics that have been _exercised by questions_ (answered correctly, or missed → cards activated + studied). Untested topics drag coverage down and widen the interval.
- **Progress/motivation surface:** show rising **practice-question performance over time** as the headline progress metric (the burnout/quitting countermeasure), with the readiness score as the projected outcome.
- **Abstention rule:** emit no score when graded responses < N **or** coverage < X% **or** the interval is wider than a stated threshold.

**Acceptance:** performance model predicts held-out responses above chance with a non-trivial memory→performance gap; readiness produces a calibrated-looking interval and correctly abstains on thin data.

---

## M4 — Sunday: validation, experiment, packaging, hand-in

### 4a. Model validation (held-out, re-runnable)

- **Memory:** reliability diagram + **Brier or log loss** on **held-out reviews** (train/test split of review history). Apply Platt/isotonic recalibration if needed.
- **Performance:** accuracy/AUC on **held-out questions** (`pool::heldout`). Run the **paraphrase test** (30 cards → 2 reworded Qs each; compare card recall vs. reworded accuracy; **report the gap**).
- **Readiness:** method + range documented; (bonus) compare to any real practice-test scores.

### 4b. Study-feature ablation (15% of grade — don't skip)

- Pick one learning-science feature (e.g., **interleaving**). Pre-register the metric and failure condition in one sentence.
- Compare **3 builds at equal study time:** full app / feature-off (ablation) / plain unmodified Anki.
- Report a range and **null results honestly**.

### 4c. Robustness + packaging

- **Crash test:** kill each app mid-review 20× → zero corrupted collections. **Offline:** AI degrades cleanly, apps keep scoring.
- **One-command benchmark** (e.g. `make bench`) on the 50k deck printing p50/p95/worst per action.
- **Sync conflict test:** same card reviewed on both devices offline → documented, correct winner.
- **Package:** desktop installer + signed Android APK. Both run with **AI off**.

### 4d. Hand-in

- Public AGPL fork (exam stated, build instructions for both apps, architecture overview, Rust-change note, files-touched list).
- Demo video (3–5 min): review session, Rust change in action, Android→desktop sync, three scores with ranges, AI features, test results.
- One-page model descriptions (Memory/Performance/Readiness incl. give-up rule).
- Brainlift (confirm format with class outline), honest results report including what failed.

---

## Reference: component contracts (quick)

| Component                   | Input                         | Output                                           | Stored as                                  |
| :-------------------------- | :---------------------------- | :----------------------------------------------- | :----------------------------------------- |
| **Gated activation (Rust)** | `question_id` + `miss_reason` | activated card IDs (qualifying misses only)      | new RPC in Rust backend                    |
| **Coverage sweep (Rust)**   | sample size                   | re-activated card IDs across topics              | new RPC in Rust backend                    |
| Activated-card queue (Rust) | topic weights + weakness map  | ordered queue from _activated_ cards             | queue-build path                           |
| Memory model                | FSRS state of activated cards | `R(t)` per card → topic mastery                  | computed (FSRS)                            |
| Question                    | —                             | stem/options/correct/source/`a`/`b` + card links | `SpeedrunQuestion` note + tags + `gates::` |
| Response + miss-reason      | student answer                | correct? + time + `miss::*`                      | native `revlog` review + tag               |
| Performance model           | mastery + `a,b` + timing      | `P(correct)` per question; `θ,se`                | model artifact + eval report               |
| Readiness model             | `θ` posterior + blueprint     | scaled score + interval + coverage + progress    | model artifact + dashboard                 |
| AI generator                | named source text             | cards w/ source ref (post-eval)                  | normal cards + eval report                 |

## Definition of done (per gate)

- **M1:** gated-activation Rust change merged with tests + undo/integrity (cards suspended by default; only qualifying misses activate; queue from activated cards); memory score with range; desktop installer clean; Android reviews shared deck.
- **M2:** AI eval + baseline + leakage clean + AI-off scoring; question-first loop with miss-reason classification → gated activation works; periodic sweep works; reviews sync; two-way offline sync reconciles.
- **M3:** performance model beats chance with a real memory→performance gap; readiness emits interval + abstains correctly.
- **M4:** all three validated on held-out data; ablation reported (incl. nulls); both apps packaged, crash-safe, benchmarked; deliverables submitted.

---

## Open decisions to confirm with the user

Resolve these by **asking the user** (with a recommended option) — do not assume. Locked items (MCAT, Android-only, native-object storage) are settled and not listed here.

1. **Sync hosting:** AnkiWeb account vs. self-hosted sync server? _(Recommend self-hosted for control + offline demos.)_
2. **Rust engine change scope:** confirm the full **question-gated card activation** engine (activation + activated-card queue ordering + coverage sweep) vs. landing only the points-at-stake ordering first.
   - 2a. **Card↔question linkage:** shared `topic::` tags only, or explicit `gates::` references per question (more precise, more setup)?
   - 2b. **Which miss-reasons activate cards:** confirm `knowledge-gap` + `missing-context` activate, and `misunderstanding` + `careless` do not.
   - 2c. **Sweep cadence:** how often / how large is the periodic full sweep?
3. **MCAT blueprint source:** where do topic list + section/topic weights come from? _(Official AAMC outline vs. a provided file.)_ This feeds the queue, coverage, and readiness simulation.
4. **Card/content source:** which MCAT deck(s) and which source text the AI generates from (licensing matters).
5. **AI provider:** which model/API for generation, and is offline/local generation required? _(Affects the AI-off path and cost.)_
6. **Abstention thresholds:** the exact N (graded responses), coverage %, and max interval width that trigger "no score."
7. **Performance model choice:** 2PL IRT vs. logistic/GBM, and the guessing floor value.
8. **Raw→scaled mapping:** which MCAT concordance/conversion data to use, and acceptable approximation.
9. **Ablation feature:** which learning-science feature to test (e.g., interleaving) and the pre-registered metric.
10. **Test "learners":** real testers vs. a synthetic/seed response set, and the acceptable minimum n.

> If a new ambiguity appears that isn't on this list, add it and ask before proceeding.
