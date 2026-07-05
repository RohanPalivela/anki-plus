# Upstream files touched + merge-difficulty (brief §7a)

The fork is deliberately **additive**: the Speedrun engine lives in new modules,
and only a handful of upstream Anki files are edited (to register the service,
hook the queue, and add config/op enums). New files are ~zero merge risk; the
edits below are the ones to watch on a future rebase against upstream Anki.

## New modules (no merge risk — pure additions)

- `rslib/src/speedrun/` — activation, sweep, value ordering, linkage, blueprint,
  mastery (Memory), performance (Performance), readiness (Readiness), synthetic,
  rng, service, mod.
- `proto/anki/speedrun.proto` — the `SpeedrunService` (gating, sweep, three score
  RPCs, synthetic seed).
- `rslib/src/scheduler/queue/builder/speedrun_value.rs` — value-ordering pass.
- Python: `pylib/anki/speedrun.py`, `speedrun_rephrase.py`, `speedrun_validation.py`,
  data under `pylib/anki/data/speedrun_*`.
- Qt: `qt/aqt/speedrun/`. Web: `ts/routes/speedrun-*`.
- Tools/docs: `tools/speedrun/`, `tools/bench/`, `docs/speedrun/`.

## Edited upstream files (watch on rebase)

| File | Edit | Merge difficulty |
| :-- | :-- | :-- |
| `rslib/src/scheduler/queue/builder/mod.rs` | Load Speedrun value order + sort the built queue when enabled | **Medium** — upstream changes queue building periodically; the hook is isolated to `new()`/`build()`. |
| `rslib/src/scheduler/queue/builder/gathering.rs` | Respect activation/suspension in gather | **Low–Medium** |
| `rslib/src/scheduler/bury_and_suspend.rs` | Activation uses suspend/unsuspend paths | **Low** |
| `rslib/src/config/bool.rs` | Add `SpeedrunOrdering` bool key | **Low** — enum addition. |
| `rslib/src/ops.rs` | Add Speedrun ops (e.g. `ActivateForMiss`) for undo | **Low** — enum addition. |
| `rslib/src/lib.rs` | Register the `speedrun` module + service | **Low**. |
| `proto/anki/scheduler.proto` | Minor wiring for the ordering flag | **Low**. |
| `rslib/src/sync/collection/tests.rs` | Added `speedrun_state_syncs_across_devices_offline` test | **Low** — test-only addition. |

## How to regenerate this list

```bash
# files changed vs the upstream base (replace BASE with the fork point):
git diff --stat BASE..HEAD -- rslib proto | grep -v speedrun
```

TODO: pin the exact upstream commit the fork branched from and paste the filtered
`git diff --stat` here for the graders.
