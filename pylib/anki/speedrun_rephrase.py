# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Speedrun (MCAT fork) AI rephrasal feature (Plan §2a — the AI generator).

This module implements the *grounded rephrasal* generator: it takes an existing
served ``SpeedrunQuestion`` and produces a NEW single-best-answer variant that
tests the **same concept, with the same correct answer**, worded differently.
It is the "same material, different phrasing" generator that also powers the
guided-session recap. It deliberately does **not** free-form generate questions
from arbitrary textbook text — every variant is grounded in one source question
so it always has a traceable origin (a hard Plan requirement: outputs with no
traceable source are rejected).

Design (all additive; nothing here re-implements or edits the frozen engine):

* **Pluggable providers.** A small :class:`Provider` protocol with two concrete
  impls: :class:`OpenAIProvider` (cloud; lazy ``import openai``; reads
  ``OPENAI_API_KEY``; model configurable) and :class:`MockProvider` (fully
  deterministic, no network — the CI / grading path and the default in tests).
* **Native-object storage.** :func:`generate_variants` writes each accepted
  variant as a native ``SpeedrunQuestion`` note (reusing the frozen notetype /
  tags from :mod:`anki.speedrun`), so variants sync to Android for free exactly
  like the imported bank — no Android-side OpenAI calls.
* **AI-off is a clean no-op.** With no provider (or no key/library),
  :func:`generate_variants` returns immediately having written nothing and
  blocking nothing. The rest of the app scores fine with AI disabled.
* **Quality gate.** Every candidate is graded (:class:`HeuristicGrader` by
  default — answer preserved, options grounded, no heldout leakage) and blocked
  when it falls below ``min_quality`` or is classified *wrong*, so a bad variant
  never reaches a student. See :mod:`tools.speedrun.rephrase_eval` for the
  offline eval harness, its pre-set pass cutoff, and the baseline / leakage
  reports.

Import hygiene: the pure parts (providers, grader, prompts, tokenisation) only
use the stdlib so the eval harness and tests can import this module *without* a
built Rust backend. The few tag/field constants and helpers from
:mod:`anki.speedrun` (which pulls in the compiled protobuf) are imported lazily
inside :func:`generate_variants`, the only function that needs a live
``Collection``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import anki.collection

# --- Public tag / config contract (new; additive) ----------------------------

#: Tag on every AI-generated variant note. Reused from :mod:`anki.speedrun` so
#: the existing M2 eval/gating tooling already finds them; re-declared here as a
#: literal only for documentation — the runtime value is imported lazily in
#: :func:`generate_variants` to keep this constant and the source of truth in
#: one place (a test asserts they match).
AI_GENERATED_TAG = "bank::ai-generated"
#: Links a variant back to the note id of the served question it rephrases, so
#: recap/coverage can treat a variant as "the same material" and the eval can
#: prove every variant has a traceable source.
VARIANT_OF_TAG_PREFIX = "variant-of::"
#: Per-variant stable id tag, making generation idempotent (re-running never
#: duplicates; a variant that already synced from another device is skipped).
VARIANT_UID_TAG_PREFIX = "variantuid::"

#: Default OpenAI model: a current small/cheap chat model. Configurable per
#: provider instance / CLI flag / ``OPENAI_MODEL`` env var.
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# --- Quality thresholds (pre-registered; see rephrase_eval.py) ----------------

#: Per-variant gate: a candidate scoring below this (see :class:`HeuristicGrader`
#: scoring) is blocked from being written, regardless of the batch pass/fail.
#: 0.6 keeps clearly-grounded variants while rejecting answer-drift or ungrounded
#: option sets; validated against the mock eval which lands well above it.
DEFAULT_MIN_QUALITY = 0.6

# Classification labels (Plan §2a: correct-and-useful / wrong / bad-teaching).
LABEL_CORRECT_USEFUL = "correct-and-useful"
LABEL_WRONG = "wrong"
LABEL_BAD_TEACHING = "correct-but-bad-teaching"

# --- Tunable heuristic-grader thresholds -------------------------------------

#: Min token-Jaccard between the source and variant *correct answer* text to
#: treat the underlying fact as preserved. A grounded reword shares most content
#: tokens; answer drift (a different fact) drops well below this.
_ANSWER_PRESERVED_JACCARD = 0.5
#: Min average best-match Jaccard of variant options against source options for
#: the option set to count as "grounded" (drawn from the same answer space, not
#: fabricated).
_OPTIONS_GROUNDED_JACCARD = 0.34
#: At/above this Jaccard to a heldout item, a variant is a leakage near-dup.
_LEAKAGE_JACCARD = 0.6
#: An explanation shorter than this reads as no real teaching (bad-teaching).
_MIN_EXPLANATION_CHARS = 40

_LETTERS = "ABCDEFGH"

# Generic tokens with no discriminating signal, dropped before overlap scoring
# (kept local so this module needs no heavy import; mirrors the spirit of
# ``anki.speedrun._LINK_STOPWORDS``).
_STOPWORDS = frozenset(
    """
    the a an and or of to in on at for with without from by as is are was were be been
    being it its this that these those which who whom whose what when where why how
    into onto than then thus so if but not no nor can could should would may might must
    will shall do does did done has have had having each per both all any some more most
    less least such only also very much many few one following about best correct answer
    question choice choices option options statement describes true false most likely
    """.split()
)


# --- Tokenisation / similarity (pure) ----------------------------------------


def _normalize(text: str) -> str:
    """Lowercase and collapse whitespace for stable comparisons."""
    return " ".join(text.lower().split())


