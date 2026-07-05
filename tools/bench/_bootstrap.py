# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Make the *built* ``anki`` package importable from a standalone tool.

Mirrors ``tools/run.py``: the runnable package is the source ``pylib/anki``
overlaid with the generated files + compiled Rust bridge that the build writes
into ``out/pylib``. Third-party deps (protobuf, orjson, …) come from the built
``out/pyenv`` — so these scripts must be launched with that interpreter (the
``just bench`` recipe does this via ``uv run``; see ``docs/speedrun/benchmark.md``).

Importing this module has the side effect of extending ``sys.path`` and
returning the repo root. It raises a clear, actionable error if the backend has
not been built yet (e.g. on a fresh worktree where ``out/`` is absent).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def ensure_anki_importable() -> Path:
    """Put ``pylib`` and ``out/pylib`` on ``sys.path`` and sanity-check the build.

    Returns the repo root. Raises ``SystemExit`` with guidance if the compiled
    backend is missing, so a run on an unbuilt checkout fails loudly instead of
    with a bare ``ImportError``.
    """
    for rel in ("pylib", "out/pylib", "out/qt"):
        path = REPO_ROOT / rel
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    built = REPO_ROOT / "out" / "pylib" / "anki"
    if not built.exists():
        raise SystemExit(
            "Anki's compiled backend was not found at out/pylib/anki.\n"
            "The benchmark needs the built pylib + out/pyenv. Build it first on a\n"
            "branch that has the toolchain (e.g. speedrun-sunday):\n\n"
            "    just bench            # builds pylib, then runs the benchmark\n\n"
            "or, to build only:  ./ninja pylib\n"
        )
    return REPO_ROOT
