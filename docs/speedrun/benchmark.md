# Speedrun benchmark (§7h / §10)

One command loads the shared **50,000-card** deck and prints **p50 / p95 /
worst** for each latency-budgeted action, next to its §10 budget and a PASS/FAIL
verdict. "One hand-picked number does not count" — every action reports its full
distribution.

## Run it

```bash
just bench                 # generates a 50k deck (once) then benchmarks
just bench -- --deck /path/to/collection.anki2 --iterations 2000
```

`just bench` builds `pylib` first (the benchmark needs the compiled Rust
backend), generates a deterministic 50k-card deck if you don't pass `--deck`,
then runs the harness. Re-runs reuse a `--deck` you pass in.

Generate a deck on its own:

```bash
python tools/bench/gen_deck.py --out /tmp/speedrun_bench.anki2 --count 50000
```

## What is measured

| Action                      | How                                          | §10 budget   |
| :-------------------------- | :------------------------------------------- | :----------- |
| Button press ack            | `sched.answerCard(card, Good)`               | p95 < 50 ms  |
| Next card appears           | `sched.getCard()`                            | p95 < 100 ms |
| Dashboard first load (cold) | `get_{memory,performance,readiness}_score()` | p95 < 1 s    |
| Dashboard refresh (warm)    | same, warmed                                 | p95 < 500 ms |

Interactive actions (button ack, next card) are additionally checked against the
cross-cutting **"no UI freeze > 100 ms"** rule on their worst sample.

## Design notes

- Percentile maths and the PASS/FAIL table live in `tools/bench/bench_lib.py`,
  which is **backend-free** and unit-tested (`tools/bench/test_bench_lib.py`)
  so the grading logic runs on a fresh checkout without a build.
- The deck generator builds native `Basic` notes across the MCAT `topic::` tags,
  leaves ~30% activated (unsuspended) with seeded review history, and suspends
  the rest (Speedrun's "off by default" model) so the score RPCs have real state.

## Results

Paste the latest run here (machine, date, commit):

```
TODO: paste `just bench` output table
```
