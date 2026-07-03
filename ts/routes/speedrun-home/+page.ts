// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import { getMemoryScore, getPerformanceScore, getReadinessScore } from "@generated/backend";
import type {
    PerformanceScoreResponse,
    ReadinessScoreResponse,
} from "@generated/anki/speedrun_pb";

import { type Curriculum, getCurriculum } from "./curriculum";
import type { PageLoad } from "./$types";

export const load = (async () => {
    // Curriculum is fork-specific and served as JSON; tolerate its absence
    // (e.g. an older host) so the home still renders its score snapshots. The
    // three engine scores are shared Rust RPCs (identical on desktop/Android);
    // tolerate the two newer ones being unavailable on an older host.
    const [score, performance, readiness, curriculum] = await Promise.all([
        getMemoryScore({}),
        getPerformanceScore({}).catch((): PerformanceScoreResponse | null => null),
        getReadinessScore({}).catch((): ReadinessScoreResponse | null => null),
        getCurriculum().catch((): Curriculum | null => null),
    ]);
    return { score, performance, readiness, curriculum };
}) satisfies PageLoad;
