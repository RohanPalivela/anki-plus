<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import {
        getMemoryScore,
        getPerformanceScore,
        getReadinessScore,
    } from "@generated/backend";
    import type {
        MemoryScoreResponse,
        PerformanceScoreResponse,
        ReadinessScoreResponse,
        TopicMastery,
        TopicPerformance,
    } from "@generated/anki/speedrun_pb";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";

    import "./speedrun-dashboard.scss";

    export let score: MemoryScoreResponse;
    export let performance: PerformanceScoreResponse;
    export let readiness: ReadinessScoreResponse;

    function pct(value: number): string {
        return `${Math.round(value * 100)}%`;
    }

    // Round a scaled MCAT score for display.
    function scaled(value: number): string {
        return `${Math.round(value)}`;
    }

    // Red (weak) -> green (strong); kept readable in light and dark themes.
    function masteryColor(value: number): string {
        return `hsl(${Math.round(value * 120)}, 60%, 45%)`;
    }

    async function refresh(): Promise<void> {
        [score, performance, readiness] = await Promise.all([
            getMemoryScore({}),
            getPerformanceScore({}),
            getReadinessScore({}),
        ]);
    }

    function resetProfile(): void {
        // The Qt host confirms (destructive) and performs the reset, then
        // reloads this page with a fresh snapshot.
        bridgeCommand("reset-profile");
    }

    $: sortedTopics = [...score.topics].sort(
        (a: TopicMastery, b: TopicMastery) =>
            Number(b.known) - Number(a.known) || b.mastery - a.mastery,
    );
    // While abstaining the derived percentages are unreliable; show placeholders
    // rather than a misleading score. The graded count and per-topic bars (real
    // data) stay visible.
    $: showScore = !score.abstained;
    $: showPerf = !performance.abstained;
    $: showReadiness = !readiness.abstained;
    $: perfTopics = [...performance.topics].sort(
        (a: TopicPerformance, b: TopicPerformance) =>
            Number(b.known) - Number(a.known) || b.pCorrect - a.pCorrect,
    );
</script>

