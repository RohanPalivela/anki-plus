// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import {
    MemoryScoreResponse,
    PerformanceScoreResponse,
    ReadinessScoreResponse,
} from "@generated/anki/speedrun_pb";
import { getMemoryScore, getPerformanceScore, getReadinessScore } from "@generated/backend";

import type { PageLoad } from "./$types";

export const load = (async () => {
    // All three engine scores are shared Rust RPCs, so this fetch is identical
    // on desktop and Android. The dashboard auto-reloads on every review /
    // activation (operation_did_execute), and a reload aborts the previous
    // load's in-flight requests — surfacing as "TypeError: Failed to fetch".
    // Guard each fetch so a transient failure degrades to an abstaining tile
    // instead of replacing the dashboard with an error alert; alertOnError is
    // off because this is a background load, not a user action.
    const [score, performance, readiness] = await Promise.all([
        getMemoryScore({}, { alertOnError: false }).catch(
            () => new MemoryScoreResponse({ abstained: true }),
        ),
        getPerformanceScore({}, { alertOnError: false }).catch(
            () => new PerformanceScoreResponse({ abstained: true }),
        ),
        getReadinessScore({}, { alertOnError: false }).catch(
            () => new ReadinessScoreResponse({ abstained: true }),
        ),
    ]);
    return { score, performance, readiness };
}) satisfies PageLoad;
