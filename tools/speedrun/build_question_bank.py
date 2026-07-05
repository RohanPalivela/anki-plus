# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Dev-time generator for the Speedrun (MCAT) question bank.

This script fetches practice questions from *legally reusable* upstream sources,
normalizes them into the ``SpeedrunQuestion`` shape, and writes a single
vendored JSON file that :mod:`anki.speedrun` imports once into a collection as
native Anki notes (which then sync to every device for free).

It is a **build/authoring tool**, not shipped runtime code, and is only run by a
maintainer when refreshing the bank. Network access is required.

Sources (see the repository README for full attribution):

* **OpenMCAT** — https://github.com/Zushah/OpenMCAT — AGPL-3.0. MCAT-specific
  C/P, B/B and P/S banks with explanations. Content is *AI-generated*; the bank
  records this so the M2 eval harness can gate it before it reaches a user.
* **MMLU** (Measuring Massive Multitask Language Understanding, Hendrycks et al.
  2021) — https://github.com/hendrycks/test — MIT. We import only the subsets
  that map onto the MCAT blueprint (college/HS science, medicine, psychology,
  sociology). MMLU items carry no explanations.

Deliberately excluded:

* **Jack Westin** — its original passages/questions are copyrighted and its
  support docs forbid reproducing or sharing them on any other platform, so they
  are not redistributable and are never fetched.
* **Khan Academy MCAT** — CC BY-NC-SA; the NonCommercial + ShareAlike terms are
  incompatible with this AGPL app, so it is not bundled.

