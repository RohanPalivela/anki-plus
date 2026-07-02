// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Speedrun (MCAT) curriculum data layer (W4).
//
// The curriculum stats are computed natively (pylib `Speedrun.curriculum` on
// desktop, `Speedrun.curriculum` in Kotlin on Android) from already-synced
// data and served as JSON from `/_anki/speedrunCurriculum`. Both hosts route
// that path to their own handler, so this fetch is identical on desktop and
// Android. We use JSON (not protobuf) so the curriculum layer stays entirely in
// Python/Kotlin without a new engine RPC.

export interface ConceptProgress {
    concept: string;
    label: string;
    topic: string;
    servedQuestions: number;
    lessonCards: number;
    answered: number;
    correct: number;
    lessonsActivated: number;
    lessonsReviewed: number;
    accuracy: number;
    practiced: boolean;
}

export interface TopicProgress {
    topic: string;
    label: string;
    weight: number;
    mastery: number;
    masteryKnown: boolean;
    servedQuestions: number;
    lessonCards: number;
    answered: number;
    correct: number;
    accuracy: number;
    concepts: ConceptProgress[];
}

export interface Curriculum {
    topics: TopicProgress[];
    overallMastery: number;
    masteryAbstained: boolean;
}

// The home renders in a webview with no API bearer token, so these paths are
// whitelisted host-side; the required application/binary content-type mirrors
// the other whitelisted endpoints (getMemoryScore/congratsInfo).
const HEADERS = { "Content-Type": "application/binary" };

export async function getCurriculum(): Promise<Curriculum> {
    const res = await fetch("/_anki/speedrunCurriculum", {
        method: "POST",
        headers: HEADERS,
        body: new Uint8Array(),
    });
    if (!res.ok) {
        throw new Error(`${res.status}: ${await res.text()}`);
    }
    return JSON.parse(await res.text()) as Curriculum;
}

/** Persist the scope for the next guided session; null/null clears it. */
export async function setSessionScope(
    topic: string | null,
    concept: string | null,
): Promise<void> {
    await fetch("/_anki/speedrunSetScope", {
        method: "POST",
        headers: HEADERS,
        body: new TextEncoder().encode(JSON.stringify({ topic, concept })),
    });
}
