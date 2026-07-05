// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import type { PerformanceScoreResponse, ReadinessScoreResponse } from "@generated/anki/speedrun_pb";
import { MemoryScoreResponse } from "@generated/anki/speedrun_pb";
import { getMemoryScore, getPerformanceScore, getReadinessScore } from "@generated/backend";

import type { PageLoad } from "./$types";
import { type Curriculum, getCurriculum } from "./curriculum";

export const load = (async () => {
    // Curriculum is fork-specific and served as JSON; tolerate its absence
    // (e.g. an older host) so the home still renders its score snapshots. The
    // three engine scores are shared Rust RPCs (identical on desktop/Android);
    // tolerate the two newer ones being unavailable on an older host.
    //
    // Every fetch is guarded (incl. the Memory score) because this loader runs
    // on each home reload, and the home reloads back-to-back when a guided
    // session returns to it (moveToState("speedrun") + refresh_if_needed) or
    // right after a sync. That second reload aborts the first load's in-flight
    // requests, which surfaces as "TypeError: Failed to fetch". A transient
    // score fetch must degrade to an abstaining snapshot, never replace the
    // whole home with an error alert — so we also pass alertOnError:false to
    // suppress postProto's alert() for this background load.
    const [score, performance, readiness, curriculum] = await Promise.all([
        getMemoryScore({}, { alertOnError: false }).catch(
            () => new MemoryScoreResponse({ abstained: true }),
        ),
        getPerformanceScore({}, { alertOnError: false }).catch(
            (): PerformanceScoreResponse | null => null,
        ),
        getReadinessScore({}, { alertOnError: false }).catch(
            (): ReadinessScoreResponse | null => null,
        ),
        getCurriculum().catch((): Curriculum | null => null),
    ]);
    return { score, performance, readiness, curriculum };
}) satisfies PageLoad;