Usage::

    python tools/speedrun/build_question_bank.py            # full fetch + write
    python tools/speedrun/build_question_bank.py --dry-run  # counts only
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Where the vendored bank is written (next to anki/speedrun.py so it ships in
# the wheel and resolves via Path(__file__).parent at import time). It is
# gzipped so the branch carries a single ~0.7 MB binary blob rather than a
# ~116k-line text diff; the importer decompresses it with the stdlib.
OUTPUT_PATH = (
    Path(__file__).resolve().parents[2]
    / "pylib"
    / "anki"
    / "data"
    / "speedrun_question_bank.json.gz"
)

SCHEMA_VERSION = 1

# Deterministic held-out fraction: 1 in every HELDOUT_MODULUS items is reserved
# as pool::heldout so the split is reproducible and identical across devices.
HELDOUT_MODULUS = 5

_HTTP_HEADERS = {"User-Agent": "anki-speedrun-question-bank-builder"}

# On-disk response cache so rate-limited re-runs are cheap and reproducible.
CACHE_DIR = Path(__file__).resolve().parents[2] / "out" / "speedrun-bank-cache"

# --- OpenMCAT -----------------------------------------------------------------

OPENMCAT_RAW = "https://raw.githubusercontent.com/Zushah/OpenMCAT/main/src/data/bank"
OPENMCAT_SECTIONS = {
    "cp": "Chemical and Physical Foundations",
    "bb": "Biological and Biochemical Foundations",
    "ps": "Psychological, Social, and Biological Foundations of Behavior",
}
OPENMCAT_URL = "https://github.com/Zushah/OpenMCAT"

# Explicit map from OpenMCAT topic id -> Speedrun blueprint topic. Explicit
# rather than heuristic so the mapping is auditable and stable.
OPENMCAT_TOPIC_MAP: dict[str, str] = {
    # C/P
    "cp_work": "physics",
    "cp_electrostatics": "physics",
    "cp_circuit_elements": "physics",
    "cp_geometrical_optics": "physics",
    "cp_gas_phase": "general-chemistry",
    "cp_electrochemistry": "general-chemistry",
    "cp_stoichiometry": "general-chemistry",
    "cp_acid_base_equilibria": "general-chemistry",
    "cp_separations_and_purifications": "organic-chemistry",
    "cp_carboxylic_acids": "organic-chemistry",
    # B/B
    "bb_amino_acids": "biochemistry",
    "bb_protein_structure": "biochemistry",
    "bb_control_of_enzyme_activity": "biochemistry",
    "bb_nucleic_acid_structure_and_function": "biochemistry",
    "bb_principles_of_bioenergetics": "biochemistry",
    "bb_glycolysis_gluconeogenesis_and_the_pentose_phosphate_pathway": "biochemistry",
    "bb_oxidative_phosphorylation": "biochemistry",
    "bb_translation": "biology",
    "bb_transcription": "biology",
    "bb_mendelian_concepts": "biology",
    # P/S
    "ps_sensory_processing": "psychology",
    "ps_memory": "psychology",
    "ps_cognition": "psychology",
    "ps_biological_bases_of_behavior": "psychology",
    "ps_associative_learning": "psychology",
    "ps_stress": "psychology",
    "ps_formation_of_identity": "psychology",
    "ps_psychological_disorders": "psychology",
    "ps_social_class": "sociology",
    "ps_theoretical_approaches": "sociology",
}
# Fallback when a topic id is not (yet) in the explicit map.
OPENMCAT_SECTION_FALLBACK = {
    "cp": "general-chemistry",
    "bb": "biology",
    "ps": "psychology",
}

# Explicit map from OpenMCAT tested-topic id -> the fine-grained `concept` used
# by the first-principles memory cards (concept:: in speedrun_first_principles
# .json). This is the strongest deterministic linkage signal: OpenMCAT's tested
# topics are essentially the same concepts, so persisting it lets the import-time
# linkage pass make an exact question->first-principles match. Ids without a
# matching first-principles concept are omitted (the item ships with no concept
# and linkage falls back to keyword/topic signals).
OPENMCAT_CONCEPT_MAP: dict[str, str] = {
    "cp_work": "work-energy",
    "cp_electrostatics": "electrostatics",
    "cp_circuit_elements": "circuits",
    "cp_geometrical_optics": "optics",
    "cp_gas_phase": "gas-phase",
    "cp_electrochemistry": "electrochemistry",
    "cp_stoichiometry": "stoichiometry",
    "cp_acid_base_equilibria": "acid-base-equilibria",
    "cp_separations_and_purifications": "separations-and-purifications",
    "cp_carboxylic_acids": "carboxylic-acids",
    "bb_amino_acids": "amino-acids",
    "bb_control_of_enzyme_activity": "enzyme-regulation",
    "bb_principles_of_bioenergetics": "bioenergetics",
    "bb_glycolysis_gluconeogenesis_and_the_pentose_phosphate_pathway": "glycolysis",
    "bb_oxidative_phosphorylation": "oxidative-phosphorylation",
    "bb_translation": "transcription-translation",
    "bb_transcription": "transcription-translation",
    "bb_mendelian_concepts": "mendelian-genetics",
    "ps_sensory_processing": "sensory-processing",
    "ps_memory": "memory",
    "ps_associative_learning": "associative-learning",
    "ps_social_class": "social-class",
    "ps_theoretical_approaches": "theoretical-approaches",
}

# --- MMLU ---------------------------------------------------------------------

MMLU_ROWS_API = "https://datasets-server.huggingface.co/rows"
MMLU_DATASET = "cais/mmlu"
MMLU_SPLITS = ("test", "validation", "dev")
MMLU_URL = "https://github.com/hendrycks/test"
MMLU_PAGE = 100  # max rows per datasets-server request

# MCAT-relevant MMLU subjects -> blueprint topic.
MMLU_SUBJECT_MAP: dict[str, str] = {
    "college_biology": "biology",
    "high_school_biology": "biology",
    "anatomy": "biology",
    "college_medicine": "biology",
    "clinical_knowledge": "biology",
    "medical_genetics": "biology",
    "professional_medicine": "biology",
    "virology": "biology",
    "human_aging": "biology",
    "nutrition": "biology",
    "college_chemistry": "general-chemistry",
    "high_school_chemistry": "general-chemistry",
    "college_physics": "physics",
    "high_school_physics": "physics",
    "conceptual_physics": "physics",
    "astronomy": "physics",
    "high_school_psychology": "psychology",
    "professional_psychology": "psychology",
    "sociology": "sociology",
}

_LETTERS = ("A", "B", "C", "D", "E", "F")


@dataclass
class Question:
    """Normalized, source-agnostic question record (the vendored schema)."""

    uid: str
    stem: str
    options: list[str]
    correct: str  # letter, e.g. "C"
    explanation: str
    topics: list[str]
    pool: str  # "served" | "heldout"
    source: str  # human-readable per-item credit
    license: str
    origin: str  # short machine key: "openmcat" | "mmlu"
    difficulty_b: float
    discrimination_a: float
    ai_generated: bool = False
    # Fine-grained concept (matches a first-principles concept::) when known,
    # feeding the import-time gates:: linkage. Empty when no mapping exists.
    concept: str = ""


def _fetch_json(url: str, *, max_retries: int = 8) -> Any:
    """GET + parse JSON with an on-disk cache and exponential backoff (429/503)."""
    cache_key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if cache_file.exists():
        with cache_file.open(encoding="utf-8") as fh:
            return json.load(fh)

    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    delay = 2.0
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310
                payload = json.load(resp)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with cache_file.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            return payload
        except urllib.error.HTTPError as err:
            if err.code in (429, 502, 503) and attempt < max_retries - 1:
                wait = float(err.headers.get("Retry-After") or delay)
                print(f"    {err.code}; retrying in {wait:.0f}s…")
                time.sleep(wait)
                delay = min(delay * 2, 60.0)
                continue
            raise
        except urllib.error.URLError as err:
            if attempt < max_retries - 1:
                print(f"    {err}; retrying in {delay:.0f}s…")
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            raise
    raise RuntimeError(f"exhausted retries for {url}")


def _pool_for(uid: str) -> str:
    digest = hashlib.sha1(uid.encode("utf-8")).hexdigest()
    return "heldout" if int(digest, 16) % HELDOUT_MODULUS == 0 else "served"


def _difficulty_b(label: str | None) -> float:
    return {"easy": -1.0, "medium": 0.0, "hard": 1.0}.get((label or "").lower(), 0.0)


# --- OpenMCAT ingestion -------------------------------------------------------


def _render_passage(passage: dict[str, Any]) -> str:
    """Flatten an OpenMCAT passage (text + tables + figures) into plain text so
    passage-dependent questions are self-contained in the note."""
    parts: list[str] = []
    if title := passage.get("title"):
        parts.append(f"Passage: {title}")
    if text := passage.get("text"):
        parts.append(text)
    for table in passage.get("tables", []):
        if caption := table.get("caption"):
            parts.append(f"Table — {caption}")
        columns = table.get("columns", [])
        if columns:
            parts.append(" | ".join(str(c) for c in columns))
        for row in table.get("rows", []):
            parts.append(" | ".join(str(c) for c in row))
    for figure in passage.get("figureDescriptions", []):
        if desc := figure.get("description") or figure.get("caption"):
            parts.append(f"Figure: {desc}")
    return "\n".join(parts)


def _openmcat_topic(tested_ids: list[str], section_id: str) -> str:
    for tid in tested_ids:
        if tid in OPENMCAT_TOPIC_MAP:
            return OPENMCAT_TOPIC_MAP[tid]
    return OPENMCAT_SECTION_FALLBACK.get(section_id, "biology")


def _openmcat_concept(tested_ids: list[str]) -> str:
    """First tested-topic id that maps to a first-principles concept, else ''."""
    for tid in tested_ids:
        if tid in OPENMCAT_CONCEPT_MAP:
            return OPENMCAT_CONCEPT_MAP[tid]
    return ""


def _openmcat_explanation(question: dict[str, Any]) -> str:
    parts: list[str] = []
    if explanation := question.get("explanation"):
        parts.append(explanation)
    choice_expls = question.get("choiceExplanations") or {}
    for letter in _LETTERS:
        if letter in choice_expls:
            parts.append(f"{letter}. {choice_expls[letter]}")
    if mistake := question.get("commonMistake"):
        parts.append(f"Common mistake: {mistake}")
    return "\n".join(parts)


def fetch_openmcat() -> list[Question]:
    out: list[Question] = []
    for section_id, section_name in OPENMCAT_SECTIONS.items():
        data = _fetch_json(f"{OPENMCAT_RAW}/{section_id}.json")
        passages = {p["id"]: p for p in data.get("passages", [])}
        for question in data.get("questions", []):
            choices = question.get("choices", [])
            if len(choices) < 2:
                continue
            options = [str(c.get("text", "")).strip() for c in choices]
            correct_id = question.get("correctChoiceId")
            ids = [c.get("id") for c in choices]
            if correct_id not in ids:
                continue
            correct = _LETTERS[ids.index(correct_id)]

            stem = str(question.get("stem", "")).strip()
            passage = passages.get(question.get("passageId"))
            if passage:
                stem = f"{_render_passage(passage)}\n\n{stem}"

            uid = f"openmcat-{section_id}-{question.get('id')}"
            tested_ids = question.get("testedTopicIds", [])
            topic = _openmcat_topic(tested_ids, section_id)
            out.append(
                Question(
                    uid=uid,
                    stem=stem,
                    options=options,
                    correct=correct,
                    explanation=_openmcat_explanation(question),
                    topics=[topic],
                    pool=_pool_for(uid),
                    source=f"OpenMCAT — {section_name} bank",
                    license="AGPL-3.0",
                    origin="openmcat",
                    difficulty_b=_difficulty_b(question.get("estimatedDifficulty")),
                    discrimination_a=1.0,
                    ai_generated=True,
                    concept=_openmcat_concept(tested_ids),
                )
            )
    return out


# --- MMLU ingestion -----------------------------------------------------------


def _fetch_mmlu_split(subject: str, split: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        url = (
            f"{MMLU_ROWS_API}?dataset={MMLU_DATASET}&config={subject}"
            f"&split={split}&offset={offset}&length={MMLU_PAGE}"
        )
        payload = _fetch_json(url)
        batch = payload.get("rows", [])
        rows.extend(r["row"] for r in batch)
        total = payload.get("num_rows_total", len(rows))
        offset += MMLU_PAGE
        if offset >= total or not batch:
            break
        time.sleep(0.6)  # be polite to the public API (avoid 429s)
    return rows


def fetch_mmlu() -> list[Question]:
    out: list[Question] = []
    for subject, topic in MMLU_SUBJECT_MAP.items():
        pretty = subject.replace("_", " ")
        for split in MMLU_SPLITS:
            for index, row in enumerate(_fetch_mmlu_split(subject, split)):
                choices = [str(c).strip() for c in row.get("choices", [])]
                answer = row.get("answer")
                if len(choices) < 2 or not isinstance(answer, int):
                    continue
                if not 0 <= answer < len(choices):
                    continue
                uid = f"mmlu-{subject}-{split}-{index}"
                out.append(
                    Question(
                        uid=uid,
                        stem=str(row.get("question", "")).strip(),
                        options=choices,
                        correct=_LETTERS[answer],
                        explanation="",  # MMLU ships no explanations
                        topics=[topic],
                        pool=_pool_for(uid),
                        source=f"MMLU — {pretty}",
                        license="MIT",
                        origin="mmlu",
                        difficulty_b=0.0,
                        discrimination_a=1.0,
                        ai_generated=False,
                    )
                )
    return out


# --- assembly -----------------------------------------------------------------


@dataclass
class Bank:
    schema_version: int
    generated_at: str
    attribution: list[dict[str, str]]
    counts: dict[str, int]
    questions: list[dict[str, Any]] = field(default_factory=list)


def build_bank(questions: list[Question]) -> Bank:
    by_origin: dict[str, int] = {}
    by_pool: dict[str, int] = {}
    by_topic: dict[str, int] = {}
    for q in questions:
        by_origin[q.origin] = by_origin.get(q.origin, 0) + 1
        by_pool[q.pool] = by_pool.get(q.pool, 0) + 1
        for t in q.topics:
            by_topic[t] = by_topic.get(t, 0) + 1

    counts = {"total": len(questions)}
    counts.update({f"origin:{k}": v for k, v in sorted(by_origin.items())})
    counts.update({f"pool:{k}": v for k, v in sorted(by_pool.items())})
    counts.update({f"topic:{k}": v for k, v in sorted(by_topic.items())})

    return Bank(
        schema_version=SCHEMA_VERSION,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        counts=counts,
        attribution=[
            {
                "source": "OpenMCAT",
                "url": OPENMCAT_URL,
                "license": "AGPL-3.0",
                "note": "MCAT-specific, AI-generated; gated by the M2 eval harness.",
            },
            {
                "source": "MMLU (Hendrycks et al., 2021)",
                "url": MMLU_URL,
                "license": "MIT",
                "note": "MCAT-relevant subsets only; no explanations upstream.",
            },
        ],
        questions=[asdict(q) for q in questions],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report counts without writing the vendored file.",
    )
    parser.add_argument(
        "--skip-mmlu",
        action="store_true",
        help="Only fetch OpenMCAT (faster; useful for quick iteration).",
    )
    args = parser.parse_args()

    print("Fetching OpenMCAT…")
    questions = fetch_openmcat()
    print(f"  OpenMCAT: {len(questions)} questions")
    if not args.skip_mmlu:
        print("Fetching MMLU (MCAT-relevant subsets)…")
        mmlu = fetch_mmlu()
        print(f"  MMLU: {len(mmlu)} questions")
        questions += mmlu

    raw_bank = build_bank(questions)
    print("Raw counts:", json.dumps(raw_bank.counts, indent=2))

    # Curate the freshly-fetched bank with the SAME deterministic rules used to
    # maintain the vendored file, so a network regen and an offline `curate.py
    # --in-place` stay consistent (blueprint-shaped, length-capped, concept-
    # mapped served pool + proportional heldout).
    from curate import (  # type: ignore[import-not-found]  # local tool import (same dir)
        build_curated_bank,
    )

    bank_dict, report = build_curated_bank(asdict(raw_bank))
    print("Curation report:", json.dumps(report, indent=2))
    print("Curated counts:", json.dumps(bank_dict["counts"], indent=2))

    if args.dry_run:
        print("Dry run — not writing.")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # ``mtime=0`` keeps the gzip byte-for-byte reproducible across runs.
    payload = json.dumps(bank_dict, ensure_ascii=False).encode("utf-8")
    with gzip.GzipFile(OUTPUT_PATH, "wb", compresslevel=9, mtime=0) as fh:
        fh.write(payload)
    size_mb = OUTPUT_PATH.stat().st_size / 1_000_000
    print(
        f"Wrote {OUTPUT_PATH} ({size_mb:.1f} MB, {bank_dict['counts']['total']} questions)"
    )


if __name__ == "__main__":
    main()
