<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->

# Speedrun on Android

This document is the **mobile plan of record** for the Speedrun MCAT fork. It
explains how the shared Rust engine reaches Android, what already exists in this
repo, what must happen in the external **AnkiDroid** repo, and the exact
step-by-step path to a reviewing APK. It closes the "how do we do mobile?"
question raised by the implementation plan (M0 A4, M1 1c, M2 2c) and the top
risk **R1** (AnkiDroid consumption of `SpeedrunService`).

> **Locked scope reminder.** Android-only mobile via AnkiDroid; iOS is out of
> scope. All custom state is native Anki objects, so Anki's existing
> object-based sync carries Speedrun data to Android for free — no sync rewrite.

---

## 0. Quick start — run the Android app

Two scripts automate the whole thing. They live in this fork under `tools/` and
assume the three repos are (or will be) **siblings** in one folder:

```
<parent>/
├── anki-plus/              ← this fork (desktop + Rust core + SpeedrunService)
├── Anki-Android/           ← the AnkiDroid app (cloned by the setup script)
└── Anki-Android-Backend/   ← rsdroid JNI bridge (cloned by the setup script)
```

### First time on a new machine (installs everything)

```bash
just android-setup
# or: ./tools/android-setup
```

This installs JDK 17 + the Android SDK/NDK/emulator, clones the two external
repos, points the backend at this fork, builds the backend AAR (with
`SpeedrunService`) and the AnkiDroid APK, then launches an emulator and installs
the app. macOS + Homebrew + rustup required. Expect a long first run (multi-GB
downloads + a full build).

### Day-to-day (dependencies already installed)

```bash
just android-run              # launch the emulator + (re)install the built app
just android-run --rebuild    # rebuild backend + app first (after changing Speedrun code)
```

### Then: the desktop ↔ phone sync demo

1. **Emulator:** on the onboarding screen tap **"Sync from AnkiWeb"** and log in.
2. **Desktop:** `just run` in `anki-plus`, then Preferences → Syncing → log into
   the **same** AnkiWeb account → **Sync**.
3. Change something on one device → Sync → Sync on the other. The MCAT deck and
   all Speedrun data (`SpeedrunQuestion` notes, `topic::`/`pool::`/`miss::` tags,
   `revlog`, blueprint config) propagate — they are native Anki objects, so
   existing sync carries them to the phone, which runs the **same Rust engine**.

> **What this proves vs. not.** This demonstrates the shared engine + full data
> sync (including all Speedrun objects) on Android. The Speedrun **Kotlin UI**
> (question surface, score tiles) is not built into AnkiDroid yet — that is the
> M2 mobile-UI task. The RPCs are present and callable in the app's backend.

### Configuration knobs (env vars for both scripts)

| Var               | Default                                        | Meaning                          |
| ----------------- | ---------------------------------------------- | -------------------------------- |
| `SPEEDRUN_BRANCH` | current branch of this fork                    | fork branch the backend compiles |
| `SPEEDRUN_API`    | `35`                                           | emulator system-image API level  |
| `SPEEDRUN_AVD`    | `speedrun_arm64`                               | emulator name                    |
| `ANDROID_HOME`    | `/opt/homebrew/share/android-commandlinetools` | SDK location                     |

### Gotchas (the scripts handle these; know them anyway)

- **Repos must be siblings** and `Anki-Android-Backend` must **not** be renamed —
  AnkiDroid hard-codes that path when `local_backend=true`.
- **Version alignment matters.** This fork is anki `26.05`; the backend targets
  `anki26.05b1`. That one-version gap makes AnkiDroid `main` fail to compile a
  non-exhaustive `when` over a proto enum, so the setup script auto-patches
  `libanki/.../Deck.kt` to add the `Order.RELATIVE_OVERDUENESS` branch. If more
  enums drift, add branches similarly (or check out an AnkiDroid revision built
  against the exact anki version).
- **Apple Silicon → `arm64-v8a`** (fast, native). The backend builds that ABI by
  default there; use an arm64 system image. Intel hosts get `x86_64`. For a
  multi-ABI release AAR, build the backend with `ALL_ARCHS=1`.
- **NDK version is exact** — read from `Anki-Android-Backend/gradle/libs.versions.toml`.
- **JDK must be 17/21/25** — the backend's Gradle rejects other majors.

The sections below explain the architecture and the manual equivalents of what
these scripts do.

---

## 1. Architecture — one Rust core, two native bridges

Desktop and Android share **one** Rust core (`rslib/`, crate `anki`) and **one**
protobuf schema (`proto/anki/*.proto`). They differ only in the native bridge
that carries protobuf-encoded RPC calls into that core:

