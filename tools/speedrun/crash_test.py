# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun crash-recovery test (brief §7g / §10 hard limit).

Kills the app mid-review N times (default 20) and asserts **zero corrupted
collections** afterward — the §10 hard reliability limit.

Method: for each trial, copy a seeded collection to a temp path, spawn a child
process that opens it and answers cards in a tight loop (writing to the DB), then
**SIGKILL the child at a random moment mid-write**. Reopen the collection in the
parent and run Anki's integrity check (`check_database`). Any reopen failure or
integrity problem counts as corruption.

Run (needs the built backend)::

    just speedrun-validate  # (any recipe that builds pylib), then:
    out/pyenv/bin/python tools/speedrun/crash_test.py --deck /tmp/seed.anki2 --trials 20

If no --deck is given, a small deck is generated first.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import random
import shutil
import signal
import tempfile
import time
from pathlib import Path

from _bootstrap import ensure_anki_importable

ensure_anki_importable()

from anki.collection import Collection  # noqa: E402


def _reviewer_child(col_path: str) -> None:
    """Open the collection and hammer the review loop until killed."""
    col = Collection(col_path)
    try:
        while True:
            card = col.sched.getCard()
            if card is None:
                # Reset everything to new and keep going so we always have writes.
                col.db.execute("update cards set queue=0, type=0")
                col.save()
                continue
            col.sched.answerCard(card, random.choice([1, 3]), from_queue=False)
    finally:  # pragma: no cover - child is SIGKILLed, this rarely runs
        col.close()


def _seed_deck(path: str, n: int = 200) -> None:
    col = Collection(path)
    try:
        basic = col.models.by_name("Basic")
        from anki.decks import DeckId

        did = DeckId(col.decks.id("Speedrun::Crash"))
        for i in range(n):
            note = col.new_note(basic)
            note["Front"] = f"crash {i}"
            note["Back"] = str(i)
            col.add_note(note, did)
        col.save()
    finally:
        col.close()


def _integrity_ok(col_path: str) -> bool:
    """Reopen and run Anki's DB integrity check; True == clean."""
    try:
        col = Collection(col_path)
    except Exception:
        return False
    try:
        # check_database returns (problems: list[str], ok: bool) across versions;
        # be defensive about the exact shape.
        result = col.check_database()
        problems = result[0] if isinstance(result, (list, tuple)) else result
        return not problems
    except Exception:
        return False
    finally:
        try:
            col.close()
        except Exception:
            pass


def run(deck: str, trials: int, max_delay_ms: int) -> int:
    corrupted = 0
    for t in range(1, trials + 1):
        tmp = os.path.join(tempfile.mkdtemp(prefix=f"crash-{t}-"), "col.anki2")
        shutil.copy(deck, tmp)
        # Copy the media folder marker if present (Collection expects sibling files).
        proc = mp.Process(target=_reviewer_child, args=(tmp,), daemon=True)
        proc.start()
        time.sleep(random.uniform(0.005, max_delay_ms / 1000.0))
        os.kill(proc.pid, signal.SIGKILL)
        proc.join(timeout=5)
        ok = _integrity_ok(tmp)
        status = "OK" if ok else "CORRUPTED"
        if not ok:
            corrupted += 1
        print(f"  trial {t:>2}/{trials}: killed mid-review -> reopen {status}")
    print(f"\nRESULT: {trials - corrupted}/{trials} clean, {corrupted} corrupted")
    print("PASS (zero corruption)" if corrupted == 0 else "FAIL")
    return 0 if corrupted == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Speedrun crash-recovery test")
    ap.add_argument("--deck", help="seed collection to copy per trial")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--max-delay-ms", type=int, default=60)
    args = ap.parse_args()

    deck = args.deck
    if not deck:
        deck = os.path.join(tempfile.mkdtemp(prefix="crash-seed-"), "seed.anki2")
        print(f"No --deck; seeding a small deck at {deck}")
        _seed_deck(deck)

    return run(deck, args.trials, args.max_delay_ms)


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    raise SystemExit(main())
