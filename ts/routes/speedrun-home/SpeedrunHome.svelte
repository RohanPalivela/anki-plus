<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type {
        MemoryScoreResponse,
        PerformanceScoreResponse,
        ReadinessScoreResponse,
    } from "@generated/anki/speedrun_pb";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";

    import type { ConceptProgress, Curriculum, TopicProgress } from "./curriculum";
    import { setSessionScope } from "./curriculum";

    import "./speedrun-home.scss";

    export let score: MemoryScoreResponse;
    export let performance: PerformanceScoreResponse | null = null;
    export let readiness: ReadinessScoreResponse | null = null;
    export let curriculum: Curriculum | null = null;

    function pct(value: number): string {
        return `${Math.round(value * 100)}%`;
    }

    function scaled(value: number): string {
        return `${Math.round(value)}`;
    }

    // While abstaining (too little data / coverage), the derived percentages are
    // unreliable, so show placeholders instead of misleading numbers. The raw
    // graded-card count stays, as honest progress toward a real score.
    $: showScore = !score.abstained;
    $: showPerf = performance != null && !performance.abstained;
    $: showReadiness = readiness != null && !readiness.abstained;
    $: topics = curriculum?.topics ?? [];
    $: hasCurriculum = topics.length > 0;

    // The set of topics whose concept list is expanded (collapsed by default so
    // the overview stays scannable). Keyed by topic slug.
    let expanded: Record<string, boolean> = {};
    function toggle(topic: string): void {
        expanded = { ...expanded, [topic]: !expanded[topic] };
    }

    // Every Start writes the scope first (null clears it), then hands off to the
    // native guided session, so desktop and Android use one identical path.
    async function startScoped(
        topic: string | null,
        concept: string | null,
    ): Promise<void> {
        await setSessionScope(topic, concept);
        bridgeCommand("start");
    }
    function start(): void {
        startScoped(null, null);
    }
    function openDashboard(): void {
        bridgeCommand("dashboard");
    }
    function openDecks(): void {
        bridgeCommand("decks");
    }

    function conceptState(c: ConceptProgress): string {
        if (!c.practiced) {
            return tr.speedrunCurriculumNotStarted();
        }
        return tr.speedrunCurriculumAccuracy({ percent: Math.round(c.accuracy * 100) });
    }

    function masteryLabel(t: TopicProgress): string {
        return t.masteryKnown ? pct(t.mastery) : tr.speedrunCurriculumMasteryUnknown();
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

    <div class="score-row">
        {#if performance}
            <section class="snapshot-card" class:muted={performance.abstained}>
                <div class="snapshot-head">
                    <span class="label">{tr.speedrunDashboardPerformance()}</span>
                    {#if performance.synthetic}
                        <span class="synthetic" title={tr.speedrunSyntheticHint()}>
                            {tr.speedrunSyntheticBadge()}
                        </span>
                    {/if}
                </div>
                <div class="score-line">
                    <span class="big-number">
                        {showPerf ? pct(performance.overall) : "—"}
                    </span>
                    <span class="big-label">
                        {tr.speedrunDashboardPerformanceOverall()}
                    </span>
                </div>
                <div class="mini-value range">
                    {showPerf
                        ? `${pct(performance.rangeLow)} – ${pct(performance.rangeHigh)}`
                        : tr.speedrunDashboardAbstaining()}
                </div>
            </section>
        {/if}

        {#if readiness}
            <section class="snapshot-card" class:muted={readiness.abstained}>
                <div class="snapshot-head">
                    <span class="label">{tr.speedrunDashboardReadiness()}</span>
                    {#if readiness.synthetic}
                        <span class="synthetic" title={tr.speedrunSyntheticHint()}>
                            {tr.speedrunSyntheticBadge()}
                        </span>
                    {/if}
                </div>
                <div class="score-line">
                    <span class="big-number">
                        {showReadiness ? scaled(readiness.scaledMedian) : "—"}
                    </span>
                    <span class="big-label">
                        {tr.speedrunDashboardReadinessHeadline()}
                    </span>
                </div>
                <div class="mini-value range">
                    {showReadiness
                        ? `${scaled(readiness.scaledLow)} – ${scaled(readiness.scaledHigh)}`
                        : tr.speedrunDashboardAbstaining()}
                </div>
            </section>
        {/if}
    </div>

    <button class="start" on:click={start}>
        <span class="start-label">{tr.speedrunHomeStart()}</span>
        <span class="start-sub">{tr.speedrunHomeStartHint()}</span>
    </button>

    <section class="curriculum">
        <div class="curriculum-head">
            <h2>{tr.speedrunHomeCurriculumTitle()}</h2>
            <p class="hint">{tr.speedrunHomeCurriculumHint()}</p>
        </div>

        {#if !hasCurriculum}
            <p class="empty">{tr.speedrunHomeCurriculumEmpty()}</p>
        {:else}
            <ul class="topic-list">
                {#each topics as topic (topic.topic)}
                    <li class="topic">
                        <div class="topic-head">
                            <button
                                class="topic-toggle"
                                on:click={() => toggle(topic.topic)}
                                aria-expanded={!!expanded[topic.topic]}
                            >
                                <span
                                    class="chevron"
                                    class:open={expanded[topic.topic]}
                                >
                                    ›
                                </span>
                                <span class="topic-name">{topic.label}</span>
                            </button>
                            <span
                                class="topic-mastery"
                                title={tr.speedrunCurriculumMastery()}
                            >
                                {masteryLabel(topic)}
                            </span>
                            <button
                                class="chip-btn"
                                on:click={() => startScoped(topic.topic, null)}
                            >
                                {tr.speedrunCurriculumStudyTopic()}
                            </button>
                        </div>
                        <div class="topic-bar" aria-hidden="true">
                            <div
                                class="topic-bar-fill"
                                class:unknown={!topic.masteryKnown}
                                style={`width: ${topic.masteryKnown ? Math.round(topic.mastery * 100) : 0}%`}
                            ></div>
                        </div>
                        <div class="topic-meta">
                            {tr.speedrunCurriculumQuestions({
                                count: topic.servedQuestions,
                            })}
                            · {tr.speedrunCurriculumLessons({
                                count: topic.lessonCards,
                            })}
                        </div>

                        {#if expanded[topic.topic]}
                            <ul class="concept-list">
                                {#each topic.concepts as concept (concept.concept)}
                                    <li class="concept">
                                        <div class="concept-info">
                                            <span class="concept-name">
                                                {concept.label}
                                            </span>
                                            <span
                                                class="concept-state"
                                                class:done={concept.practiced}
                                            >
                                                {conceptState(concept)}
                                            </span>
                                        </div>
                                        <div class="concept-meta">
                                            {tr.speedrunCurriculumQuestions({
                                                count: concept.servedQuestions,
                                            })}
                                            {#if concept.lessonCards > 0}
                                                · {tr.speedrunCurriculumLessonsActive({
                                                    activated: String(
                                                        concept.lessonsActivated,
                                                    ),
                                                    total: concept.lessonCards,
                                                })}
                                            {/if}
                                        </div>
                                        <button
                                            class="chip-btn concept-study"
                                            on:click={() =>
                                                startScoped(
                                                    concept.topic,
                                                    concept.concept,
                                                )}
                                        >
                                            {tr.speedrunCurriculumStudy()}
                                        </button>
                                    </li>
                                {/each}
                            </ul>
                        {/if}
                    </li>
                {/each}
            </ul>
        {/if}
    </section>

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
