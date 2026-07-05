# Sync proof runbook (brief §7b)

Goal: prove progress syncs **both ways** between desktop and Android, including a
same-card conflict, reusing Anki's existing sync (no custom protocol). The rules
are in `docs/speedrun/sync.md`; this is the step-by-step to capture the evidence.

## Engine-level proof (automated, already committed)

```bash
just test-rust        # or: cargo test -p anki speedrun_state_syncs
```

`rslib/src/sync/collection/tests.rs::speedrun_state_syncs_across_devices_offline`
exercises two collections syncing all Speedrun state (activation, revlog, tags,
blueprint config) through the normal sync path.

TODO: paste the captured passing `cargo test` log here.

## Manual two-device proof (§7b, for the demo)

Prereq: both apps signed into the same AnkiWeb account (or a self-hosted
syncserver — see `docs/speedrun/developers/syncserver` for offline demos).

1. **Desktop → phone.** On desktop, answer 10 practice questions (miss a couple
   with `knowledge-gap` so cards activate). Sync. On the phone, Sync and confirm
   the reviews, activated cards, and updated scores appear.
2. **Phone → desktop.** On the phone, study 10 activated cards. Sync. On desktop,
   Sync and confirm the new reviews + changed Memory/Readiness.
3. **Offline both sides.** Put both offline. Review different cards on each.
   Bring both online and sync sequentially.
   - **Reviews (revlog):** both devices' review rows are kept (append-only) — no
     history lost.
   - **Mutable objects (card state, tags, config):** last-writer-wins by
     modification time (standard Anki).
4. **Same-card conflict.** Offline, answer the **same** card differently on each
   device (Again vs Good). Sync both. Confirm: both revlog rows survive; the
   card's scheduling reflects the later modification; no crash, no corruption.

TODO: paste screenshots / screen-recording timestamps for steps 1–4.

## What this proves

Two-way sync of all Speedrun progress with a deterministic, explained conflict
rule — on top of unmodified Anki sync infrastructure.
