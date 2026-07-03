// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import { getMemoryScore, getPerformanceScore, getReadinessScore } from "@generated/backend";

import type { PageLoad } from "./$types";

export const load = (async () => {
    // All three engine scores are shared Rust RPCs, so this fetch is identical
    // on desktop and Android.
    const [score, performance, readiness] = await Promise.all([
        getMemoryScore({}),
        getPerformanceScore({}),
        getReadinessScore({}),
    ]);
    return { score, performance, readiness };
}) satisfies PageLoad;
