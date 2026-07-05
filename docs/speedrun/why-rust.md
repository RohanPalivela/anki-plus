<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->

# Why the engine change belongs in Rust

The marquee change of this fork — **question-gated card activation**, the
**coverage sweep**, and the **value-ordered review queue** (`value =
topic_weight × weakness`) — lives in Anki's **Rust core** (`rslib/src/speedrun/`
+ `rslib/src/scheduler/queue/builder/speedrun_value.rs`), not in the Python
(`pylib`/`aqt`) layer. Three reasons drove that decision (plan decision D-12).

---

## 1. Atomic, undo-safe state transitions

Activation flips a *set* of cards from `Suspended` back to their scheduled queue.
That is a mutation of collection state, and it must be **all-or-nothing** and
**reversible in one step** — a half-applied activation, or one that can't be
undone, would corrupt the study loop.

The Rust core already owns the only correct primitives for this. Activation runs
inside `transact(Op::ActivateForMiss, …)` and reuses the verified
`unsuspend_or_unbury_searched_cards` path
(`rslib/src/speedrun/activation.rs`), so it inherits Anki's transactional
guarantees for free:

- a single undo entry restores the exact prior queue state;
- it is idempotent on already-active cards (re-running a miss is safe);
- it **never touches FSRS intervals/due dates** — it governs *activation +
  ordering* only;
- `check_database()` passes after an activate → undo round-trip (verified by
  `activation_is_undoable_and_preserves_scheduling`).

Doing this from Python would mean re-implementing transaction/undo bookkeeping
*outside* the layer that owns the write path — exactly the kind of duplicated,
drift-prone state machine that causes silent collection corruption. The engine
belongs where the transaction boundary already is.

## 2. Performance on a 50k-card collection

The queue is rebuilt on every study session and must stay within the
interaction latency budget even on a large deck (§7h / §10). Value ordering
touches every gathered review/new card:

- the per-topic FSRS **mastery/weakness map** is computed in a single read-only
  pass over activated, topic-tagged cards (`mastery.rs::compute_topic_mastery`);
- each card's value is a batch tag-lookup + arithmetic during `gather_cards`
  (`speedrun_value.rs::compute_speedrun_values`);
- the final sort is an in-memory comparator (`value_order::compare_desc`).

Running this in Rust keeps it in the same process and memory space as the queue
builder — no per-card protobuf round-trips, no Python object churn on 50k cards.
The alternative (compute values in Python, ship them back into the Rust queue
builder) would add an O(cards) serialization tax to the hot path. `just bench`
(WS2) measures the actual p50/p95/worst on a 50k deck.

## 3. One engine, shared by Android for free

Desktop (PyQt) and Android (AnkiDroid) share **one** Rust core and **one**
protobuf schema; they differ only in the thin native bridge that carries RPC
calls into that core (see [`architecture.md`](architecture.md)). Because
`SpeedrunService` is Rust-implemented and **not** in `FrontendService`, every
piece of engine logic — activation, sweep, value ordering, and all three score
models — is callable from Android over JNI with **no Python and no
re-implementation** (verified reachable via `just speedrun-codegen-check`).

Had the gating logic lived in `pylib`, Android would have needed a second,
hand-ported copy of the engine in Kotlin — a guaranteed source of
platform-divergent scoring and drift. Putting it in Rust means desktop and phone
compute *identical* scores from *identical* data, which is also what makes the
fixed-seed Readiness simulation reproducible across devices.

---

## Summary

| Requirement | Why Rust wins |
| :--- | :--- |
| Atomic + undo-safe activation | Reuses the core's `transact`/undo + verified unsuspend path; never corrupts state or alters FSRS. |
| Fast on 50k cards | Single in-process pass; no per-card Python/IPC round-trips on the queue hot path. |
| Shared by Android | One engine behind one proto contract; AnkiDroid calls it over JNI with thin UI only. |

The trade-off is that engine changes require a full build to regenerate
bindings (a `cargo check` alone won't refresh the proto-derived clients), and
the queue-builder integration touches upstream files (see
[`files-touched.md`](files-touched.md)). Both are accepted costs for
correctness, performance, and a genuinely shared engine.