<div class="speedrun-dashboard">
    <header class="header">
        <div>
            <h1>{tr.speedrunDashboardTitle()}</h1>
            <p class="subtitle">{tr.speedrunDashboardSubtitle()}</p>
        </div>
        <button class="refresh" on:click={refresh}>
            {tr.speedrunDashboardRefresh()}
        </button>
    </header>

    <div class="tiles">
        <!-- Memory (live, M1) -->
        <section class="tile memory">
            <div class="tile-head">
                <h2>{tr.speedrunDashboardMemory()}</h2>
                <span class="pill live">M1</span>
            </div>

            {#if score.abstained}
                <div class="abstain-badge" role="status">
                    {tr.speedrunDashboardAbstaining()}
                </div>
                <p class="abstain-hint">{tr.speedrunDashboardAbstainingHint()}</p>
            {/if}

            <div class="headline" class:muted={score.abstained}>
                <div class="big-number">{showScore ? pct(score.overall) : "—"}</div>
                <div class="big-label">{tr.speedrunDashboardOverall()}</div>
            </div>

            <div class="stats">
                <div class="stat">
                    <span class="stat-label">{tr.speedrunDashboardRange()}</span>
                    <span class="stat-value">
                        {showScore
                            ? `${pct(score.rangeLow)} – ${pct(score.rangeHigh)}`
                            : "—"}
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">{tr.speedrunDashboardCoverage()}</span>
                    <span class="stat-value">
                        {showScore ? pct(score.coverage) : "—"}
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">{tr.speedrunDashboardGraded()}</span>
                    <span class="stat-value">{score.gradedCount}</span>
                </div>
            </div>

            <h3 class="topics-title">{tr.speedrunDashboardPerTopic()}</h3>
            {#if sortedTopics.length === 0}
                <p class="empty">{tr.speedrunDashboardEmpty()}</p>
            {:else}
                <ul class="topics">
                    {#each sortedTopics as topic (topic.topic)}
                        <li class="topic">
                            <div class="topic-row">
                                <span class="topic-name">{topic.topic}</span>
                                <span class="topic-value">
                                    {#if topic.known}
                                        {pct(topic.mastery)}
                                    {:else}
                                        <span class="unknown">
                                            {tr.speedrunDashboardTopicUnknown()}
                                        </span>
                                    {/if}
                                </span>
                            </div>
                            <div class="bar">
                                <div
                                    class="bar-fill"
                                    style:width={topic.known
                                        ? pct(topic.mastery)
                                        : "0%"}
                                    style:background={masteryColor(topic.mastery)}
                                ></div>
                            </div>
                            {#if topic.known}
                                <div class="topic-meta">
                                    {tr.speedrunDashboardTopicCards({
                                        count: topic.cardCount,
                                    })}
                                </div>
                            {/if}
                        </li>
                    {/each}
                </ul>
            {/if}
        </section>

        <!-- Performance (2PL-IRT, live) -->
        <section class="tile performance">
            <div class="tile-head">
                <h2>{tr.speedrunDashboardPerformance()}</h2>
                <span class="pill live">M3</span>
            </div>

            {#if performance.synthetic}
                <div class="synthetic-badge" title={tr.speedrunSyntheticHint()}>
                    {tr.speedrunSyntheticBadge()}
                </div>
            {/if}

            {#if performance.abstained}
                <div class="abstain-badge" role="status">
                    {tr.speedrunDashboardAbstaining()}
                </div>
                <p class="abstain-hint">
                    {tr.speedrunDashboardPerformanceAbstainingHint()}
                </p>
            {/if}

            <div class="headline" class:muted={performance.abstained}>
                <div class="big-number">
                    {showPerf ? pct(performance.overall) : "—"}
                </div>
                <div class="big-label">
                    {tr.speedrunDashboardPerformanceOverall()}
                </div>
            </div>

            <div class="stats">
                <div class="stat">
                    <span class="stat-label">{tr.speedrunDashboardRange()}</span>
                    <span class="stat-value">
                        {showPerf
                            ? `${pct(performance.rangeLow)} – ${pct(performance.rangeHigh)}`
                            : "—"}
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">{tr.speedrunDashboardCoverage()}</span>
                    <span class="stat-value">
                        {showPerf ? pct(performance.coverage) : "—"}
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">{tr.speedrunDashboardGraded()}</span>
                    <span class="stat-value">{performance.gradedCount}</span>
                </div>
                <div class="stat">
                    <span class="stat-label">
                        {tr.speedrunDashboardPerformanceAbility()}
                    </span>
                    <span class="stat-value">
                        {showPerf ? performance.theta.toFixed(2) : "—"}
                    </span>
                </div>
            </div>

            <h3 class="topics-title">
                {tr.speedrunDashboardPerformancePerTopic()}
            </h3>
            {#if perfTopics.length === 0}
                <p class="empty">{tr.speedrunDashboardEmpty()}</p>
            {:else}
                <ul class="topics">
                    {#each perfTopics as topic (topic.topic)}
                        <li class="topic">
                            <div class="topic-row">
                                <span class="topic-name">{topic.topic}</span>
                                <span class="topic-value">
                                    {#if topic.known}
                                        {pct(topic.pCorrect)}
                                    {:else}
                                        <span class="unknown">
                                            {tr.speedrunDashboardTopicUnknown()}
                                        </span>
                                    {/if}
                                </span>
                            </div>
                            <div class="bar">
                                <div
                                    class="bar-fill"
                                    style:width={pct(topic.pCorrect)}
                                    style:background={masteryColor(topic.pCorrect)}
                                ></div>
                            </div>
                            {#if topic.known}
                                <div class="topic-meta">
                                    {tr.speedrunDashboardPerformanceResponses({
                                        count: topic.responseCount,
                                    })}
                                </div>
                            {/if}
                        </li>
                    {/each}
                </ul>
            {/if}
        </section>

        <!-- Readiness (Monte-Carlo projected MCAT score, live) -->
        <section class="tile readiness">
            <div class="tile-head">
                <h2>{tr.speedrunDashboardReadiness()}</h2>
                <span class="pill live">M3</span>
            </div>

            {#if readiness.synthetic}
                <div class="synthetic-badge" title={tr.speedrunSyntheticHint()}>
                    {tr.speedrunSyntheticBadge()}
                </div>
            {/if}

            {#if readiness.abstained}
                <div class="abstain-badge" role="status">
                    {tr.speedrunDashboardAbstaining()}
                </div>
                <p class="abstain-hint">
                    {tr.speedrunDashboardReadinessAbstainingHint()}
                </p>
            {/if}

            <div class="headline" class:muted={readiness.abstained}>
                <div class="big-number">
                    {showReadiness ? scaled(readiness.scaledMedian) : "—"}
                </div>
                <div class="big-label">
                    {tr.speedrunDashboardReadinessHeadline()}
                </div>
            </div>

            <div class="stats">
                <div class="stat">
                    <span class="stat-label">
                        {tr.speedrunDashboardReadinessInterval()}
                    </span>
                    <span class="stat-value">
                        {showReadiness
                            ? `${scaled(readiness.scaledLow)} – ${scaled(readiness.scaledHigh)}`
                            : "—"}
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">{tr.speedrunDashboardCoverage()}</span>
                    <span class="stat-value">
                        {showReadiness ? pct(readiness.coverage) : "—"}
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">
                        {tr.speedrunDashboardReadinessConfidence()}
                    </span>
                    <span class="stat-value">
                        {showReadiness ? pct(readiness.confidence) : "—"}
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">
                        {tr.speedrunDashboardReadinessRaw()}
                    </span>
                    <span class="stat-value">
                        {showReadiness ? pct(readiness.rawMedian) : "—"}
                    </span>
                </div>
            </div>

            {#if readiness.topReasons.length > 0}
                <h3 class="topics-title">
                    {tr.speedrunDashboardReadinessReasons()}
                </h3>
                <ul class="reasons">
                    {#each readiness.topReasons as reason}
                        <li class="reason">{reason}</li>
                    {/each}
                </ul>
            {/if}
        </section>
    </div>

    <footer class="dashboard-footer">
        <button
            class="reset"
            on:click={resetProfile}
            title={tr.speedrunResetProfileConfirm()}
        >
            {tr.speedrunResetProfileAction()}
        </button>
    </footer>
</div>
