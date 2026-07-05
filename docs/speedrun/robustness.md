# Speedrun robustness (brief §7g / §10 hard limits)

Two hard limits: **crashing mid-review must never corrupt the collection**, and
**turning off AI/network must degrade gracefully** (scores still produced).

## Crash recovery — zero corrupted collections

Automated on desktop:

```bash
# builds pylib first via any recipe, then:
out/pyenv/bin/python tools/speedrun/crash_test.py --trials 20
```

`crash_test.py` copies a seeded collection, spawns a child that answers cards in
a loop, **SIGKILLs it at a random mid-write moment**, reopens the collection and
runs Anki's `check_database` integrity check. It must report **20/20 clean**.
This works because all Speedrun state is native Anki objects in the SQLite
collection, which uses WAL + transactional writes — activation
(`ActivateCardsForMiss`) is atomic and undo-safe, so a kill leaves either the
pre- or post-state, never a torn one.

Android: repeat manually — start a review, force-stop the app (or `adb shell am
force-stop`) 20×, reopen, and confirm no "database corrupt" prompt and correct
counts. Record the run for the demo.

TODO: paste the `crash_test.py` 20/20 output + the Android force-stop notes.

## Offline / AI-off degradation

The three scores are computed **entirely in the Rust engine** with no network or
LLM dependency, so they work fully offline. AI card generation/rephrasing is the
only network feature.

Checklist:

1. Enable airplane mode (or block network).
2. Toggle the AI rephrase feature **off** in the Speedrun UI.
3. Confirm: study loop works; Memory/Performance/Readiness still render (with
   ranges + abstention); AI generation is disabled with a clear message, not a
   crash or a hang.
4. Re-enable network → AI features return.

TODO: paste offline screenshots / notes for both platforms.