def tokens(text: str) -> set[str]:
    """Lowercase alphanumeric content tokens (len >= 3, non-stopword)."""
    out: set[str] = set()
    current: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            current.append(ch)
        elif current:
            out.add("".join(current))
            current = []
    if current:
        out.add("".join(current))
    return {t for t in out if len(t) >= 3 and t not in _STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two token sets (0.0 when both empty)."""
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _letter_to_index(correct: str, num_options: int = len(_LETTERS)) -> int:
    """Resolve a ``correct`` value (letter A-H or 1-based number) to an index."""
    text = str(correct).strip()
    if not text:
        return -1
    if text[0].isalpha():
        idx = ord(text[0].upper()) - ord("A")
    else:
        try:
            idx = int(text) - 1
        except ValueError:
            return -1
    return idx if 0 <= idx < num_options else -1


def _index_to_letter(index: int) -> str:
    return _LETTERS[index] if 0 <= index < len(_LETTERS) else ""


def _as_option_list(options: Any) -> list[str]:
    """Accept options as a list or a newline-delimited string; normalise."""
    if isinstance(options, str):
        raw: list[Any] = options.splitlines()
    else:
        raw = list(options or [])
    return [" ".join(str(o).split()) for o in raw if str(o).strip()]


# --- Question value objects ---------------------------------------------------


@dataclass
class SourceQuestion:
    """The served question being rephrased (read-only input to a provider)."""

    stem: str
    options: list[str]
    correct: str  # letter A-H
    explanation: str
    source: str
    topics: list[str] = field(default_factory=list)
    concept: str = ""
    difficulty_b: float = 0.0
    discrimination_a: float = 1.0
    note_id: int | None = None

    @property
    def correct_index(self) -> int:
        return _letter_to_index(self.correct, len(self.options))

    @property
    def correct_option(self) -> str:
        idx = self.correct_index
        return self.options[idx] if 0 <= idx < len(self.options) else ""

    @classmethod
    def from_dict(cls, fields: dict[str, Any]) -> SourceQuestion:
        """Build from a plain dict of ``SpeedrunQuestion`` fields (used by the
        eval harness, which works on vendored JSON without a collection)."""
        options = _as_option_list(fields.get("options", []))
        correct = str(fields.get("correct", "")).strip()
        # Normalise a numeric/letter ``correct`` to a canonical letter.
        idx = _letter_to_index(correct, len(options)) if options else -1
        letter = _index_to_letter(idx) if idx >= 0 else correct
        topics = list(fields.get("topics", []) or [])
        return cls(
            stem=str(fields.get("stem", "")),
            options=options,
            correct=letter,
            explanation=str(fields.get("explanation", "")),
            source=str(fields.get("source", "")),
            topics=topics,
            concept=str(fields.get("concept", "")),
            difficulty_b=_safe_float(fields.get("difficulty_b"), 0.0),
            discrimination_a=_safe_float(fields.get("discrimination_a"), 1.0),
            note_id=fields.get("note_id"),
        )


@dataclass
class RephrasedQuestion:
    """A generated variant returned by a :class:`Provider`."""

    stem: str
    options: list[str]
    correct: str  # letter A-H
    explanation: str
    source: str

    @property
    def correct_index(self) -> int:
        return _letter_to_index(self.correct, len(self.options))

    @property
    def correct_option(self) -> str:
        idx = self.correct_index
        return self.options[idx] if 0 <= idx < len(self.options) else ""


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --- Prompt design (shared by the OpenAI provider; exposed for transparency) --

REPHRASE_SYSTEM_PROMPT = (
    "You are an expert MCAT item writer. You rewrite one single-best-answer "
    "multiple-choice question into a NEW variant that tests the EXACT same "
    "concept and keeps the SAME correct answer.\n"
    "Where the rewording goes: the STEM changes, the ANSWER SPACE does not.\n"
    "Strict rules:\n"
    "1. Substantially reword the STEM in fresh language — a new scenario, frame, "
    "or phrasing for the same underlying question. Do not copy the original stem "
    "verbatim.\n"
    "2. KEEP THE SAME ANSWER CHOICES. Preserve every option's meaning and its "
    "key identifying terms (technical names, values, units, chemical/anatomical "
    "terms). You may lightly polish wording, but do NOT substitute new facts, do "
    "NOT add or remove options, and do NOT introduce any answer that is not in "
    "the source. Keeping the answer space fixed is what guarantees the variant "
    "has the same correct answer and cannot drift to a different fact.\n"
    "3. The correct choice must remain the SAME fact as the source's correct "
    "option. Never change which choice is correct.\n"
    "4. Keep exactly the same number of options.\n"
    "5. Write a concise (1-2 sentence) explanation of why the correct answer is "
    "right.\n"
    "6. Do not reference 'the original question' or 'the passage'. The variant "
    "must stand alone.\n"
    'Respond with STRICT JSON only: {"stem": str, "options": [str, ...], '
    '"correct_index": int (0-based), "explanation": str}.'
)


def build_user_prompt(source: SourceQuestion) -> str:
    """The per-question user prompt: the source item as compact JSON."""
    payload = {
        "stem": source.stem,
        "options": source.options,
        "correct_index": source.correct_index,
        "explanation": source.explanation,
        "concept": source.concept,
        "topics": source.topics,
    }
    return (
        "Rewrite this MCAT question into one same-concept, same-answer variant.\n"
        + json.dumps(payload, ensure_ascii=False)
    )


# --- Provider protocol + implementations -------------------------------------


@runtime_checkable
class Provider(Protocol):
    """A pluggable rephrasal backend. ``name`` is used in eval reports."""

    name: str

    def rephrase(self, source: SourceQuestion) -> RephrasedQuestion: ...


class OpenAIProvider:
    """Cloud provider backed by the OpenAI Chat Completions API.

    ``openai`` is imported lazily and only when a rephrasal is actually
    requested, so the library is a genuinely optional dependency and the AI-off
    path never needs it installed. Requires ``OPENAI_API_KEY`` (or an explicit
    ``api_key``); raises a clear error if neither the key nor the library is
    available *at call time*.
    """

    name = "openai"

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        # Low temperature: this is a faithful rephrasal, not creative writing.
        # Higher values let the model drift the correct fact or invent options
        # absent from the source, which the grader (correctly) rejects as wrong.
        temperature: float = 0.3,
    ) -> None:
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.temperature = temperature
        self._client: Any = None

    @classmethod
    def available(cls, api_key: str | None = None) -> bool:
        """True when a key is present and the ``openai`` library imports."""
        if not (api_key or os.environ.get("OPENAI_API_KEY")):
            return False
        try:
            import openai  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            return False
        return True

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise RephraseError(
                "OpenAIProvider requires OPENAI_API_KEY (set the env var or pass "
                "api_key=...). Use MockProvider for offline/CI runs."
            )
        try:
            import openai  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RephraseError(
                "The 'openai' package is not installed. Install the optional "
                "dependency (pip install openai) or use MockProvider."
            ) from exc
        self._client = openai.OpenAI(api_key=self.api_key)
        return self._client

    def rephrase(self, source: SourceQuestion) -> RephrasedQuestion:
        client = self._get_client()
        response = client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": REPHRASE_SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(source)},
            ],
        )
        content = response.choices[0].message.content or "{}"
        return parse_provider_json(content, source)

    def rephrase_card(self, source: SourceCard) -> RephrasedCard:
        client = self._get_client()
        response = client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CARD_REPHRASE_SYSTEM_PROMPT},
                {"role": "user", "content": build_card_user_prompt(source)},
            ],
        )
        content = response.choices[0].message.content or "{}"
        return parse_card_provider_json(content, source)


def parse_provider_json(content: str, source: SourceQuestion) -> RephrasedQuestion:
    """Parse a provider's JSON reply into a :class:`RephrasedQuestion`.

    Accepts ``correct_index`` (0-based) or ``correct`` (letter/number). The
    variant always inherits the source's ``source`` credit so it is traceable
    even if the model omits it (Plan: no untraceable outputs).
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RephraseError(f"provider returned non-JSON output: {exc}") from exc
    options = _as_option_list(data.get("options", []))
    if "correct_index" in data:
        idx = int(data.get("correct_index", -1))
    else:
        idx = _letter_to_index(str(data.get("correct", "")), len(options))
    letter = _index_to_letter(idx)
    return RephrasedQuestion(
        stem=str(data.get("stem", "")).strip(),
        options=options,
        correct=letter,
        explanation=str(data.get("explanation", "")).strip(),
        source=str(data.get("source") or source.source),
    )


class MockProvider:
    """Deterministic, offline provider for tests / CI / grading.

    It produces a grounded variant with **zero randomness or network**: the
    correct answer's *text* is preserved verbatim (so the fact never drifts),
    the options are deterministically reordered (exercising correct-letter
    tracking), and the stem is lightly reworded. ``quality`` lets tests force
    degenerate output to prove the quality gate and leakage checks bite:

    * ``"good"`` — a clean, grounded, well-taught variant (the default).
    * ``"wrong"`` — marks a distractor as correct (answer drift -> blocked).
    * ``"verbatim"`` — copies the stem and drops the explanation (bad-teaching).
    """

    def __init__(self, quality: str = "good") -> None:
        self.quality = quality
        self.name = f"mock:{quality}" if quality != "good" else "mock"

    def rephrase(self, source: SourceQuestion) -> RephrasedQuestion:
        options = list(source.options)
        if not options:
            return RephrasedQuestion("", [], "", "", source.source)
        correct_idx = source.correct_index if source.correct_index >= 0 else 0
        correct_text = options[correct_idx]

        # Deterministic reorder: rotate by a stable offset from the stem so the
        # correct letter usually moves (tests letter tracking), option TEXT is
        # untouched so the fact is preserved.
        offset = (sum(ord(c) for c in _normalize(source.stem)) % len(options)) or 1
        reordered = options[offset:] + options[:offset]
        new_idx = reordered.index(correct_text)

        if self.quality == "wrong":
            # Pick any distractor as "correct" -> answer drift the grader catches.
            distractor = next(
                (i for i in range(len(reordered)) if i != new_idx), new_idx
            )
            new_idx = distractor

        if self.quality == "verbatim":
            stem = source.stem
            explanation = ""
        else:
            stem = _mock_reword_stem(source.stem)
            explanation = source.explanation.strip() or (
                "The correct choice follows from the same core principle the "
                "original item tests; the other options are common distractors."
            )

        return RephrasedQuestion(
            stem=stem,
            options=reordered,
            correct=_index_to_letter(new_idx),
            explanation=explanation,
            source=source.source,
        )

    def rephrase_card(self, source: SourceCard) -> RephrasedCard:
        """Deterministic flashcard variant: reword the FRONT, preserve the BACK
        fact verbatim (the strongest possible "fact preserved" signal). The
        ``quality`` knob mirrors :meth:`rephrase` so tests can force the card
        grader's wrong / bad-teaching verdicts.

        * ``"good"`` — reworded front, fact-preserving back (default).
        * ``"wrong"`` — replaces the back with a different fact (drift -> blocked).
        * ``"verbatim"`` — copies the front unchanged; the fact survives but the
          front is not rephrased (bad-teaching, filtered but not dangerous).
        """
        if self.quality == "wrong":
            return RephrasedCard(
                front=_mock_reword_front(source.front),
                back="This is an unrelated, contradicting fact not present in the "
                "source card.",
            )
        if self.quality == "verbatim":
            return RephrasedCard(front=source.front, back=source.back)
        return RephrasedCard(front=_mock_reword_front(source.front), back=source.back)


def _mock_reword_stem(stem: str) -> str:
    """A deterministic, content-preserving reword used only by the mock."""
    s = stem.strip().rstrip("?.")
    return f"Consider the following MCAT scenario. {s}. Which single option is best?"


def _mock_reword_front(front: str) -> str:
    """Deterministic, content-preserving reword of a flashcard front."""
    s = front.strip().rstrip("?.")
    return f"Explain from first principles: {s}."


# --- Grading -----------------------------------------------------------------


@dataclass
class VariantGrade:
    """The automatic grader's verdict on one variant."""

    label: str
    score: float
    answer_preserved: bool
    options_grounded: bool
    leaked: bool
    reworded: bool
    good_teaching: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def acceptable(self) -> bool:
        """A variant safe to write: not wrong and not leaking."""
        return self.label != LABEL_WRONG and not self.leaked


@runtime_checkable
class Grader(Protocol):
    def grade(
        self, source: SourceQuestion, variant: RephrasedQuestion
    ) -> VariantGrade: ...


class HeuristicGrader:
    """Deterministic, offline automatic grader (no network, no model).

    Checks the properties the Plan calls out — the correct answer's meaning is
    preserved, options are plausible/grounded in the source, nothing leaks a
    heldout item — and derives the three-way label plus a continuous ``score``
    used by the ``min_quality`` gate. Suitable for CI and as the deterministic
    fallback behind an optional LLM-judge.
    """

    def __init__(self, heldout_texts: list[str] | None = None) -> None:
        # Pre-tokenise heldout stems once for the leakage near-dup scan.
        self._heldout_tokens = [tokens(t) for t in (heldout_texts or [])]

    def grade(self, source: SourceQuestion, variant: RephrasedQuestion) -> VariantGrade:
        reasons: list[str] = []

        src_correct = tokens(source.correct_option)
        var_correct = tokens(variant.correct_option)
        answer_jac = jaccard(src_correct, var_correct)
        # A verbatim-preserved correct option is the strongest possible signal
        # that the underlying fact did not drift — stronger than any token
        # overlap — and it is the only reliable signal when the answer text is
        # too short or enumerated (e.g. "True", "7.4", "All of the above") to
        # yield content tokens for a Jaccard measure.
        src_ans_norm = _normalize(source.correct_option)
        exact_answer = bool(src_ans_norm) and (
            _normalize(variant.correct_option) == src_ans_norm
        )
        if exact_answer:
            answer_jac = 1.0
        answer_preserved = exact_answer or (
            bool(var_correct)
            and bool(src_correct)
            and answer_jac >= _ANSWER_PRESERVED_JACCARD
        )
        if not answer_preserved:
            reasons.append(
                f"correct-answer meaning not preserved (jaccard={answer_jac:.2f})"
            )

        options_score = _options_grounding(source.options, variant.options)
        options_grounded = (
            len(variant.options) == len(source.options)
            and options_score >= _OPTIONS_GROUNDED_JACCARD
        )
        if not options_grounded:
            reasons.append(
                f"options not grounded in source (score={options_score:.2f})"
            )

        leaked = self._is_leak(variant)
        if leaked:
            reasons.append("variant near-duplicates a pool::heldout item")

        reworded = _normalize(variant.stem) != _normalize(source.stem)
        if not reworded:
            reasons.append("stem is a verbatim copy (not rephrased)")

        good_teaching = len(variant.explanation.strip()) >= _MIN_EXPLANATION_CHARS
        if not good_teaching:
            reasons.append("explanation too thin to teach")

        # Continuous score for the min_quality gate.
        score = (
            0.55 * answer_jac
            + 0.25 * options_score
            + 0.10 * (0.0 if leaked else 1.0)
            + 0.10 * (1.0 if good_teaching else 0.0)
        )

        if leaked or not answer_preserved or not options_grounded:
            label = LABEL_WRONG
        elif not reworded or not good_teaching:
            label = LABEL_BAD_TEACHING
        else:
            label = LABEL_CORRECT_USEFUL

        return VariantGrade(
            label=label,
            score=round(score, 4),
            answer_preserved=answer_preserved,
            options_grounded=options_grounded,
            leaked=leaked,
            reworded=reworded,
            good_teaching=good_teaching,
            reasons=reasons,
        )

    def _is_leak(self, variant: RephrasedQuestion) -> bool:
        if not self._heldout_tokens:
            return False
        probe = tokens(variant.stem + " " + " ".join(variant.options))
        return any(
            jaccard(probe, held) >= _LEAKAGE_JACCARD for held in self._heldout_tokens
        )


def _options_grounding(source_opts: list[str], variant_opts: list[str]) -> float:
    """Average best-match Jaccard of each variant option to a source option.

    High when the variant's answer space is drawn from the same facts as the
    source (grounded); low when options are fabricated from new material.
    """
    if not variant_opts:
        return 0.0
    src = [tokens(o) for o in source_opts]
    if not src:
        return 0.0
    src_norm = {_normalize(o) for o in source_opts}
    totals = 0.0
    for opt in variant_opts:
        # An option reproduced verbatim from the source is maximally grounded,
        # even when it is too short to yield content tokens.
        if _normalize(opt) in src_norm:
            totals += 1.0
            continue
        vt = tokens(opt)
        totals += max((jaccard(vt, s) for s in src), default=0.0)
    return totals / len(variant_opts)


# --- Leakage scan (used by the eval harness) ---------------------------------


@dataclass
class LeakageReport:
    """Result of scanning generation inputs/outputs for heldout leakage."""

    heldout_in_inputs: list[str] = field(default_factory=list)
    near_duplicates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.heldout_in_inputs and not self.near_duplicates


def scan_leakage(
    input_items: list[dict[str, Any]],
    variant_texts: list[str],
    heldout_items: list[dict[str, Any]],
) -> LeakageReport:
    """Scan for any ``pool::heldout`` item among the inputs, or any generated
    variant that near-duplicates a heldout item. Returns a clean report only
    when neither is found (Plan: leakage check must report clean and refuse).
    """
    report = LeakageReport()
    for item in input_items:
        if item.get("pool") == "heldout":
            report.heldout_in_inputs.append(str(item.get("uid", "?")))

    heldout_tokens = [
        (str(h.get("uid", "?")), tokens(_item_text(h))) for h in heldout_items
    ]
    for i, text in enumerate(variant_texts):
        probe = tokens(text)
        for uid, held in heldout_tokens:
            sim = jaccard(probe, held)
            if sim >= _LEAKAGE_JACCARD:
                report.near_duplicates.append(
                    {
                        "variant_index": i,
                        "heldout_uid": uid,
                        "similarity": round(sim, 3),
                    }
                )
    return report


def _item_text(item: dict[str, Any]) -> str:
    return " ".join(
        [str(item.get("stem", "")), " ".join(_as_option_list(item.get("options", [])))]
    )


# --- Native variant generation (needs a live Collection) ---------------------


@dataclass
class GenerateSummary:
    """Outcome of a :func:`generate_variants` run."""

    #: True when AI was off (no provider) — a clean no-op, never blocks anything.
    ai_disabled: bool = False
    #: Source questions considered (served, traceable, well-formed).
    considered: int = 0
    #: Variants written as native notes.
    written: int = 0
    #: Candidates blocked by the min_quality gate / wrong classification.
    blocked: int = 0
    #: Variants skipped because their stable uid already existed (idempotency).
    skipped_existing: int = 0
    #: Heldout notes refused (never rephrased/served).
    refused_heldout: int = 0
    #: Sources skipped because they are themselves AI variants (no rephrasing a
    #: rephrasal — keeps generation idempotent and bounded).
    skipped_ai_source: int = 0
    #: Sources rejected for having no traceable ``source`` (Plan requirement).
    rejected_untraceable: int = 0
    #: Malformed sources (unresolvable correct answer / no options).
    rejected_malformed: int = 0
    #: Provider errors (network/parse) that skipped a candidate.
    provider_errors: int = 0
    #: Count of graded candidates by classification label.
    by_label: dict[str, int] = field(default_factory=dict)

    def _tally(self, label: str) -> None:
        self.by_label[label] = self.by_label.get(label, 0) + 1


def generate_variants(
    col: anki.collection.Collection,
    note_ids: list[int] | None = None,
    *,
    n: int = 1,
    provider: Provider | None = None,
    min_quality: float = DEFAULT_MIN_QUALITY,
    grader: Grader | None = None,
) -> GenerateSummary:
    """Generate grounded variant ``SpeedrunQuestion`` notes for served questions.

    For each source (served, never ``pool::heldout``) question, produce ``n``
    reworded same-concept variants via ``provider``, grade each, and write only
    those that clear the ``min_quality`` gate and are not classified *wrong*.
    Each accepted variant is a native note tagged ``bank::ai-generated``,
    ``variant-of::<source_note_id>``, ``pool::served``, inheriting the source's
    ``topic::``/``concept::`` tags, with ``source`` crediting the original — so
    it syncs to Android for free and is always traceable.

    Idempotent via a stable ``variantuid::`` tag, so re-running (or running after
    variants synced from another device) never duplicates.

    **AI-off:** with ``provider is None`` this is an immediate no-op that writes
    nothing and blocks nothing (``summary.ai_disabled`` is True).
    """
    summary = GenerateSummary()
    if provider is None:
        summary.ai_disabled = True
        return summary

    # Lazy import: these pull in the compiled backend, so keep them out of the
    # module top level (eval/tests import the pure parts without a build).
    from anki.notes import NoteId
    from anki.speedrun import (
        BANK_AI_GENERATED_TAG,
        BANK_TAG,
        CONCEPT_TAG_PREFIX,
        POOL_HELDOUT_TAG,
        POOL_SERVED_TAG,
        TOPIC_TAG_PREFIX,
        concept_of_note,
        correct_index,
        option_lines,
        topics_of_note,
    )

    sr = col.speedrun
    if note_ids is None:
        note_ids = [int(nid) for nid in sr.served_question_note_ids()]

    notetype_id = sr.ensure_question_notetype()
    questions_deck, _ = sr.ensure_decks()
    notetype = col.models.get(notetype_id)
    assert notetype is not None

    if grader is None:
        grader = HeuristicGrader(heldout_texts=_heldout_texts(col))

    ctx = _GenContext(
        col=col,
        notetype=notetype,
        questions_deck=questions_deck,
        grader=grader,
        min_quality=min_quality,
        existing=_existing_variant_uids(col),
        topic_prefix=TOPIC_TAG_PREFIX,
        concept_prefix=CONCEPT_TAG_PREFIX,
        pool_served_tag=POOL_SERVED_TAG,
        bank_tag=BANK_TAG,
        ai_tag=BANK_AI_GENERATED_TAG,
        heldout_tag=POOL_HELDOUT_TAG,
        option_lines=option_lines,
        correct_index=correct_index,
        topics_of_note=topics_of_note,
        concept_of_note=concept_of_note,
    )

    for nid in note_ids:
        note = col.get_note(NoteId(int(nid)))
        source = _source_from_note(ctx, note, int(nid), summary)
        if source is None:
            continue
        summary.considered += 1
        _emit_variants(ctx, provider, source, int(nid), n, summary)

    return summary


@dataclass
class _GenContext:
    """Runtime handles + tag constants for :func:`generate_variants` helpers.

    Bundled so the per-source/per-variant helpers stay small and don't take a
    dozen positional arguments. The ``*_of_note`` / ``option_lines`` /
    ``correct_index`` callables are the lazily-imported ``anki.speedrun``
    helpers (kept off this module's top level so the pure parts import without a
    built backend).
    """

    col: Any
    notetype: Any
    questions_deck: Any
    grader: Grader
    min_quality: float
    existing: set[str]
    topic_prefix: str
    concept_prefix: str
    pool_served_tag: str
    bank_tag: str
    ai_tag: str
    heldout_tag: str
    option_lines: Any
    correct_index: Any
    topics_of_note: Any
    concept_of_note: Any


def _source_from_note(
    ctx: _GenContext, note: Any, nid: int, summary: GenerateSummary
) -> SourceQuestion | None:
    """Validate one question note and build its :class:`SourceQuestion`.

    Returns ``None`` (recording the reason on ``summary``) when the note is
    heldout, an AI variant, malformed, or has no traceable source.
    """
    tags = note.tags
    if ctx.heldout_tag in tags:
        summary.refused_heldout += 1
        return None
    # Never rephrase an AI-generated variant: it would spawn variants of
    # variants, breaking idempotency and drifting away from a real source.
    if ctx.ai_tag in tags:
        summary.skipped_ai_source += 1
        return None

    opts = ctx.option_lines(note["options"])
    idx = ctx.correct_index(note["correct"], len(opts))
    if idx < 0 or len(opts) < 2:
        summary.rejected_malformed += 1
        return None
    credit = str(note["source"]).strip()
    if not credit:
        summary.rejected_untraceable += 1
        return None

    return SourceQuestion(
        stem=str(note["stem"]),
        options=opts,
        correct=_index_to_letter(idx),
        explanation=str(note["explanation"]),
        source=credit,
        topics=ctx.topics_of_note(note),
        concept=ctx.concept_of_note(note) or "",
        difficulty_b=_safe_float(note["difficulty_b"], 0.0),
        discrimination_a=_safe_float(note["discrimination_a"], 1.0),
        note_id=nid,
    )


def _emit_variants(
    ctx: _GenContext,
    provider: Provider,
    source: SourceQuestion,
    nid: int,
    n: int,
    summary: GenerateSummary,
) -> None:
    """Generate, grade, and write up to ``n`` variants for one source note."""
    for i in range(n):
        variant_uid = f"{nid}-v{i}"
        if variant_uid in ctx.existing:
            summary.skipped_existing += 1
            continue
        try:
            variant = provider.rephrase(source)
        except Exception:  # noqa: BLE001 — a bad provider call must not abort the batch
            summary.provider_errors += 1
            continue

        if not variant.source.strip():
            summary.rejected_untraceable += 1
            continue
        grade = ctx.grader.grade(source, variant)
        summary._tally(grade.label)
        if grade.label == LABEL_WRONG or grade.score < ctx.min_quality:
            summary.blocked += 1
            continue

        new_note = _build_variant_note(
            ctx.col,
            ctx.notetype,
            source,
            variant,
            variant_uid,
            nid,
            topic_prefix=ctx.topic_prefix,
            concept_prefix=ctx.concept_prefix,
            pool_served_tag=ctx.pool_served_tag,
            bank_tag=ctx.bank_tag,
            ai_tag=ctx.ai_tag,
        )
        ctx.col.add_note(new_note, ctx.questions_deck)
        ctx.existing.add(variant_uid)
        summary.written += 1


def _build_variant_note(
    col: anki.collection.Collection,
    notetype: Any,
    source: SourceQuestion,
    variant: RephrasedQuestion,
    variant_uid: str,
    source_note_id: int,
    *,
    topic_prefix: str,
    concept_prefix: str,
    pool_served_tag: str,
    bank_tag: str,
    ai_tag: str,
) -> Any:
    """Build (not add) a native ``SpeedrunQuestion`` note for one variant."""
    note = col.new_note(notetype)
    note["stem"] = variant.stem
    # One option per line (the study loop parses ``options`` line-by-line).
    note["options"] = "\n".join(" ".join(o.split()) for o in variant.options)
    note["correct"] = variant.correct
    note["explanation"] = variant.explanation
    note["source"] = f"AI rephrasal of {source.source}"
    note["difficulty_b"] = f"{source.difficulty_b:.2f}"
    note["discrimination_a"] = f"{source.discrimination_a:.2f}"

    tags = [f"{topic_prefix}{t}" for t in source.topics]
    if source.concept:
        tags.append(f"{concept_prefix}{source.concept}")
    tags.append(pool_served_tag)
    tags.append(bank_tag)
    tags.append(ai_tag)
    tags.append(f"{VARIANT_OF_TAG_PREFIX}{source_note_id}")
    tags.append(f"{VARIANT_UID_TAG_PREFIX}{variant_uid}")
    note.tags = tags
    return note


def _existing_variant_uids(col: anki.collection.Collection) -> set[str]:
    """UIDs of variants that currently exist **as notes** (the idempotency key).

    Derived from live notes rather than ``col.tags.all()``: Anki keeps a tag in
    the registry even after every note carrying it is deleted, so reading the
    registry would treat a *deleted* variant as still-existing and wrongly skip
    regenerating it. Reading the notes makes "exists" mean what it says.
    """
    prefix = VARIANT_UID_TAG_PREFIX
    uids: set[str] = set()
    for nid in col.find_notes(f'"tag:{prefix}*"'):
        note = col.get_note(nid)
        uids.update(t[len(prefix) :] for t in note.tags if t.startswith(prefix))
    return uids


def _heldout_texts(col: anki.collection.Collection) -> list[str]:
    """Stem+options text of every heldout question, for the leakage near-dup
    scan inside the default grader."""
    from anki.speedrun import (
        POOL_HELDOUT_TAG,
        QUESTION_NOTETYPE_NAME,
        option_lines,
    )

    texts: list[str] = []
    for nid in col.find_notes(f"note:{QUESTION_NOTETYPE_NAME} tag:{POOL_HELDOUT_TAG}"):
        note = col.get_note(nid)
        texts.append(str(note["stem"]) + " " + " ".join(option_lines(note["options"])))
    return texts


# --- Flashcard rephrasal (first-principles memory cards) ---------------------
#
# The question path above rephrases single-best-answer MCQs. This path rephrases
# the *memory flashcards* — the hand-authored first-principles Basic (Front/Back)
# cards a missed question activates. A variant tests the SAME principle with a
# reworded FRONT while preserving the BACK fact, so it is genuine "same material,
# different phrasing" desirable-difficulty practice. Variants are written as
# native suspended Basic notes (so they sync + gate like their source) but are
# tagged AI-generated so the Rust Memory model excludes them (review-only:
# a reworded copy of a fact must not double-count that fact's mastery).

#: Min fraction of the source back's content tokens that must reappear in the
#: variant back for the underlying fact to count as preserved. Recall-oriented
#: (not Jaccard) so a faithful reword that *adds* words is not penalised, while a
#: back that drops or swaps the facts falls below it and is blocked as wrong.
_FACT_PRESERVED_COVERAGE = 0.5


@dataclass
class SourceCard:
    """A first-principles memory card being rephrased (read-only provider input)."""

    front: str
    back: str
    source: str
    topics: list[str] = field(default_factory=list)
    concept: str = ""
    note_id: int | None = None

    @classmethod
    def from_dict(cls, fields: dict[str, Any]) -> SourceCard:
        return cls(
            front=str(fields.get("front", "")),
            back=str(fields.get("back", "")),
            source=str(fields.get("source", "")),
            topics=list(fields.get("topics", []) or []),
            concept=str(fields.get("concept", "")),
            note_id=fields.get("note_id"),
        )


@dataclass
class RephrasedCard:
    """A generated flashcard variant returned by a :class:`CardProvider`."""

    front: str
    back: str


CARD_REPHRASE_SYSTEM_PROMPT = (
    "You are an expert MCAT tutor. You rewrite one first-principles flashcard "
    "(a FRONT prompt and a BACK answer) into a NEW variant that tests the EXACT "
    "same underlying fact/principle, worded differently.\n"
    "Strict rules:\n"
    "1. Substantially reword the FRONT in fresh language — a new phrasing, angle, "
    "or mini-scenario that still asks for the same principle. Do not copy the "
    "original front verbatim.\n"
    "2. The BACK must state the SAME fact(s) as the source back. Preserve every "
    "key term, value, unit, equation, and relationship. You may reword for "
    "clarity, but do NOT add facts absent from the source, do NOT drop any of "
    "its facts, and NEVER change or contradict what it says.\n"
    "3. Keep it self-contained: do not reference 'the original card' or 'the "
    "source'. The variant must stand alone.\n"
    "4. Keep the back concise and genuinely explanatory (no one-word answers).\n"
    'Respond with STRICT JSON only: {"front": str, "back": str}.'
)


def build_card_user_prompt(source: SourceCard) -> str:
    """The per-card user prompt: the source card as compact JSON."""
    payload = {
        "front": source.front,
        "back": source.back,
        "concept": source.concept,
        "topics": source.topics,
    }
    return (
        "Rewrite this first-principles flashcard into one same-fact variant.\n"
        + json.dumps(payload, ensure_ascii=False)
    )


@runtime_checkable
class CardProvider(Protocol):
    """A pluggable flashcard rephrasal backend. ``name`` is used in reports."""

    name: str

    def rephrase_card(self, source: SourceCard) -> RephrasedCard: ...


def parse_card_provider_json(content: str, source: SourceCard) -> RephrasedCard:
    """Parse a provider's JSON reply into a :class:`RephrasedCard`."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RephraseError(f"provider returned non-JSON output: {exc}") from exc
    return RephrasedCard(
        front=str(data.get("front", "")).strip(),
        back=str(data.get("back", "")).strip(),
    )


@dataclass
class CardVariantGrade:
    """The automatic grader's verdict on one flashcard variant."""

    label: str
    score: float
    fact_preserved: bool
    reworded: bool
    leaked: bool
    good_teaching: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def acceptable(self) -> bool:
        return self.label != LABEL_WRONG and not self.leaked


@runtime_checkable
class CardGrader(Protocol):
    def grade(self, source: SourceCard, variant: RephrasedCard) -> CardVariantGrade: ...


class HeuristicCardGrader:
    """Deterministic, offline grader for flashcard variants (no network, no model).

    Mirrors :class:`HeuristicGrader` but for Front/Back cards: the BACK fact must
    be preserved (recall-style token coverage, or an exact match), the FRONT must
    actually be reworded, the back must teach (non-trivial), and nothing may leak
    a heldout question. Derives the same three-way label + a continuous ``score``
    the ``min_quality`` gate uses.
    """

    def __init__(self, heldout_texts: list[str] | None = None) -> None:
        self._heldout_tokens = [tokens(t) for t in (heldout_texts or [])]

    def grade(self, source: SourceCard, variant: RephrasedCard) -> CardVariantGrade:
        reasons: list[str] = []

        coverage = _back_coverage(source.back, variant.back)
        exact_back = bool(_normalize(source.back)) and (
            _normalize(variant.back) == _normalize(source.back)
        )
        if exact_back:
            coverage = 1.0
        fact_preserved = exact_back or coverage >= _FACT_PRESERVED_COVERAGE
        if not fact_preserved:
            reasons.append(f"back fact not preserved (coverage={coverage:.2f})")

        reworded = bool(_normalize(variant.front)) and (
            _normalize(variant.front) != _normalize(source.front)
        )
        if not reworded:
            reasons.append("front is a verbatim copy (not rephrased)")

        good_teaching = len(variant.back.strip()) >= _MIN_EXPLANATION_CHARS
        if not good_teaching:
            reasons.append("back too thin to teach")

        leaked = self._is_leak(variant)
        if leaked:
            reasons.append("variant near-duplicates a pool::heldout item")

        score = (
            0.6 * coverage
            + 0.2 * (1.0 if reworded else 0.0)
            + 0.1 * (0.0 if leaked else 1.0)
            + 0.1 * (1.0 if good_teaching else 0.0)
        )

        if leaked or not fact_preserved:
            label = LABEL_WRONG
        elif not reworded or not good_teaching:
            label = LABEL_BAD_TEACHING
        else:
            label = LABEL_CORRECT_USEFUL

        return CardVariantGrade(
            label=label,
            score=round(score, 4),
            fact_preserved=fact_preserved,
            reworded=reworded,
            leaked=leaked,
            good_teaching=good_teaching,
            reasons=reasons,
        )

    def _is_leak(self, variant: RephrasedCard) -> bool:
        if not self._heldout_tokens:
            return False
        probe = tokens(variant.front + " " + variant.back)
        return any(
            jaccard(probe, held) >= _LEAKAGE_JACCARD for held in self._heldout_tokens
        )


def _back_coverage(source_back: str, variant_back: str) -> float:
    """Fraction of the source back's content tokens that reappear in the variant
    back (recall-oriented). 0.0 when the source has no content tokens."""
    src = tokens(source_back)
    if not src:
        return 0.0
    var = tokens(variant_back)
    return len(src & var) / len(src)


@dataclass
class GenerateCardSummary:
    """Outcome of a :func:`generate_card_variants` run (mirrors GenerateSummary)."""

    ai_disabled: bool = False
    considered: int = 0
    written: int = 0
    blocked: int = 0
    skipped_existing: int = 0
    skipped_ai_source: int = 0
    rejected_malformed: int = 0
    provider_errors: int = 0
    #: First provider exception seen (``"TypeName: message"``), so a run that
    #: silently errors on every call can still report *why* to the user.
    first_error: str = ""
    by_label: dict[str, int] = field(default_factory=dict)

    def _tally(self, label: str) -> None:
        self.by_label[label] = self.by_label.get(label, 0) + 1


def generate_card_variants(
    col: anki.collection.Collection,
    note_ids: list[int] | None = None,
    *,
    n: int = 1,
    provider: CardProvider | None = None,
    min_quality: float = DEFAULT_MIN_QUALITY,
    grader: CardGrader | None = None,
) -> GenerateCardSummary:
    """Generate grounded variant memory cards from first-principles Basic notes.

    For each source first-principles card, produce ``n`` reworded same-fact
    variants via ``provider``, grade each, and write only those that clear the
    ``min_quality`` gate and are not classified *wrong*. Each accepted variant is
    a native **suspended** ``Basic`` note in ``Speedrun::Cards`` tagged
    ``bank::ai-generated``, ``variant-of::<source_note_id>``, a stable
    ``variantuid::`` (idempotency), and the source's ``topic::``/``concept::``
    tags — so it syncs, activates by topic exactly like its source, and is
    excluded from the Memory score (review-only).

    **AI-off:** with ``provider is None`` this is an immediate no-op that writes
    nothing and blocks nothing (``summary.ai_disabled`` is True).
    """
    summary = GenerateCardSummary()
    if provider is None:
        summary.ai_disabled = True
        return summary

    from anki.notes import NoteId
    from anki.speedrun import (
        BANK_AI_GENERATED_TAG,
        CONCEPT_TAG_PREFIX,
        FIRST_PRINCIPLES_TAG,
        FLASHCARD_NOTETYPE_NAME,
        TOPIC_TAG_PREFIX,
        concepts_of_note,
        topics_of_note,
    )

    sr = col.speedrun
    _, flashcards_deck = sr.ensure_decks()
    basic = col.models.by_name(FLASHCARD_NOTETYPE_NAME)
    assert basic is not None, "stock Basic notetype must exist"

    if note_ids is None:
        note_ids = [int(nid) for nid in col.find_notes(f"tag:{FIRST_PRINCIPLES_TAG}")]

    if grader is None:
        grader = HeuristicCardGrader(heldout_texts=_heldout_texts(col))

    existing = _existing_variant_uids(col)
    new_card_ids: list[Any] = []

    for nid in note_ids:
        note = col.get_note(NoteId(int(nid)))
        # Never rephrase an AI variant (no variants of variants).
        if BANK_AI_GENERATED_TAG in note.tags:
            summary.skipped_ai_source += 1
            continue
        front = str(note["Front"]) if "Front" in note else ""
        back = str(note["Back"]) if "Back" in note else ""
        if not front.strip() or not back.strip():
            summary.rejected_malformed += 1
            continue
        note_concepts = concepts_of_note(note)
        source = SourceCard(
            front=front,
            back=back,
            source=f"first-principles {nid}",
            topics=topics_of_note(note),
            concept=note_concepts[0] if note_concepts else "",
            note_id=int(nid),
        )
        summary.considered += 1
        for i in range(n):
            variant_uid = f"fp-{nid}-v{i}"
            if variant_uid in existing:
                summary.skipped_existing += 1
                continue
            try:
                variant = provider.rephrase_card(source)
            except Exception as exc:  # noqa: BLE001 — one bad call must not abort the batch
                summary.provider_errors += 1
                if not summary.first_error:
                    summary.first_error = f"{type(exc).__name__}: {exc}"
                continue
            grade = grader.grade(source, variant)
            summary._tally(grade.label)
            if grade.label == LABEL_WRONG or grade.score < min_quality:
                summary.blocked += 1
                continue
            new_note = col.new_note(basic)
            new_note["Front"] = variant.front
            new_note["Back"] = variant.back
            tags = [f"{TOPIC_TAG_PREFIX}{t}" for t in source.topics]
            if source.concept:
                tags.append(f"{CONCEPT_TAG_PREFIX}{source.concept}")
            tags.append(BANK_AI_GENERATED_TAG)
            tags.append(f"{VARIANT_OF_TAG_PREFIX}{nid}")
            tags.append(f"{VARIANT_UID_TAG_PREFIX}{variant_uid}")
            new_note.tags = tags
            col.add_note(new_note, flashcards_deck)
            existing.add(variant_uid)
            new_card_ids.extend(c.id for c in new_note.cards())
            summary.written += 1

    # Suspend all new variant cards so they stay inert until a related question
    # is missed (or a coverage sweep activates them) — exactly like their source.
    if new_card_ids:
        col.sched.suspend_cards(new_card_ids)

    return summary


def ai_variant_note_ids(col: anki.collection.Collection) -> list[int]:
    """Note ids of every AI-generated flashcard variant in the collection.

    These are exactly the notes :func:`generate_card_variants` writes — tagged
    ``bank::ai-generated`` — so the caller can count or remove them.
    """
    from anki.speedrun import BANK_AI_GENERATED_TAG

    return [int(nid) for nid in col.find_notes(f'"tag:{BANK_AI_GENERATED_TAG}"')]


def remove_card_variants(col: anki.collection.Collection) -> int:
    """Delete all AI-generated flashcard variants and return how many were removed.

    The inverse of :func:`generate_card_variants`: it removes only notes tagged
    ``bank::ai-generated`` (source first-principles cards and the question bank
    are untouched). Removal goes through ``col.remove_notes`` so it is a single
    undoable operation that syncs like any other deletion.
    """
    from anki.notes import NoteId

    note_ids = ai_variant_note_ids(col)
    if note_ids:
        col.remove_notes([NoteId(nid) for nid in note_ids])
        # Drop the now-orphaned bank::/variant-of::/variantuid:: tags so they
        # neither clutter the sidebar nor linger in the tag registry.
        col.tags.clear_unused_tags()
    return len(note_ids)


# --- Top-level convenience ----------------------------------------------------


def rephrase_question(
    note_fields: dict[str, Any], provider: Provider
) -> RephrasedQuestion:
    """Rephrase one question given its ``SpeedrunQuestion`` field dict.

    A thin, collection-free entry point (used by the eval harness and callers
    that already have field values): build a :class:`SourceQuestion` and delegate
    to ``provider``. The variant always carries a traceable ``source``.
    """
    source = SourceQuestion.from_dict(note_fields)
    return provider.rephrase(source)


class RephraseError(RuntimeError):
    """Raised for provider configuration / output errors (missing key/library,
    non-JSON reply). Never raised on the AI-off path."""
