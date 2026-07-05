# Demo video script + shot checklist (3–5 min, brief §12)

Target 3–5 minutes. Cover every required beat: a review session, the Rust change
in action, a card synced phone→desktop, the three scores with ranges, the AI
features, and test results.

## Before recording

- Build both apps: desktop (`just run`) and Android (`just android-run --rebuild`).
- Study a short real session (or import the question bank) so scores leave
  abstention — an empty deck shows "—" and looks broken.
- Have a terminal ready for the test/eval commands.

## Shot list

1. **(0:00–0:30) Intro + exam.** Repo README on screen: "MCAT, two apps, one
   engine." State the thesis: practice questions drive the flashcards.
2. **(0:30–1:15) Question-first loop + the Rust change.** Answer a practice
   question wrong → pick a miss reason. Show `knowledge-gap`/`missing-context`
   **activating** linked cards, and `misunderstanding`/`careless` **not** — this
   is `ActivateCardsForMiss` in the Rust engine. Mention the value-ordered queue
   (`topic_weight × weakness`).
3. **(1:15–2:00) Three scores with ranges.** Open the dashboard: Memory,
   Performance, Readiness each with a range + coverage; show one **abstaining**
   ("—") on thin data to prove the give-up rule is honest.
4. **(2:00–2:45) Phone ↔ desktop sync.** Review a card on the phone, Sync, show
   it appear on desktop (and reverse). One engine, shared via AnkiWeb.
5. **(2:45–3:30) AI features.** Generate an AI flashcard variant grounded in a
   named source; show it carries a traceable `source`. Run
   `python tools/speedrun/rephrase_eval.py --mock` → accuracy, wrong-rate,
   baseline beat, leakage clean, PASS.
6. **(3:30–4:30) Test results.** Run and show:
   - `just speedrun-validate -- --demo` (calibration Brier/log loss, AUC)
   - `just speedrun-paraphrase -- --demo` (the gap)
   - `just speedrun-ablation` (3-build comparison)
   - `just bench` (p50/p95/worst table) — or paste the captured table.
7. **(4:30–5:00) Close.** "One engine change, three scores we can back up,
   honest uncertainty, on desktop and phone." Note what's still synthetic vs real.

## Honesty reminders on camera

- Label any synthetic/seeded data as such.
- Never show a readiness number without its range + abstention.
