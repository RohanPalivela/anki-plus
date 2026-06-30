# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun (MCAT) Tier-2 home screen.

A branded **MCAT Anki-Plus** landing page rendered into the main window's web
view (``mw.web``) as a first-class main-window state (``"speedrun"``). It shows a
live Memory snapshot (via the existing ``get_memory_score`` RPC, fetched by the
Svelte page) and a prominent **Start** button that launches the guided session.
Small links jump to the full Memory dashboard or to standard Anki's deck list,
so the regular Anki home stays reachable.

This screen object mirrors :class:`aqt.deckbrowser.DeckBrowser` so the main
window can treat it like any other state (show / refresh / op_executed).
"""

from __future__ import annotations

from typing import Any

import aqt
import aqt.main
from anki.collection import OpChanges
from aqt.sound import av_player
from aqt.toolbar import BottomBar


class SpeedrunHome:
    """The ``"speedrun"`` main-window state: an MCAT home rendered into mw.web."""

    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        self.mw = mw
        self.web = mw.web
        self.bottom = BottomBar(mw, mw.bottomWeb)
        self._refresh_needed = False

    def show(self) -> None:
        av_player.stop_and_clear_queue()
        self.web.set_bridge_command(self._link_handler, self)
        # Keep the top toolbar in sync (theme, sync status, Home/Decks links).
        self.mw.toolbar.redraw()
        self.web.load_sveltekit_page("speedrun-home")
        # Clear any bottom-bar buttons left over from a previous state (the home
        # keeps its actions in the page itself).
        self.bottom.draw(buf="")
        self._refresh_needed = False

    def refresh(self) -> None:
        # Reloading the SvelteKit page re-runs its loader, re-fetching the
        # Memory snapshot — this is how the home reflects post-session state.
        self.web.load_sveltekit_page("speedrun-home")
        self._refresh_needed = False

    def refresh_if_needed(self) -> None:
        if self._refresh_needed:
            self.refresh()

    def op_executed(
        self, changes: OpChanges, handler: object | None, focused: bool
    ) -> bool:
        # Reviews / activation change FSRS state, which the Memory snapshot reads.
        if changes.study_queues and handler is not self:
            self._refresh_needed = True
        if focused:
            self.refresh_if_needed()
        return self._refresh_needed

    # Bridge commands from the Svelte home page.
    ##########################################################################

    def _link_handler(self, url: str) -> Any:
        if url == "start":
            from aqt.speedrun.session import start_session

            start_session(self.mw)
        elif url == "dashboard":
            from aqt.speedrun import MemoryDashboardDialog

            MemoryDashboardDialog(self.mw)
        elif url == "decks":
            self.mw.moveToState("deckBrowser")
        return False
