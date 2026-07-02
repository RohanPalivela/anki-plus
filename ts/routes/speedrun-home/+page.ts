// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import { getMemoryScore } from "@generated/backend";

import { type Curriculum, getCurriculum } from "./curriculum";
import type { PageLoad } from "./$types";

export const load = (async () => {
    // Curriculum is fork-specific and served as JSON; tolerate its absence
    // (e.g. an older host) so the home still renders its Memory snapshot.
    const [score, curriculum] = await Promise.all([
        getMemoryScore({}),
        getCurriculum().catch((): Curriculum | null => null),
    ]);
    return { score, curriculum };
}) satisfies PageLoad;
