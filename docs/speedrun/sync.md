<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->

# Speedrun sync — verification & conflict rule (M2 2c)

This document closes the plan's **M2 2c** requirement: prove that the Speedrun
MCAT fork reuses Anki's existing sync (no rewrite), that two-way offline reviews
reconcile with **none lost or double-counted**, and **document the conflict
rule**.

> **Locked scope.** Reuse Anki's object-based sync. Every piece of custom
> Speedrun state is a **native Anki object** (notes, tags, cards, `revlog`), so
> it syncs for free — desktop and Android run the same Rust engine over the same
> protocol.

---

## 1. What syncs, and why it's free

Speedrun invents no side tables. Each concept maps to an object Anki already
syncs:

| Speedrun concept               | Native object                                  | Sync path           |
| ------------------------------ | ---------------------------------------------- | ------------------- |
| Practice question              | `SpeedrunQuestion` note + its card             | chunked notes/cards |
| Topic / pool / miss labels     | `topic::` / `pool::` / `miss::` **tags**       | unchunked changes   |
| Question↔card link             | shared `topic::` tag and/or `gates::<nid>` tag | (tags)              |
| A student answering a question | a **`revlog`** review row                      | chunked revlog      |
| Gated card activation          | card `queue`: `Suspended` → active             | chunked cards       |
| Blueprint / config             | collection config                              | unchunked changes   |

Because all of these are ordinary rows carrying a USN, Anki's incremental sync
(`rslib/src/sync/collection/`) transfers them with no Speedrun-specific code.

---

## 2. What was verified (automated)

An integration test drives the **exact Rust sync path both desktop and Android
share** (an in-process sync server, two collections), so passing it proves the
behaviour for both clients:

- **Test:** `speedrun_state_syncs_across_devices_offline`
- **Location:** `rslib/src/sync/collection/tests.rs`
- **Run:** `cargo test -p anki --lib speedrun_state_syncs_across_devices_offline`
  (or `just test-rust`)

It models the "desktop ↔ Android, both offline" scenario end-to-end:

1. **Baseline.** "Desktop" creates a served question linked (shared `topic::`)
   to a flashcard that starts **suspended** (SPOV3 "off"). Initial full sync
   pairs the two devices; the second device confirms it sees the same off-state.
2. **Offline divergence — same question on both devices.**
   - Desktop: a `knowledge-gap` miss → gated activation **unsuspends** the linked
     flashcard, a `miss::knowledge-gap` **tag** is written, and the answer is
     recorded as a **`revlog`** review. It reconnects first.
   - Android: answers the **same question** offline (a second `revlog` row) and
     then syncs.
   - Desktop reconnects again to pull Android's review.
3. **Reconciliation assertions (on _both_ devices):**
   - Both offline reviews of the same card survive **exactly once** →
     `select count() from revlog where cid=…` equals `2` (nothing lost, nothing
     double-counted).
   - The gated **activation propagated** (flashcard no longer `Suspended`).
   - The **`miss::knowledge-gap` tag propagated**.
   - Whole-object equality across devices for the question note, flashcard note,
     flashcard card, and each `revlog` row; identical total review counts.
   - `check_database()` passes on both collections (no corruption).

**Why this is honest:** reviews are append-only rows keyed by a unique
epoch-millisecond id, so two devices answering the same card offline produce two
distinct rows that both survive the merge — there is no last-writer-wins that
could silently drop or duplicate a review.

For the manual desktop↔phone demo (AnkiWeb login on both, change → Sync → Sync),
see [`android.md` §0](android.md).

---

## 3. The conflict rule (documented)

Anki decides sync direction from the server/client **meta** comparison in
`SyncMeta::compared_to_remote` (`rslib/src/sync/collection/meta.rs`):

```rust
let required = if remote.modified == local.modified {
    SyncActionRequired::NoChanges
} else if remote.schema != local.schema {
    // schema conflict → forced one-way full sync
    SyncActionRequired::FullSyncRequired { upload_ok, download_ok }
} else {
    SyncActionRequired::NormalSyncRequired // incremental merge
};
```

Two regimes follow:

### Incremental sync (same schema, different modification time)

The normal path. Objects changed since the last sync are exchanged and **merged
by id + USN**. For Speedrun this means:

- **`revlog` merges from both devices.** Each review is its own row with a unique
  id, so offline reviews on both devices are **all kept** — none lost, none
  double-counted (verified above). Reviews are never "resolved" against each
  other; they simply coexist.
- For a **single mutable object** edited on both sides (e.g. the same card's
  `queue`, or a note's tags), the exchange is **last-writer-wins by modification
  time** — the more recently modified version wins. Speedrun activation only ever
  flips `Suspended → active` and re-applying it is idempotent, so a concurrent
  activation converges rather than conflicts.

### Schema conflict → forced one-way full sync

When the **schema timestamp (`scm`)** differs, Anki **cannot merge** and forces
a **full one-way sync**; the user must pick a direction and the chosen side
**overwrites** the other wholesale (`upload_ok`/`download_ok` gate which
directions are offered; an empty collection may always be overwritten). A schema
bump is caused by structural changes — adding/removing a notetype, changing
fields/templates, a full upload/download, or a collection exceeding the max sync
payload (`meta.rs` sets `schema = now()` to force one-way in that case).

**Implication for Speedrun:** the day-to-day loop (answering questions, gated
activation, tagging) only bumps the **modification** time, so it always takes the
merging incremental path. The one operation that changes schema is
**provisioning the data model** (creating the `SpeedrunQuestion` notetype /
`Speedrun::Questions` deck on first run). Do that on **one** device and let the
initial full sync carry it to the others **before** studying on a second device,
so the two never diverge at the schema level. After that first pairing, all
Speedrun activity reconciles incrementally.

> This matches upstream Anki behaviour exactly — we add no new conflict logic.
> The regression `sync::collection::tests::regular_sync` also shows a schema
> change (removing a notetype) forcing `FullSyncRequired`.
