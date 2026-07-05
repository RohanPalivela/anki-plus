# Speedrun architecture overview

Speedrun is an Anki fork for **one exam (MCAT)** that turns practice questions
into the driver of study and produces three honest scores (Memory, Performance,
Readiness). One engine powers **desktop + Android**.

## Layers

```
┌───────────────────────────────────────────────┐
│                Rust core (rslib)               │
│  scheduler / FSRS  +  src/speedrun/*           │
│  - activation (question-gated unsuspend)       │
│  - coverage sweep                              │
│  - value-ordered queue (topic_weight×weakness) │
│  - Memory / Performance / Readiness models     │
└───────────────▲───────────────▲───────────────┘
                │ protobuf (SpeedrunService)     │
  ┌─────────────┴───────┐        ┌───────────────┴─────────────┐
  │  pylib (_backend)   │        │  rsdroid JNI (GeneratedBackend)│
  │  col.speedrun.*     │        │  backend.*Speedrun RPCs        │
  └──────────▲──────────┘        └──────────────▲────────────────┘
             │                                  │
  ┌──────────┴──────────┐          ┌────────────┴───────────────┐
  │  Qt desktop (aqt)   │          │  AnkiDroid (Kotlin)         │
  │  qt/aqt/speedrun/*  │          │  com.ichi2.anki.speedrun.*  │
  │  + Svelte web pages │◄────────►│  + shared Svelte in WebView │
  └─────────────────────┘  shared  └─────────────────────────────┘
             ts/routes/speedrun-home, speedrun-dashboard
```

## Data model — everything is a native Anki object (so sync/undo are free)

- **Questions** → notes of type `SpeedrunQuestion` (fields: stem, options,
  correct, explanation, source, `difficulty_b`, `discrimination_a`) in the
  `Speedrun::Questions` deck.
- **Topic / pool labels** → tags: `topic::<name>`, `pool::served` /
  `pool::heldout`.
- **Card ↔ question linkage** → shared `topic::` tags and/or explicit
  `gates::<note_id>`.
- **Answers** → normal `revlog` reviews (Again = incorrect, Good = correct).
- **Miss reason** → tag (`miss::knowledge-gap|missing-context|misunderstanding|
  careless`); only the first two activate cards.
- **Flashcards** → normal cards, **suspended by default**; activated only by a
  qualifying miss or the coverage sweep.

No side tables — Anki's object-based sync carries all Speedrun state to Android
for free (see `docs/speedrun/sync.md`).

## Three-score dataflow

1. Practice questions → `revlog` + `miss::` tags.
2. Qualifying misses → `ActivateCardsForMiss` → unsuspend linked cards.
3. Activated cards flow through FSRS review.
4. **Memory** reads FSRS state; **Performance** fits θ from responses + masteries;
   **Readiness** Monte-Carlos the blueprint from the θ posterior → scaled score.
5. Dashboard (shared Svelte) renders all three with ranges + abstention on both
   platforms.

## Where do I change X?

- Activation/queue/scores → `rslib/src/speedrun/` (+ regenerate proto).
- Study loop / dashboard → `qt/aqt/speedrun/`, `ts/routes/speedrun-*`,
  AnkiDroid `com.ichi2.anki.speedrun.*`.
- AI rephrase/eval → `pylib/anki/speedrun_rephrase.py`, `tools/speedrun/`.
- Evidence harnesses → `tools/speedrun/validation.py`, `tools/speedrun/ablation.py`,
  `tools/speedrun/paraphrase_test.py`, `tools/bench/`.
