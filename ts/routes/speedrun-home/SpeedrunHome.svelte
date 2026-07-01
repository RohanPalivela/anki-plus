<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { MemoryScoreResponse } from "@generated/anki/speedrun_pb";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";

    import "./speedrun-home.scss";

    export let score: MemoryScoreResponse;

    function pct(value: number): string {
        return `${Math.round(value * 100)}%`;
    }

    // While abstaining (too little data / coverage), the derived percentages are
    // unreliable, so show placeholders instead of misleading numbers. The raw
    // graded-card count stays, as honest progress toward a real score.
    $: showScore = !score.abstained;

    function start(): void {
        bridgeCommand("start");
    }
    function openDashboard(): void {
        bridgeCommand("dashboard");
    }
    function openDecks(): void {
        bridgeCommand("decks");
    }
</script>

<div class="speedrun-home">
    <header class="brand">
        <div class="logo" aria-hidden="true">A+</div>
        <div class="brand-text">
            <h1>{tr.speedrunHomeTitle()}</h1>
            <p class="tagline">{tr.speedrunHomeTagline()}</p>
        </div>
    </header>

    <section class="memory-snapshot" class:muted={score.abstained}>
        <div class="snapshot-head">
            <span class="label">{tr.speedrunDashboardMemory()}</span>
            {#if score.abstained}
                <span class="abstain" role="status">
                    {tr.speedrunDashboardAbstaining()}
                </span>
            {/if}
        </div>
        <div class="score-line">
            <span class="big-number">{showScore ? pct(score.overall) : "—"}</span>
            <span class="big-label">{tr.speedrunDashboardOverall()}</span>
        </div>
        <div class="mini-stats">
            <div class="mini-stat">
                <span class="mini-label">{tr.speedrunDashboardRange()}</span>
                <span class="mini-value">
                    {showScore
                        ? `${pct(score.rangeLow)} – ${pct(score.rangeHigh)}`
                        : "—"}
                </span>
            </div>
            <div class="mini-stat">
                <span class="mini-label">{tr.speedrunDashboardCoverage()}</span>
                <span class="mini-value">{showScore ? pct(score.coverage) : "—"}</span>
            </div>
            <div class="mini-stat">
                <span class="mini-label">{tr.speedrunDashboardGraded()}</span>
                <span class="mini-value">{score.gradedCount}</span>
            </div>
        </div>
    </section>

    <button class="start" on:click={start}>
        <span class="start-label">{tr.speedrunHomeStart()}</span>
        <span class="start-sub">{tr.speedrunHomeStartHint()}</span>
    </button>

    <nav class="links">
        <button class="link" on:click={openDashboard}>
            {tr.speedrunHomeOpenDashboard()}
        </button>
        <span class="dot" aria-hidden="true">·</span>
        <button class="link" on:click={openDecks}>
            {tr.speedrunHomeOpenDecks()}
        </button>
    </nav>
</div>