```
                      proto/anki/*.proto  (single source of truth)
                                 │
                  full build (just check / just build)
                                 │
                 out/rslib/proto/descriptors.bin  ── service/method indices
                                 │
     ┌───────────────────────────┴───────────────────────────┐
     │                                                         │
Desktop bridge                                          Android bridge
pylib/rsbridge (PyO3 cdylib)                            rsdroid (JNI)  ← EXTERNAL
     │                                                         │
Python  col._backend.*                                  Kotlin backend wrapper
     │                                                         │
qt/aqt + ts/ (Speedrun UI)                              AnkiDroid Kotlin UI ← EXTERNAL
```

Every RPC is dispatched the same way on both platforms:

`Backend::run_service_method(service: u32, method: u32, input: &[u8]) -> Vec<u8>`

- Services are auto-discovered from `proto/**/*.proto`
  (glob in `build/configure/src/web.rs`).
- `rslib/proto_gen/src/lib.rs::get_services` pairs each `FooService` with a
  required `BackendFooService` and assigns stable numeric indices.
- Collection-scoped RPCs (including all of `SpeedrunService`) are implemented on
  `Collection` and reached from `Backend` via `with_col(...)`.
- Desktop calls this from `pylib/rsbridge/lib.rs`; Android calls the identical
  function over JNI from the external `rsdroid` crate.

**Where Speedrun fits.** `proto/anki/speedrun.proto` declares `SpeedrunService`
(`ActivateCardsForMiss`, `RunCoverageSweep`, `GetMemoryScore`) plus the required
empty `BackendSpeedrunService`. Because it is **not** in `FrontendService`, it is
Rust-implemented (`rslib/src/speedrun/service.rs`) and therefore available to
Android for free — this is exactly why the score models live in Rust (decision
D-12): Android needs only thin UI, no Python.

> `proto/anki/ankidroid.proto` + `rslib/src/ankidroid/` are **unrelated** to
> Speedrun. They are pre-existing Android plumbing (paginated SQLite proxy for
> `CursorWindow` limits, legacy scheduler timing). Speedrun uses the standard
> service path, not `AnkidroidService`.

---

## 2. What already exists in this repo (mobile-ready)

The **engine + contract** side of Android is already done and verified:

| Piece                     | Location                                                                                                  | State                     |
| ------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------- |
| Speedrun RPC contract     | `proto/anki/speedrun.proto` (`java_multiple_files = true`)                                                | Kotlin/Java-codegen ready |
| Rust engine               | `rslib/src/speedrun/` (activation, sweep, mastery, blueprint, value ordering, service)                    | Compiles + tests pass     |
| Service registration      | `rslib/proto/src/lib.rs` (`protobuf!(speedrun, "speedrun")`)                                              | Wired                     |
| Android-aware core        | `rslib/src/storage/sqlite.rs`, `collection/backup.rs`, `media/files.rs` (`#[cfg(target_os = "android")]`) | Upstream, reused          |
| Cross-compile smoke check | `tools/android-check` → `just android-check`                                                              | **NEW (this repo)**       |
| Codegen verification      | `tools/speedrun-codegen-check` → `just speedrun-codegen-check`                                            | **NEW (this repo)**       |

`just speedrun-codegen-check` (run after a build) confirms `SpeedrunService`
appears in `out/rslib/proto/descriptors.bin`, `out/pylib/anki/_backend_generated.py`,
and `out/ts/lib/generated/backend.ts` — i.e. the exact descriptor pool AnkiDroid
regenerates its Kotlin/JNI client from.

