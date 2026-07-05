# Speedrun paraphrase test (brief §7d / M4 §4a)

Proves the Performance model measures **transfer**, not just card memorization.
If a student's accuracy on reworded questions equals their card recall, the
Performance model is just copying the Memory model — no bridge. We report the
**gap**.

## The set

`tools/speedrun/data/paraphrase_set.json` — **30 cards**, each with **2
exam-style questions** that test the same idea in new words (60 questions),
grounded in the MCAT first-principles topics. `correct` is the 0-based option
index. Each card carries a `card_id`; when imported, its reworded questions get a
`paraphrase::<card_id>` tag so the harness can match answers back to the card.

## Run it

```bash
just speedrun-paraphrase -- --demo                     # synthetic; no build
just speedrun-paraphrase -- --collection ~/col.anki2   # real study data
just speedrun-paraphrase -- --demo --transfer-penalty 0.0   # the NULL case
```

## What it reports

For each card: **card recall** (Memory model P(recall)) vs **reworded accuracy**
(the student's accuracy on the 2 reworded questions), then overall + per-topic
means and the **gap = recall − reworded accuracy**. A gap > ~0.05 means
performance ≠ memory (the bridge exists); a ~0 gap is reported honestly as "no
bridge."

## Method + honesty

- Gap aggregation is pure Python, unit-tested
  (`pylib/tests/test_speedrun_paraphrase.py`), so the maths is verified without a
  build.
- `--demo` uses a synthetic learner where reworded accuracy sits below card
  recall by a tunable `--transfer-penalty` (default 0.18); at penalty 0 the gap
  collapses to ~0 — the honest null. Demo numbers are labelled SYNTHETIC.
- `--collection` computes card recall from FSRS retrievability and reworded
  accuracy from real revlog answers to the tagged questions; refined at
  integration against a studied deck.

## Results

Reference demo run (transfer_penalty=0.18):

```
cards: 30   mean recall 0.769   mean reworded 0.589   GAP +0.180
verdict: MEANINGFUL GAP — performance != memory
```

Paste the real studied-deck numbers here:

```
TODO: real card-recall vs reworded-accuracy gap on a studied collection
```
