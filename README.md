# Anki-Plus — Speedrun (MCAT)

> **Exam: MCAT** — scale 472–528, four sections each 118–132. **Anki-Plus is an
> AGPL-3.0 fork of [Anki](https://apps.ankiweb.net)**, rebuilt into a
> question-first study app for the MCAT.

Anki-Plus keeps Anki's spaced-repetition core and adds, on top of it:

- **Question-gated card activation** — a change inside Anki's **Rust core**
  (`rslib/src/speedrun/`): flashcards start suspended and are unsuspended only
  when a _missed_ practice question qualifies. Atomic, undo-safe, and shared
  with Android.
- **Points-at-stake value ordering** — a second Rust-core change that reorders
  due cards by `topic weight × student weakness`, so the highest-value cards
  come first (FSRS intervals untouched, undo intact).
- **Three honest predictions** — Memory, Performance, and Readiness, each with
  an uncertainty range and an abstention rule (never a score without evidence).
- **Grounded AI rephrasal (opt-in)** — reworded, source-checked variants of
  practice questions and memory cards. Off by default; every score is produced
  with AI disabled. ([details](#ai-features-grounded-rephrasal))
- **A legally reusable MCAT question bank** ([see credits](#credits--attribution))
  imported once and synced to every device as native Anki notes.
- **Desktop + Android** sharing one Rust engine through Anki's existing sync.

It is **not affiliated with, endorsed by, or sponsored by the AAMC**. MCAT is a
registered trademark of the AAMC.

### Build & run

- **Desktop:** `just run` (build + launch). See [docs/development.md](./docs/development.md)
  and [docs/build.md](./docs/build.md).
- **Android:** shares the Rust core via AnkiDroid. First machine:
  `just android-setup` (installs deps, builds, launches an emulator).
  Afterwards: `just android-run`. Full guide:
  [docs/speedrun/android.md](./docs/speedrun/android.md).
- **Architecture & plan:** [CODEBASE_PRIMER.md](./CODEBASE_PRIMER.md) for the
  map; [Anki_Plan.md](./Anki_Plan.md) for the fork's milestones and contracts.

## Features

Everything below is built on native Anki objects (notes, cards, tags, `revlog`,
config), so it all syncs for free between desktop and Android.

**Study engine (Rust core, `rslib/src/speedrun/`)**

- **Question-first gating loop** — practise questions gate memory cards; a card
  is unsuspended only when a related question is missed for a _memory_ reason
  (knowledge gap / missing context). Miss reasons are stored as `miss::` tags.
- **Points-at-stake value ordering** — new queue order sorting due cards by
  `topic weight × weakness`, exposed via protobuf and callable from Python.
- **Coverage sweep** — re-activates a spread of cards across all blueprint
  topics so no topic is silently starved.

**Three honest scores (with ranges + a give-up rule)**

- **Memory** — per-topic mastery from FSRS retrievability of activated cards.
- **Performance** — 2PL-IRT `P(correct)` on a new question, fit from your
  `revlog` responses.
- **Readiness** — Monte-Carlo projected MCAT scaled score (472–528) with an 80%
  interval. Uses a **fixed seed**, so it is reproducible and identical across
  devices given the same data.
- Each score shows an uncertainty range and **abstains** when the data is too
  thin, and is labelled `synthetic` if any dev/test seed data is included.

**Learner experience**

- **Curriculum home** (topic → concept) and a **guided session**
  (Practice → Flashcards → Recap) that targets your weak spots.
- **Memory dashboard** (shared Svelte page) rendered identically on desktop and
  Android.

**Grounded AI rephrasal (opt-in)** — see the next section.

**Platform**

- **Desktop** (PyQt) and **Android** (AnkiDroid) run the same Rust engine and
  reconcile through Anki's existing object sync — offline reviews on both
  devices merge with none lost or double-counted
  ([conflict rule](./docs/speedrun/sync.md)).

## AI features (grounded rephrasal)

_This section is the project's AI note: what was built, why, how it is checked,
and what was deliberately skipped._

### What it is

An **opt-in, grounded rephrasal** feature that turns existing, license-clean
content into extra practice with fresh phrasing:

- **Question variants** — reword a practice question's stem while keeping the
  answer choices and correct answer anchored.
- **Flashcard variants** — reword a first-principles memory card's _front_ while
  preserving the _fact_ in its _back_.

Variants test the **same underlying fact with different phrasing**
(desirable-difficulty practice), expanding the pool without new licensing.
Backed by OpenAI, with a deterministic `MockProvider` fallback so the pipeline
runs (and tests) with no key. Code: `pylib/anki/speedrun_rephrase.py`.

### Why it belongs here (and why grounded)

Students plateau by re-seeing identical wording. Rewordings force retrieval of
the principle, not the surface form — and they are cheap to make from content we
already ship. The generator is **grounded/extractive** (must preserve the
answer/fact) rather than free-form, so it cannot invent unverified content or
drift into copyrighted material.

### Every output traces to a named source

Each generated note is written as a **native, suspended** Anki note tagged:

- `bank::ai-generated` — marks it as machine-generated,
- `variant-of::<source note id>` — the exact source it was derived from,
- `variantuid::…` — a stable id (idempotent; re-running never duplicates),
- plus the source's `topic::` / `concept::` tags (so it activates like its
  source).

AI variants are **review-only**: they are excluded from the Memory mastery pass
in Rust, so a reworded copy never double-counts a fact's retention.

### Checked before students see anything

`tools/speedrun/rephrase_eval.py` runs an **offline evaluation** on a vendored
gold set before anything is shown:

- A deterministic `HeuristicGrader` labels each variant
  **correct-and-useful / correct-but-bad-teaching / wrong** (answer preserved,
  options/fact grounded, actually reworded, teaches, no leakage).
- Reports **accuracy** and **wrong-rate** against **fixed, pre-set cutoffs**
  (accuracy ≥ 80%, wrong ≤ 10%) and prints PASS/FAIL.
- **Leakage guard** — refuses to run on held-out items and flags near-duplicates
  of them.
- **Baseline comparison** — must beat a **TF-IDF retrieval baseline** by ≥ 5%
  on same-concept rate (AI vs. a simpler keyword/vector method).
- A per-variant **min-quality gate** blocks anything weak from ever being
  written, independent of the aggregate report.

```bash
python3 tools/speedrun/rephrase_eval.py         # real provider (needs OPENAI_API_KEY)
python3 tools/speedrun/rephrase_eval.py --mock  # deterministic, no key
```

### The app still scores with AI off

AI is a **synced, off-by-default master switch**
(Tools → _Speedrun (MCAT)_ → _Enable AI rewording_). All three scores are
produced with AI disabled; generation is a clean no-op when the switch is off or
no key/library is present (the UI explains how to set it up rather than silently
degrading).

### How to use it

1. `export OPENAI_API_KEY=…` and `pip install openai` (optional
   `export OPENAI_MODEL=…`), then launch from that terminal with `just run`.
2. Tools → _Speedrun (MCAT)_ → **Enable AI rewording**.
3. Tools → _Speedrun (MCAT)_ → **Generate AI flashcard variants…** — reports how
   many variants were written vs. blocked by the quality gate.

### What was deliberately skipped

- **No mobile AI-generation UI** — generation is desktop-only; Android only
  consumes the shared engine (and excludes AI variants from Memory).
- **No chatbot / live tutor** and **no auto-generation on import** — generation
  is always an explicit, opt-in action.
- **No fine-tuning** — a general model with a strict grounding prompt plus an
  offline, deterministic grader keeps the gate cheap, auditable, and
  reproducible.
- **No LLM-as-grader** — grading is a deterministic heuristic so the pass/fail
  gate is offline and repeatable, not model-dependent.
- **No free-form question generation** — only grounded rewordings of existing
  items, a deliberate safety and licensing choice.

## About Anki

Anki is a spaced repetition program. Please see the [website](https://apps.ankiweb.net) to learn more.

## MCAT question bank

The fork ships a vendored, **legally reusable** bank of MCAT-relevant practice
questions, imported once on desktop as native Anki notes (Tools →
_Speedrun (MCAT)_ → _Import question bank_). Because they are native objects, one
import **syncs to Android and every other device** through Anki's normal sync —
no per-device re-import, no side tables. The bank is regenerated by
[`tools/speedrun/build_question_bank.py`](tools/speedrun/build_question_bank.py)
and stored (gzipped) at `pylib/anki/data/speedrun_question_bank.json.gz`.

Full source licensing is documented under [Credits & attribution](#credits--attribution).

## Getting Started

### Contributing

Want to contribute to Anki? Check out the [Contribution Guidelines](./docs/contributing.md).

For more information on building and developing, please see [Development](./docs/development.md).

#### Contributors

The following people have contributed to Anki: [CONTRIBUTORS](./CONTRIBUTORS)

### Anki Betas

If you'd like to try development builds of Anki but don't feel comfortable
building the code, please see [Anki betas](https://betas.ankiweb.net/).

## Credits & attribution

### Anki (upstream)

Anki-Plus is a fork of **Anki**, created by Damien Elmes and
[Ankitects Pty Ltd](https://apps.ankiweb.net/) and its
[contributors](./CONTRIBUTORS). All credit for the underlying spaced-repetition
engine, FSRS scheduler, sync, and application goes to the Anki project. Anki is
licensed **AGPL-3.0-or-later** (some components are BSD-3); this fork keeps the
same license. Upstream: <https://github.com/ankitects/anki>.

### MCAT question bank

The bundled practice questions come from the following **free, license-clean**
sources. Thank you to their authors — please keep these credits if you reuse
this fork.

| Source                                                                                                                   | License  | Use in this fork                                                                                                                                                                    |
| :----------------------------------------------------------------------------------------------------------------------- | :------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [OpenMCAT](https://github.com/Zushah/OpenMCAT)                                                                           | AGPL-3.0 | MCAT-specific C/P, B/B and P/S questions with explanations. Content is **AI-generated**; imported items are tagged `bank::ai-generated` so the M2 evaluation harness can gate them. |
| [MMLU](https://github.com/hendrycks/test) (Hendrycks et al., 2021, [arXiv:2009.03300](https://arxiv.org/abs/2009.03300)) | MIT      | MCAT-relevant subsets only (college/high-school biology, chemistry, physics; anatomy, medicine, genetics, nutrition; psychology; sociology). MMLU carries no upstream explanations. |

**Deliberately excluded, for licensing reasons:**

- **Jack Westin** — its original passages and questions are copyrighted, and its
  own support documentation states that no one may reproduce or share its
  content on another platform. It is therefore **not redistributable** and is
  never fetched or bundled.
- **Khan Academy MCAT content** — licensed CC BY-NC-SA. The **NonCommercial**
  and **ShareAlike** terms are incompatible with this AGPL app, so it is not
  bundled. Khan Academy's material remains available for free at
  [khanacademy.org](https://www.khanacademy.org) under its own license.

### Trademarks

**MCAT** is a registered trademark of the **Association of American Medical
Colleges (AAMC)**. This project is not affiliated with, endorsed by, or
sponsored by the AAMC, and is not a substitute for official AAMC materials.

## License

Anki-Plus is licensed under the GNU AGPL-3.0-or-later, the same as upstream
Anki: [LICENSE](./LICENSE).
