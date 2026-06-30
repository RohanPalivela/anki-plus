<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { getMemoryScore } from "@generated/backend";
    import type {
        MemoryScoreResponse,
        TopicMastery,
    } from "@generated/anki/speedrun_pb";
    import * as tr from "@generated/ftl";

    import "./speedrun-dashboard.scss";

    export let score: MemoryScoreResponse;

    function pct(value: number): string {
        return `${Math.round(value * 100)}%`;
    }

    // Red (weak) -> green (strong); kept readable in light and dark themes.
    function masteryColor(value: number): string {
        return `hsl(${Math.round(value * 120)}, 60%, 45%)`;
    }

    async function refresh(): Promise<void> {
        score = await getMemoryScore({});
    }

    $: sortedTopics = [...score.topics].sort(
        (a: TopicMastery, b: TopicMastery) =>
            Number(b.known) - Number(a.known) || b.mastery - a.mastery,
    );
    $: hasData = score.gradedCount > 0;
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
                <div class="big-number">{hasData ? pct(score.overall) : "—"}</div>
                <div class="big-label">{tr.speedrunDashboardOverall()}</div>
            </div>

            <div class="stats">
                <div class="stat">
                    <span class="stat-label">{tr.speedrunDashboardRange()}</span>
                    <span class="stat-value">
                        {hasData
                            ? `${pct(score.rangeLow)} – ${pct(score.rangeHigh)}`
                            : "—"}
                    </span>
                </div>
                <div class="stat">
                    <span class="stat-label">{tr.speedrunDashboardCoverage()}</span>
                    <span class="stat-value">{pct(score.coverage)}</span>
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

        <!-- Performance (M3 placeholder) -->
        <section class="tile placeholder">
            <div class="tile-head">
                <h2>{tr.speedrunDashboardPerformance()}</h2>
                <span class="pill soon">M3</span>
            </div>
            <div class="coming-soon">{tr.speedrunDashboardComingM3()}</div>
        </section>

        <!-- Readiness (M3 placeholder) -->
        <section class="tile placeholder">
            <div class="tile-head">
                <h2>{tr.speedrunDashboardReadiness()}</h2>
                <span class="pill soon">M3</span>
            </div>
            <div class="coming-soon">{tr.speedrunDashboardComingM3()}</div>
        </section>
    </div>
</div>
