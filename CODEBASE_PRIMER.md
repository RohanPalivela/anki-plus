# Anki Codebase Primer

> Orientation guide for autonomous coding agents (and their human supervisors)
> who have never seen this repo. Optimized for fast onboarding and avoiding
> footguns. Every path and command below was verified against the working tree.

---

## Table of Contents

1. [TL;DR](#1-tldr)
2. [Architecture Overview](#2-architecture-overview)
3. [Tech Stack & Languages](#3-tech-stack--languages)
4. [Build, Run, Test, Lint](#4-build-run-test-lint)
5. [Cross-Language / Codegen Boundaries](#5-cross-language--codegen-boundaries)
6. [Key Directories Deep-Dive](#6-key-directories-deep-dive)
7. [Conventions & Gotchas](#7-conventions--gotchas)
8. ["Where Do I Make This Change?" Playbook](#8-where-do-i-make-this-change-playbook)
9. [Testing & Verification](#9-testing--verification)
10. [Existing Documentation Index](#10-existing-documentation-index)
11. [Pitfalls / Do-Not-Touch](#11-pitfalls--do-not-touch)
12. [Stale / Unresolved References](#12-stale--unresolved-references)

---

## 1. TL;DR

**Anki** is a spaced-repetition flashcard application with a multi-layer
architecture: a **Rust core** (`rslib/`) holds the bulk of the business logic,
a **Python library** (`pylib/`) wraps it via a Rust bridge (`pylib/rsbridge/`),
a **PyQt GUI** (`qt/aqt/`) drives the desktop app, and a **Svelte/TypeScript
frontend** (`ts/`) renders embedded web views. The layers communicate through
**Protocol Buffers** defined in `proto/anki/`, from which Rust, Python, and
TypeScript bindings are auto-generated into `out/`.

The project is driven by **`just` recipes** (see `justfile`), which wrap a
custom Ninja-based build system in `build/`. **Do not invoke `./ninja`,
`./run`, or `tools/` scripts directly — use the `just` recipes.**

The 4 commands an agent needs most:

```bash
just run     # Build pylib + qt and launch Anki in dev mode (web served at http://localhost:40000/_anki/pages/)
just check   # Format + full build + all lint/type checks + all tests — run this before completing a task
just lint    # clippy + mypy + ruff + eslint + svelte + typescript (needs build outputs)
just test    # Run all tests (Rust, Python, TypeScript)
```

Quick per-language iteration: `cargo check` (Rust), `just test-rust`,
`just test-py`, `just test-ts`. Format with `just fmt` (check) / `just fix-fmt`
(apply). See `just --list` for the full recipe set.

---

## 2. Architecture Overview

### Layers

Anki is logically split into a **library** (backend) and a **GUI** (frontend),
spanning three programming languages. Each layer may make RPC calls **down** to
layers below it; lower layers never call up. (Source: `docs/architecture.md`,
`docs/language_bridge.md`.)

| Layer          | Language          | Location          | Responsibility                                                                    |
| -------------- | ----------------- | ----------------- | --------------------------------------------------------------------------------- |
| Core           | Rust              | `rslib/`          | Collections, scheduling, search, media, sync, import/export — most business logic |
| Rust bridge    | Rust (PyO3)       | `pylib/rsbridge/` | Exposes the Rust API to Python as the private `rsbridge` module                   |
| Python library | Python            | `pylib/anki/`     | `import anki`; thin wrappers that proxy to Rust + legacy Python logic             |
| Desktop GUI    | Python + PyQt     | `qt/aqt/`         | `import aqt`; windows, dialogs, hooks, the `mediasrv` web server                  |
| Web frontend   | Svelte/TypeScript | `ts/`             | Embedded web views (deck options, graphs, editor, etc.)                           |
| IPC schema     | Protobuf          | `proto/anki/`     | Defines backend methods + some on-disk storage formats                            |

### Data / Control Flow

```
+-----------------------------------------------------------+
|                     Desktop GUI                           |
|  qt/aqt/  (PyQt, Python)        ts/  (Svelte/TypeScript)   |
|        |                                |                  |
|        |  col.decks.new_deck()          |  getCsvMetadata()|
|        |  (helpers in pylib/anki/)      |  (@generated/    |
|        v                                |     backend)     |
|  +-----------+                          |   POST /_anki/.. |
|  |  pylib/   |  col._backend.* <---------+        |        |
|  |  anki/    |                                    |        |
|  +-----+-----+                                    |        |
|        |  rsbridge (PyO3)                          |        |
+--------|-------------------------------------------|-------+
         v                                           v
   +-----------------------------------------------------+
   |              Rust core  (rslib/)                    |
   |   *Service impls in rslib/src/<area>/service.rs     |
   |   May call OUT to AnkiWeb / sync servers            |
   +-----------------------------------------------------+
         ^                                           ^
         |     proto/anki/*.proto  (shared schema)   |
         +-------------------------------------------+
            generated bindings live in out/ (Rust/Py/TS)
```

- **Python → Rust:** Python helpers in `pylib/anki/*.py` call `col._backend.*`
  (snake_case methods generated from protobuf RPCs). Most callers should use the
  helper, not `_backend` directly. (`docs/language_bridge.md`.)
- **TypeScript → Rust:** TS imports RPC functions from `@generated/backend` and
  types from `@generated/anki/*_pb`; calls become POST requests to `/_anki/...`
  served by `qt/aqt/mediasrv.py`. (`docs/language_bridge.md`, `docs/e2e-testing.md`.)
- **Python → TypeScript** (discouraged): via `web.eval` / `web.evalWithCallback`
  in `qt/aqt/` — does not use protobuf. (`docs/language_bridge.md`.)
- **RPC routing rule:** RPCs declared in `FrontendService`
  (`proto/anki/frontend.proto`) are implemented in **Python**; RPCs in any other
  service are implemented in **Rust**. (`docs/language_bridge.md`.)

### Top-Level Directory Map

| Path                         | Purpose                                                                    |
| ---------------------------- | -------------------------------------------------------------------------- |
| `rslib/`                     | Rust core library (scheduling, search, media, sync, etc.)                  |
| `pylib/`                     | Python library (`pylib/anki/`) + Rust bridge (`pylib/rsbridge/`)           |
| `qt/`                        | PyQt desktop GUI (`qt/aqt/`), installer (`qt/installer/`), tests           |
| `ts/`                        | Svelte/TypeScript web frontend, libraries, e2e tests                       |
| `proto/`                     | Protobuf schema (`proto/anki/*.proto`) — the cross-language IPC contract   |
| `ftl/`                       | Fluent translation files (`ftl/core/`, `ftl/qt/`)                          |
| `build/`                     | Custom build system (`build/configure`, `build/ninja_gen`, `build/runner`) |
| `tools/`                     | Helper scripts wrapped by `just` recipes (don't call directly)             |
| `docs/`                      | Project documentation (Sphinx site source)                                 |
| `docs-site/`                 | Additional docs-site assets                                                |
| `python/`                    | Python tooling / requirements support                                      |
| `cargo/`, `.cargo/`          | Cargo/vendoring configuration                                              |
| `out/`                       | **Generated build output — do not hand-edit** (gitignored)                 |
| `justfile`                   | Canonical command recipes (entry point for all tasks)                      |
| `release.just`               | Release-specific recipes (`just release ...`)                              |
| `Cargo.toml` / `Cargo.lock`  | Rust workspace manifest                                                    |
| `pyproject.toml` / `uv.lock` | Python project config (managed with `uv`)                                  |
| `package.json` / `yarn.lock` | JS deps (managed with `yarn`)                                              |

---

## 3. Tech Stack & Languages

| Concern            | Tooling                                                                | Where it lives                                                                                                    |
| ------------------ | ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Rust core          | Rust (workspace), `prost` for protobuf, `snafu` for errors             | `rslib/`, `Cargo.toml`, `rust-toolchain.toml`                                                                     |
| Python lib/GUI     | Python 3.9+ (`.python-version`), `uv` env in `out/pyenv`               | `pylib/`, `qt/`, `pyproject.toml`, `uv.lock`                                                                      |
| Rust↔Python bridge | PyO3                                                                   | `pylib/rsbridge/`                                                                                                 |
| Web frontend       | Svelte + TypeScript, SvelteKit routes, Vite, SCSS/Sass                 | `ts/`, `package.json`, `playwright.config.ts`                                                                     |
| Protobuf           | proto3; `prost` (Rust), official Python impl, `protobuf-es` (TS)       | `proto/anki/`                                                                                                     |
| i18n               | Fluent (`.ftl`)                                                        | `ftl/core/`, `ftl/qt/`                                                                                            |
| Build system       | custom Rust build graph → Ninja/N2                                     | `build/`, `./ninja` (wrapped by `just`)                                                                           |
| Linters/formatters | clippy, mypy, ruff, eslint, svelte-check, tsc, dprint                  | configs at repo root (`.ruff.toml`, `.mypy.ini`, `.eslintrc.cjs`, `.dprint.json`, `.rustfmt.toml`, `.prettierrc`) |
| Tests              | `cargo nextest` (Rust), `pytest` (Py), `vitest` (TS), Playwright (e2e) | see §9                                                                                                            |

Package managers are vendored/wrapped: `yarn` resolves to
`out/extracted/node/bin/yarn` and `uv` to `out/extracted/uv/uv` (see the
variable block at the bottom of `justfile`).

---

## 4. Build, Run, Test, Lint

All canonical commands are `just` recipes (verified against `justfile`).

### Build & Run

| Task                | Command              | Notes                                                                       |
| ------------------- | -------------------- | --------------------------------------------------------------------------- |
| Build pylib + qt    | `just build`         | Wraps `ninja pylib qt`                                                      |
| Run (dev)           | `just run`           | Launches Anki; web at `http://localhost:40000/_anki/pages/`; sets `ANKIDEV` |
| Run (optimized)     | `just run-optimized` | Release build, slower to compile                                            |
| Build wheels        | `just wheels`        | Needed for some platforms / add-on dev                                      |
| Web live-reload     | `just web-watch`     | Watches `ts/`, `sass/`, `qt/aqt/data/web/`; run in a separate terminal      |
| One-off web rebuild | `just rebuild-web`   | Reloads web stack without restarting Anki                                   |

### Full Check (run before completing a task)

```bash
just check     # ninja pylib qt check — format + build + lint + all tests
```

### Lint / Format

| Task                                                       | Command                                 |
| ---------------------------------------------------------- | --------------------------------------- |
| Lint + typecheck (clippy, mypy, ruff, eslint, svelte, tsc) | `just lint` (requires build outputs)    |
| Auto-fix lint (ruff + eslint)                              | `just fix-lint`                         |
| Check formatting (fast, no build)                          | `just fmt`                              |
| Apply formatting                                           | `just fix-fmt`                          |
| Minilints (copyright/contributors/licenses)                | `just minilints` / `just fix-minilints` |

### Tests

| Task                       | Command                                  |
| -------------------------- | ---------------------------------------- |
| All tests                  | `just test` (add `--coverage`, `--html`) |
| Rust only                  | `just test-rust`                         |
| Python only (pylib + qt)   | `just test-py`                           |
| TypeScript/Svelte (Vitest) | `just test-ts`                           |
| Browser e2e (Playwright)   | `just test-e2e` (add `--ui`)             |

### Quick Per-Language Iteration

| Language  | Command              | Notes                                          |
| --------- | -------------------- | ---------------------------------------------- |
| Rust      | `cargo check`        | Fast type check without full build             |
| Rust      | `cargo clippy --fix` | Auto-fix clippy lints                          |
| Python    | `just lint`          | Runs mypy/ruff; `just wheels` if wheel-related |
| TS/Svelte | `just lint`          | Includes `check:svelte` + `check:typescript`   |

> **Full-build trigger:** Changes to `.proto` files (and other codegen inputs)
> require a full build — run `just check` (or `just build`) so the generated
> bindings in `out/` are regenerated. (CLAUDE.md, §5 below.)

---

## 5. Cross-Language / Codegen Boundaries

The `.proto` files in `proto/anki/` are the **single source of truth** for the
cross-language API and some on-disk storage formats. The build system generates
language bindings from them; **the generated output is never hand-edited.**

| Target     | Generated artifact (in `out/`)                                                                 | Consumed as                                  |
| ---------- | ---------------------------------------------------------------------------------------------- | -------------------------------------------- |
| Python     | `out/pylib/anki/*_pb2.py`, `out/pylib/anki/_backend_generated.py`, `out/pylib/anki/_fluent.py` | `col._backend.*` (snake_case RPCs)           |
| TypeScript | `out/ts/lib/generated/` (`backend.ts`, `anki/*_pb`, `ftl.ts`)                                  | `@generated/backend`, `@generated/anki/*_pb` |
| Rust       | generated `pb` module (browse via `cargo doc --open --document-private-items` from `rslib/`)   | `anki_proto::<area>::*` types                |

Notes verified from `docs/protobuf.md` and `docs/language_bridge.md`:

- **Naming across languages:** field `foo_bar` → `fooBar` in TS; message
  `FooBar` namespace is `foo_bar` in Rust; RPC `NewDeck` → `new_deck` in
  Python/Rust impls.
- **Optionals:** unset optional values are the type default (`0`, `""`) not
  `None`/`null`/`undefined`. Use `HasField()`/`WhichOneof()` in Python; prefer
  avoiding default-as-sentinel in TS.
- **Storage-format messages** (e.g. `Deck`) are stored in the DB; incompatible
  changes must go through a schema upgrade.
- The committed `ts/lib/generated/` folder contains hand-written files
  (`post.ts`, `ftl-helpers.ts`) that get **combined** with the generated
  `out/ts/lib/generated/` at build time (see `ts/lib/generated/README.md`).

### i18n (Fluent)

- Translatable strings live in `ftl/core/*.ftl` and `ftl/qt/*.ftl`.
- Scripts in `rslib/i18n` auto-generate type-safe translation APIs for Rust, TS,
  and Python. (CLAUDE.md.)
- **Prefer `ftl/core`** for non-Qt-specific strings; use `ftl/qt` only for
  Qt-interface-specific text. Confirm the right `.ftl` file and match existing
  style before adding strings.
- Sync/deprecate translation files with `just ftl-sync` / `just ftl-deprecate`.

---

## 6. Key Directories Deep-Dive

### `rslib/src/` — Rust core

Organized by domain. Representative subdirs/files (verified):

- Domain modules: `decks/`, `card/`, `notes/`, `notetype/`, `scheduler/`,
  `search/`, `media/`, `import_export/`, `stats/`, `sync/`, `config/`,
  `deckconfig/`, `revlog/`, `collection/`, `storage/`, `undo/`.
- Each service domain typically has a `service.rs` implementing the protobuf
  RPCs for that area (e.g. `rslib/src/decks/service.rs` implements
  `DecksService::new_deck`). `rslib/src/services.rs` wires services together.
- `rslib/src/error/` — error types (`mod.rs` defines `AnkiError` + `Result`).
- `rslib/io` and `rslib/process` — file/process helpers (**prefer these** over
  std equivalents for better error context; CLAUDE.md).
- `rslib/i18n/` — translation API generation.

### `pylib/anki/` — Python library

- `_backend.py` — public-ish wrapper around generated RPCs; pulls in
  `out/pylib/anki/_backend_generated.py`.
- Domain modules mirror Rust: `decks.py`, `cards.py`, `notes.py`, `models.py`,
  `collection.py`, `scheduler/`, `importing/`, etc.
- `errors.py`, `hooks.py`, `_legacy.py` (back-compat shims for add-ons).
- **Add-on stability matters:** prefer exposing `col.<area>.<helper>()` rather
  than leaking `_backend`/protobuf objects directly. (`docs/language_bridge.md`.)

### `qt/aqt/` — PyQt GUI

- Window/dialog modules: `main.py`, `browser/`, `editor.py`, `reviewer.py`,
  `deckbrowser.py`, `deckoptions.py`, `addcards.py`, `preferences.py`, etc.
- `mediasrv.py` — local HTTP server that serves web pages and handles
  `/_anki/...` protobuf POST requests (key Python↔TS↔Rust junction).
- `gui_hooks.py` — the add-on hook system.
- `data/web/` — web assets; new web code lives in `ts/` and is copied here at
  build time. (`docs/architecture.md`.)
- `operations/`, `forms/` — Qt operations and generated form modules.

### `ts/` — Svelte/TypeScript frontend

- `routes/` — SvelteKit pages, e.g. `deck-options/`, `graphs/`, `card-info/`,
  `congrats/`, `change-notetype/`, `image-occlusion/`, `import-csv/`,
  `import-anki-package/`, `import-page/`.
- `lib/` — shared code: `components/`, `sveltelib/`, `domlib/`, `tslib/`,
  `sass/`, `tag-editor/`, and `generated/` (hand-written + generated combo).
- `editor/`, `reviewer/`, `editable/`, `html-filter/`, `mathjax/` — feature areas.
- `tests/e2e/` — Playwright e2e tests (see §9).
- Build entry/config: `vite.config.ts`, `svelte.config.js`, `bundle_*.mjs`,
  `tsconfig*.json`.

### `proto/anki/` — Protobuf schema

- One file per domain (`decks.proto`, `cards.proto`, `scheduler.proto`, …).
- `frontend.proto` — `FrontendService` RPCs are implemented in **Python**; all
  others in **Rust**.
- `backend.proto`, `generic.proto`, `collection.proto` — core/shared messages.

---

## 7. Conventions & Gotchas

Honor these (from CLAUDE.md / `AGENTS.md` — note: `AGENTS.md` is a symlink to
`CLAUDE.md`):

- **Use `just`, not raw scripts.** Do not invoke `./ninja`, `./run`, or `tools/`
  scripts directly; use the `just` recipes. (The `docs/` and Ninja notes mention
  raw `./ninja`/`./run` for historical/manual context, but the project's stated
  preference is `just`.)
- **Rust error handling:**
  - In `rslib/`, use `error/mod.rs`'s `AnkiError` / `Result` and `snafu`.
  - In other Rust modules (build scripts, bridge, tools), prefer `anyhow` with
    added context. Unwrapping in build scripts/tests is acceptable.
- **Rust dependencies:** add to the **root workspace** `Cargo.toml`, then use
  `dep.workspace = true` in the individual crate.
- **Rust utilities:** prefer `rslib/io` and `rslib/process` helpers over raw std
  for file/process ops (better error messages).
- **Don't grep to verify fixes — re-run checks.** When fixing build/test errors,
  re-run `just check` or a quick-iteration command rather than grepping for other
  instances. (CLAUDE.md.)
- **Generated code is not hand-edited** — see §5 and §11.
- **Protobuf naming & optionals** — see §5 (case conversion; defaults vs null).
- **i18n placement** — prefer `ftl/core` over `ftl/qt`; match existing style.
- **Untracked dev files:** put personal/untracked files in an `extra/` folder so
  formatters and checks ignore them. (`docs/development.md`.)
- **Env vars for debugging** (set before launching): `ANKIDEV` (extra logs,
  disables auto-backup; auto-set by `just run`), `TRACESQL`, `LOGTERM`,
  `ANKI_PROFILE_CODE`. (`docs/development.md`.)
- **Formatting/style** is enforced by configs at the repo root; let `just fmt` /
  `just fix-fmt` handle it rather than formatting by hand.

---

## 8. "Where Do I Make This Change?" Playbook

| Task                                  | Where / Steps                                                                                                                                                                                                                                                                                                                                                                           |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Add a new backend RPC (Rust impl)** | 1) Declare `rpc` + messages in the relevant `proto/anki/<area>.proto` (a non-`FrontendService` service). 2) Implement in `rslib/src/<area>/service.rs` (snake_case method on the service). 3) Run a full build (`just check`) to regenerate bindings. 4) Call from Python via `col._backend.<rpc>()` (wrap in a `pylib/anki/<area>.py` helper) and/or from TS via `@generated/backend`. |
| **Add an RPC implemented in Python**  | Declare it in `FrontendService` in `proto/anki/frontend.proto`, then implement the handler in `qt/aqt/` (e.g. `mediasrv.py`-routed handler). (`docs/language_bridge.md`.)                                                                                                                                                                                                               |
| **Add / change a frontend web page**  | Add a route under `ts/routes/<page>/` (SvelteKit). Shared UI goes in `ts/lib/components/`. Use `just web-watch` for live reload.                                                                                                                                                                                                                                                        |
| **Add a translatable string**         | Add to the correct file in `ftl/core/` (preferred) or `ftl/qt/` (Qt-specific). Build to regenerate the typed API; use it from Rust/Python/TS. Run `just ftl-sync` if syncing.                                                                                                                                                                                                           |
| **Fix a Rust core bug**               | Edit `rslib/src/<area>/`. Iterate with `cargo check` / `just test-rust`. Use `AnkiError`/`snafu` for errors.                                                                                                                                                                                                                                                                            |
| **Change Python library behavior**    | Edit `pylib/anki/<area>.py`; keep add-on-facing API stable. Iterate with `just test-py`.                                                                                                                                                                                                                                                                                                |
| **Change desktop GUI behavior**       | Edit `qt/aqt/` modules; add-on hooks live in `qt/aqt/gui_hooks.py`.                                                                                                                                                                                                                                                                                                                     |
| **Change the web↔backend HTTP layer** | `qt/aqt/mediasrv.py` (serves pages, handles `/_anki/` protobuf POSTs).                                                                                                                                                                                                                                                                                                                  |
| **Add a Rust dependency**             | Add to root `Cargo.toml`, reference with `dep.workspace = true` in the crate.                                                                                                                                                                                                                                                                                                           |
| **Modify the build graph**            | `build/configure` (define actions/inputs/outputs); helpers in `build/ninja_gen`. (`docs/build.md`.)                                                                                                                                                                                                                                                                                     |
| **Modify the installer**              | `qt/installer/` (per-platform templates `mac-template/`, `linux-template/`, `windows-template/`).                                                                                                                                                                                                                                                                                       |

---

## 9. Testing & Verification

| Layer       | Runner                                              | Command          | Location                  |
| ----------- | --------------------------------------------------- | ---------------- | ------------------------- |
| Rust        | `cargo nextest` (via `cargo-llvm-cov` for coverage) | `just test-rust` | `rslib/`, crate `tests/`  |
| Python      | `pytest` (split: pylib + qt)                        | `just test-py`   | `pylib/tests`, `qt/tests` |
| TS/Svelte   | `vitest`                                            | `just test-ts`   | `ts/`                     |
| Browser e2e | Playwright (Chromium)                               | `just test-e2e`  | `ts/tests/e2e/`           |

- **Coverage:** add `--coverage` (and `--html`) to any `just test*` recipe.
  Thresholds and tooling are documented in `docs/testing-coverage.md`
  (HTML reports → `out/coverage/`, gitignored).
- **E2E specifics** (`docs/e2e-testing.md`): build once with `just build` first;
  Chromium installs into `out/playwright-browsers/` on first run. Tests use the
  `.test.ts` suffix and import from `./fixtures`. For fast iteration, run `./run`
  in one terminal and `ANKI_E2E_REUSE_SERVER=1 just test-e2e` in another.
  Anki's `/_anki/` endpoints use protobuf binary payloads — decode with the
  matching generated type from `ts/lib/generated/`.
- **Pre-completion gate:** run **`just check`** as the final step before marking
  a task complete (format + build + lint + all tests). (CLAUDE.md.)
- CI runs the e2e suite in the `check-linux` job (`.github/workflows/ci.yml`),
  uploading screenshots/traces from failures for 7 days.

---

## 10. Existing Documentation Index

Drill into the repo's own docs (all under `docs/`):

| Doc                                                                                   | Covers                                                              |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `docs/index.md`                                                                       | Sphinx docs entry / toctree                                         |
| `docs/architecture.md`                                                                | High-level backend/GUI split, protobuf overview                     |
| `docs/language_bridge.md`                                                             | How RPCs are declared/called/implemented across Rust↔Python↔TS      |
| `docs/protobuf.md`                                                                    | Protobuf conventions, optionals/oneofs, per-language notes          |
| `docs/build.md`                                                                       | Build-system internals (`build/configure`, `ninja_gen`, `runner`)   |
| `docs/development.md`                                                                 | Building from source, running, env vars, IDE pointers, installer    |
| `docs/editing.md`                                                                     | VS Code / PyCharm setup (`.vscode.dist`, `.idea.dist`, `out/pyenv`) |
| `docs/testing-coverage.md`                                                            | Test runners, coverage tools, thresholds                            |
| `docs/e2e-testing.md`                                                                 | Playwright e2e workflow and page families                           |
| `docs/ninja.md`                                                                       | Notes on the Ninja build (historical/manual usage)                  |
| `docs/linux.md` / `docs/mac.md` / `docs/windows.md`                                   | Platform-specific build requirements                                |
| `docs/releasing.md`                                                                   | Release process                                                     |
| `docs/contributing.md`                                                                | Contribution guidelines                                             |
| `docs/api-python.md` / `docs/api-rust.md` / `docs/api-*-modules.md`                   | Auto-generated API reference entry points                           |
| `docs/syncserver/`                                                                    | Sync server docs                                                    |
| `docs/docker/`                                                                        | Docker-related docs                                                 |
| `proto/README.md`                                                                     | What the protobuf layer is for                                      |
| `rslib/README.md`, `pylib/README.md`, `qt/README.md`, `ts/README.md`, `ftl/README.md` | Per-layer readmes                                                   |

Build the docs site locally with `just docs` (or `just docs-serve`); Rust API
docs with `just docs-rust`.

---

## 11. Pitfalls / Do-Not-Touch

- **`out/` is generated and gitignored — never hand-edit it.** It holds
  generated protobuf bindings, the Python venv (`out/pyenv`), node modules
  (`node_modules` → `out/node_modules`), build output, and coverage reports.
  Edit the _sources_ (`proto/`, `ftl/`, layer source dirs), then rebuild. You may
  _read_ `out/{pylib/anki, qt/_aqt, ts/lib/generated}` to understand
  cross-language glue. (CLAUDE.md.)
- **`.proto` edits need a full build** before Rust/Python/TS see them — run
  `just check` / `just build`, don't just `cargo check`.
- **Don't bypass the helper layer:** prefer `col.<area>.<helper>()` over
  `col._backend.*` so the add-on-facing API stays stable.
- **Protobuf default-value trap:** unset optionals read back as `0`/`""`, not
  null — guard with `HasField()`/`WhichOneof()` (Python) and avoid
  default-as-sentinel (TS). (`docs/protobuf.md`.)
- **Storage-format proto messages** (e.g. `Deck`) require a schema upgrade for
  incompatible changes — don't reshuffle them casually. (`docs/protobuf.md`.)
- **Coverage runs are slow** (Rust rebuilds with instrumentation); use plain
  `just test-rust` for fast iteration. Rust coverage is unsupported on Windows
  ARM64. (`docs/testing-coverage.md`.)
- **First build is slow** (downloads + builds many deps). The build system is
  custom — debug it with the techniques in `docs/build.md`, not by editing
  `out/build.ninja` by hand.
- **Don't call `tools/` scripts or `./ninja`/`./run` directly** — go through
  `just`. (CLAUDE.md.)

---

## 12. Stale / Unresolved References

- **`CLAUDE.md` references `@.claude/user.md`** ("Individual preferences"), but
  **no `.claude/` directory exists** in this checkout. `.gitignore` lists
  `.claude/user.md` and `.claude/settings.local.json`, so this is an _optional,
  gitignored per-user preferences file_ that is simply absent here — there are no
  individual preferences to honor in this clone. Treat the reference as
  intentional-but-empty, not a rule.
- **`CLAUDE.md` says the GUI "embeds the web components in `aqt/`"**, but there is
  **no top-level `aqt/` directory**. The PyQt GUI actually lives at **`qt/aqt/`**
  (confirmed on disk and in `docs/architecture.md`). Use `qt/aqt/`.
- **`docs/ninja.md` / `docs/development.md`** describe raw `./ninja` and `./run`
  usage (e.g. `./ninja check`, `./ninja format`). These still work but conflict
  with CLAUDE.md's directive to use `just`. **Prefer the `just` recipes**
  (`just check`, `just fmt`, etc.); the raw commands are useful only for targeted
  Ninja debugging (e.g. `./ninja check:svelte`).
- **`docs/e2e-testing.md`** notes the add-card editor endpoint
  (`/editor/?mode=add`) is "not yet present in upstream Anki" pending issue #3830
  — verify availability before relying on it.