**What is NOT in this repo (by design):** no Gradle/Kotlin, no `rsdroid`/JNI
crate, no Android NDK config, no APK. The runnable app lives in the external
[AnkiDroid](https://github.com/ankidroid/Anki-Android) repo, which consumes this
fork's `rslib`.

---

## 3. Phase scope — what "mobile" means per milestone

| Milestone | Full-plan target                                                                                    | This phase (`02_build_prompt.md`)                                            |
| --------- | --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| **M0 A4** | AnkiDroid build links this fork's backend and opens a deck                                          | Documented follow-up; **not a blocker**                                      |
| **M1 1c** | Desktop installer + Android **review** build on shared engine (no Speedrun UI yet; no two-way sync) | **Scaffolding/notes only** (this doc + cross-compile smoke + codegen verify) |
| **M2 2c** | Reuse sync; Android renders the three scores via `SpeedrunService` RPCs; Kotlin question surface    | Later phase                                                                  |

So for the current phase the in-repo deliverable is: **prove the shared engine
compiles for Android and the Speedrun contract is exposed**, and document the
external steps. That is what the two new `just` recipes + this doc provide.

---

## 4. In-repo steps (do these here)

### 4.1 Verify the contract reached every backend

```bash
just build                 # regenerates out/ (bindings, descriptors)
just speedrun-codegen-check
```

Expected: all checks `ok`. If you edited `proto/anki/speedrun.proto`, you MUST
run a full build — `cargo check` alone will not regenerate bindings (risk R10).

### 4.2 Cross-compile the shared core for Android

Install the NDK (Android Studio SDK Manager, or
`sdkmanager --install "ndk;26.1.10909125"`), then:

```bash
export ANDROID_NDK_HOME="$HOME/Library/Android/sdk/ndk/26.1.10909125"
just android-check
```

This runs `cargo build -p anki --target aarch64-linux-android`, proving the
engine + `speedrun` module compile for the primary device ABI. Add more ABIs
with `ANDROID_TARGETS="aarch64-linux-android x86_64-linux-android armv7-linux-androideabi"`.

> This compiles the Rust `rlib`; it does **not** produce a JNI `.so`. Producing a
> loadable `.so` requires `rsdroid`'s JNI glue, which lives in AnkiDroid.

### 4.3 Freeze the RPC contract (hand-off C2/C3)

`proto/anki/speedrun.proto` is the stable interface AnkiDroid depends on. Any
change shifts service/method indices in the descriptor pool and forces an
AnkiDroid regen. Freeze `SpeedrunService`, `MissReason`, `ActivateCardsResponse`,
and the score messages before Kotlin work starts, so mobile can proceed in
parallel.

---

## 5. External steps (in the AnkiDroid repo)

These require a clone of AnkiDroid and cannot be done in this repo (R1 — the
top, least-controllable risk):

1. **Pin the backend.** Point AnkiDroid's `rsdroid`/backend dependency at this
   fork's `rslib` commit (git submodule or path dependency to `anki-plus`).
2. **Regenerate the JNI/protobuf backend** against this fork's proto set /
   `descriptors.bin`, so `SpeedrunService` appears in Kotlin with the correct
   service/method indices (use `get_services()` order, never `.enumerate()`).
3. **JNI smoke test** each RPC from Kotlin against a sample collection:
   `activateCardsForMiss`, `runCoverageSweep`, `getMemoryScore`.
4. **M1 minimal app** — stock AnkiDroid reviewer opening a deck on the forked
   engine (no Speedrun UI required to satisfy M1 1c).
5. **M2 UI** — question-first surface, miss-reason chooser, and three-score
   dashboard tiles that call the Rust RPCs.
6. **CI drift guard** — a periodic job that builds AnkiDroid against this fork to
   catch proto/index drift early.

**Mitigations baked into the design:** all scoring is in Rust (D-12), so Android
needs only thin UI; the C2/C3 contracts are frozen at the proto layer so Kotlin
work runs in parallel; and Android Speedrun UI should sit behind a feature flag
so a lagging port never blocks the desktop track.

---

## 6. Known blockers / unknowns

- **NDK + toolchain** are not configured in `.cargo/config.toml`; each dev/CI
  machine must install the NDK and Rust Android targets (`just android-check`
  handles the target install and linker wiring given `ANDROID_NDK_HOME`).
- **`rsdroid` / JNI** is external; no loadable `.so` or APK can be produced or
  tested inside this repo.
- **TLS**: desktop Linux wheels use `rustls`; verify AnkiDroid's build selects
  the intended TLS feature (`rustls` vs `native-tls`, see `rslib/Cargo.toml`
  features) for the Android target.
- **Sync**: two-way offline reconcile is verified against the shared Rust sync
  path (see [`sync.md`](sync.md) — test
  `speedrun_state_syncs_across_devices_offline`, plus the conflict rule). The
  Android **score UI** is still M2 2c. M1 1c only needs a reviewing build.

---

## 7. Quick reference

```bash
just speedrun-codegen-check   # after a build: is SpeedrunService exposed everywhere?
ANDROID_NDK_HOME=... just android-check   # does the shared engine compile for Android?
```

- Engine: `rslib/src/speedrun/`
- Contract: `proto/anki/speedrun.proto`
- Descriptor pool (AnkiDroid regenerates from this): `out/rslib/proto/descriptors.bin`
- Desktop bridge reference: `pylib/rsbridge/lib.rs`
- Top risk: R1 in `planning/Anki_Implementation_Plan.md` §6
