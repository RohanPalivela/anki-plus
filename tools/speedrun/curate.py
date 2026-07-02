# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Deterministic curation for the Speedrun (MCAT) question bank.

The raw bank produced by :mod:`build_question_bank` is large, noisy, and wildly
off the MCAT blueprint (biology/psychology dominate; long MMLU passage dumps
that don't fit a flashcard; malformed items; no fine-grained ``concept``). This
module turns that raw bank into a high-quality, blueprint-shaped, concept-mapped
*served* pool plus a proportional *heldout* pool.

Everything here is **pure, deterministic and offline**:

* no network, no wall-clock, no randomness — the same input always yields the
  same output (stable sorts keyed on ``uid``), so the vendored gzip is
  reproducible and byte-identical across repos;
* it never mutates the importer contract (field names, tag prefixes) — it only
  drops/reorders items and *adds* a ``concept`` value that the importer already
  knows how to read;
* the fine-grained concept vocabulary lives in :data:`CONCEPT_TAXONOMY`, which
  is also the source for the public ``speedrun_concepts.json`` contract other
  workers consume (emitted with :func:`build_taxonomy_json`).

Public API:

* :func:`curate_bank` — ``(questions, blueprint) -> (curated_questions, report)``
* :func:`build_curated_bank` — curate a whole loaded bank dict (updates counts)
* :func:`build_taxonomy_json` — the canonical concept taxonomy contract
* ``python tools/speedrun/curate.py --in-place`` — regenerate the vendored gzip
  (and the concept taxonomy) offline, in every repo, byte-identically.
"""

from __future__ import annotations

import argparse
import gzip
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- Tunable curation thresholds ---------------------------------------------
#
# MAX_SERVED_STEM_CHARS: a served item must read as a *discrete, self-contained
# MCQ* on a single flashcard. Measured stem lengths are median 99 / p90 382, so
# a 600-char cap (~100 words) comfortably keeps the overwhelming majority of
# genuine MCQs while excluding the 271 stems >700 and virtually all MMLU passage
# dumps (max 4671). We never truncate mid-sentence to force-fit: an item that
# can't be a clean short MCQ is dropped from the served pool outright.
MAX_SERVED_STEM_CHARS = 600
#: A stem shorter than this is malformed (e.g. a 3-char fragment), never a real
#: question.
MIN_STEM_CHARS = 15
#: A real MCQ needs at least two distinct answer choices.
MIN_OPTIONS = 2

#: Target size of the curated *served* pool. The blueprint is aspirational and
#: the data is starved for its two highest-weight topics (only ~56 biochemistry
#: and 14 organic-chemistry items exist), so exact blueprint proportions are
#: impossible. We instead size each topic proportional to its weight against
#: this target, capped by what actually exists — tiny topics contribute all
#: their valid items, the rest fill proportionally. With the current data this
#: lands ~1300–1400 high-quality served items (inside the 1200–1800 goal).
TARGET_SERVED_TOTAL = 2000
#: Per-topic floor so a low-weight topic is never squeezed to zero (it still
#: can't exceed what exists). Matches the "pull in all their valid items" rule.
MIN_TOPIC_FLOOR = 25
#: Heldout kept per topic, as a fraction of that topic's curated served count.
#: ~1 in 5 mirrors the generator's ``HELDOUT_MODULUS = 5`` split so the reserved
#: scoring set stays proportional to what students actually practise.
HELDOUT_FRACTION = 0.2

#: Blueprint weights (source of truth: ``anki.speedrun.DEFAULT_MCAT_BLUEPRINT``).
#: Mirrored here so this tool stays importable without a built backend; a test
#: asserts the two never drift apart.
DEFAULT_BLUEPRINT: dict[str, Any] = {
    "topics": [
        {"name": "biochemistry", "weight": 0.25},
        {"name": "biology", "weight": 0.18},
        {"name": "general-chemistry", "weight": 0.13},
        {"name": "organic-chemistry", "weight": 0.10},
        {"name": "physics", "weight": 0.09},
        {"name": "psychology", "weight": 0.15},
        {"name": "sociology", "weight": 0.10},
    ]
}


# --- Concept taxonomy ---------------------------------------------------------
#
# The canonical, MCAT-aligned concept vocabulary. Ordered per topic (blueprint
# order); the first concepts in each topic are the ones that back an existing
# first-principles memory card (see speedrun_first_principles.json) or an
# OpenMCAT tested topic (OPENMCAT_CONCEPT_MAP), so a curated question tagged with
# one links precisely via the gates:: pass. The remaining concepts extend
# coverage to the MMLU-heavy topics that previously had no concept granularity.
#
# ``keywords`` drive deterministic assignment: a question is mapped to the
# concept (within its topic) whose keyword set overlaps its text the most. Slugs
# are kebab-case and stable — they are the contract other workers consume.


@dataclass(frozen=True)
class Concept:
    """A fine-grained MCAT concept within one blueprint topic."""

    id: str
    label: str
    topic: str
    #: Lowercase single words or multiword phrases used for deterministic
    #: keyword-overlap assignment. Phrases match against the normalized text.
    keywords: tuple[str, ...]


# NOTE: order matters — earlier concepts win ties and are round-robined first.
CONCEPT_TAXONOMY: tuple[Concept, ...] = (
    # --- biochemistry (FP/OpenMCAT-backed first) -----------------------------
    Concept(
        "amino-acids",
        "Amino acids & proteins",
        "biochemistry",
        (
            "amino acid",
            "amino",
            "peptide",
            "protein",
            "residue",
            "zwitterion",
            "isoelectric",
            "side chain",
            "alpha helix",
            "polypeptide",
            "proline",
        ),
    ),
    Concept(
        "enzyme-regulation",
        "Enzymes & regulation",
        "biochemistry",
        (
            "enzyme",
            "enzymes",
            "catalytic",
            "substrate",
            "michaelis",
            "kinetics",
            "inhibitor",
            "inhibition",
            "allosteric",
            "cofactor",
            "coenzyme",
            "active site",
            "vmax",
            "km",
        ),
    ),
    Concept(
        "bioenergetics",
        "Bioenergetics",
        "biochemistry",
        (
            "bioenergetics",
            "free energy",
            "gibbs",
            "atp hydrolysis",
            "exergonic",
            "endergonic",
            "thermodynamic",
            "coupled reaction",
            "delta g",
        ),
    ),
    Concept(
        "glycolysis",
        "Glycolysis & carbohydrate metabolism",
        "biochemistry",
        (
            "glycolysis",
            "glucose",
            "pyruvate",
            "gluconeogenesis",
            "fructose",
            "phosphofructokinase",
            "carbohydrate",
            "glycogen",
            "pentose",
            "hexokinase",
        ),
    ),
    Concept(
        "oxidative-phosphorylation",
        "Oxidative phosphorylation",
        "biochemistry",
        (
            "oxidative phosphorylation",
            "electron transport",
            "chemiosmosis",
            "proton gradient",
            "atp synthase",
            "mitochondrial",
            "nadh",
            "fadh",
            "citric acid",
            "krebs",
            "oxidative",
        ),
    ),
    Concept(
        "nucleic-acids",
        "Nucleic acids",
        "biochemistry",
        (
            "nucleic acid",
            "nucleotide",
            "purine",
            "pyrimidine",
            "base pair",
            "double helix",
            "phosphodiester",
            "deoxyribose",
        ),
    ),
    Concept(
        "lipids-membranes",
        "Lipids & membranes",
        "biochemistry",
        (
            "lipid",
            "fatty acid",
            "phospholipid",
            "steroid",
            "cholesterol",
            "triglyceride",
            "membrane bilayer",
            "saturated",
            "unsaturated",
        ),
    ),
    # --- biology -------------------------------------------------------------
    Concept(
        "transcription-translation",
        "Central dogma",
        "biology",
        (
            "transcription",
            "translation",
            "messenger rna",
            "mrna",
            "ribosome",
            "codon",
            "rna polymerase",
            "central dogma",
            "gene expression",
            "trna",
            "splicing",
            "promoter",
        ),
    ),
    Concept(
        "mendelian-genetics",
        "Mendelian genetics",
        "biology",
        (
            "mendelian",
            "allele",
            "genotype",
            "phenotype",
            "dominant",
            "recessive",
            "heterozygous",
            "homozygous",
            "punnett",
            "inheritance",
            "chromosome",
            "genetics",
            "genes",
            "hardy weinberg",
            "mutation",
        ),
    ),
    Concept(
        "membrane-transport",
        "Membrane transport",
        "biology",
        (
            "membrane transport",
            "diffusion",
            "osmosis",
            "osmotic",
            "active transport",
            "passive transport",
            "sodium potassium",
            "ion channel",
            "concentration gradient",
            "permeability",
        ),
    ),
    Concept(
        "cell-biology",
        "Cell structure & organelles",
        "biology",
        (
            "organelle",
            "mitochondria",
            "mitochondrion",
            "nucleus",
            "cytoplasm",
            "endoplasmic reticulum",
            "golgi",
            "lysosome",
            "chloroplast",
            "cytoskeleton",
            "eukaryotic",
            "prokaryotic",
            "cell wall",
        ),
    ),
    Concept(
        "cell-division",
        "Cell division & development",
        "biology",
        (
            "mitosis",
            "meiosis",
            "cell cycle",
            "gamete",
            "fertilization",
            "embryo",
            "embryological",
            "zygote",
            "cell division",
            "chromatid",
            "spindle",
            "cytokinesis",
            "ectoderm",
            "mesoderm",
            "endoderm",
            "germ layer",
        ),
    ),
    Concept(
        "microbiology-immunology",
        "Microbiology & immunology",
        "biology",
        (
            "bacteria",
            "bacterial",
            "virus",
            "viral",
            "virology",
            "pathogen",
            "infection",
            "immune",
            "antibody",
            "antigen",
            "vaccine",
            "lymphocyte",
            "antibiotic",
            "immunity",
            "microbe",
        ),
    ),
    Concept(
        "physiology",
        "Human anatomy & physiology",
        "biology",
        (
            "heart",
            "cardiac",
            "blood",
            "circulatory",
            "respiratory",
            "lung",
            "kidney",
            "renal",
            "nervous system",
            "neuron",
            "hormone",
            "endocrine",
            "muscle",
            "digestive",
            "liver",
            "insulin",
            "physiology",
            "artery",
            "nerve",
            "cranial",
            "spinal",
            "bone",
            "skeletal",
            "joint",
            "gland",
            "anatomy",
            "vertebra",
            "tongue",
            "bladder",
            "ossification",
            "foramen",
            "cavity",
        ),
    ),
    Concept(
        "evolution-ecology",
        "Evolution & ecology",
        "biology",
        (
            "evolution",
            "natural selection",
            "species",
            "population",
            "ecosystem",
            "adaptation",
            "phylogenetic",
            "biodiversity",
            "ecological",
            "darwin",
            "fitness",
        ),
    ),
    # --- general-chemistry ---------------------------------------------------
    Concept(
        "electrochemistry",
        "Electrochemistry",
        "general-chemistry",
        (
            "electrochemistry",
            "electrochemical",
            "oxidation",
            "reduction",
            "redox",
            "anode",
            "cathode",
            "galvanic",
            "electrolytic",
            "faraday",
            "half reaction",
            "cell potential",
            "electrode",
        ),
    ),
    Concept(
        "gas-phase",
        "Gas phase",
        "general-chemistry",
        (
            "ideal gas",
            "gas law",
            "partial pressure",
            "boyle",
            "charles",
            "dalton",
            "mole fraction",
            "kinetic theory",
            "gaseous",
        ),
    ),
    Concept(
        "stoichiometry",
        "Stoichiometry",
        "general-chemistry",
        (
            "stoichiometry",
            "stoichiometric",
            "mole ratio",
            "limiting reagent",
            "molar mass",
            "empirical formula",
            "balanced equation",
            "percent yield",
            "moles",
        ),
    ),
    Concept(
        "acid-base-equilibria",
        "Acid-base equilibria",
        "general-chemistry",
        (
            "acid",
            "base",
            "buffer",
            "titration",
            "henderson",
            "conjugate",
            "dissociation",
            "hydronium",
            "pka",
            "acidic",
            "basic",
            "neutralization",
        ),
    ),
    Concept(
        "atomic-structure",
        "Atomic structure & periodicity",
        "general-chemistry",
        (
            "atomic",
            "electron configuration",
            "orbital",
            "periodic table",
            "isotope",
            "valence",
            "quantum number",
            "electronegativity",
            "ionization energy",
            "proton",
            "neutron",
        ),
    ),
    Concept(
        "chemical-bonding",
        "Chemical bonding",
        "general-chemistry",
        (
            "covalent",
            "ionic bond",
            "lewis structure",
            "molecular geometry",
            "hybridization",
            "vsepr",
            "polar bond",
            "hydrogen bond",
            "bonding",
            "dipole",
        ),
    ),
    Concept(
        "thermochemistry",
        "Thermochemistry",
        "general-chemistry",
        (
            "enthalpy",
            "entropy",
            "calorimetry",
            "heat of reaction",
            "hess",
            "exothermic",
            "endothermic",
            "specific heat",
            "thermochemistry",
        ),
    ),
    Concept(
        "kinetics-equilibrium",
        "Kinetics & equilibrium",
        "general-chemistry",
        (
            "reaction rate",
            "rate law",
            "catalyst",
            "activation energy",
            "equilibrium",
            "le chatelier",
            "reaction quotient",
            "kinetics",
            "rate constant",
        ),
    ),
    Concept(
        "solutions",
        "Solutions & concentration",
        "general-chemistry",
        (
            "solution",
            "molarity",
            "concentration",
            "solubility",
            "dissolve",
            "colligative",
            "solute",
            "solvent",
            "precipitate",
            "dilution",
        ),
    ),
    # --- organic-chemistry ---------------------------------------------------
    Concept(
        "separations-and-purifications",
        "Separations & purifications",
        "organic-chemistry",
        (
            "extraction",
            "chromatography",
            "distillation",
            "separation",
            "purification",
            "partition",
            "recrystallization",
            "solubility",
        ),
    ),
    Concept(
        "carboxylic-acids",
        "Carboxylic acids & derivatives",
        "organic-chemistry",
        (
            "carboxylic",
            "carboxylate",
            "ester",
            "amide",
            "anhydride",
            "acyl",
            "ketone",
            "aldehyde",
            "carbonyl",
        ),
    ),
    Concept(
        "stereochemistry",
        "Stereochemistry",
        "organic-chemistry",
        (
            "stereochemistry",
            "chiral",
            "chirality",
            "enantiomer",
            "diastereomer",
            "stereocenter",
            "optical",
            "racemic",
            "isomer",
            "configuration",
        ),
    ),
    Concept(
        "functional-groups",
        "Functional groups & nomenclature",
        "organic-chemistry",
        (
            "functional group",
            "alcohol",
            "amine",
            "alkane",
            "alkene",
            "alkyne",
            "hydroxyl",
            "aromatic",
            "benzene",
            "nomenclature",
        ),
    ),
    Concept(
        "reaction-mechanisms",
        "Reaction mechanisms",
        "organic-chemistry",
        (
            "nucleophile",
            "electrophile",
            "substitution",
            "elimination",
            "mechanism",
            "carbocation",
            "radical",
            "addition reaction",
            "leaving group",
        ),
    ),
    Concept(
        "spectroscopy",
        "Spectroscopy",
        "organic-chemistry",
        (
            "nmr",
            "infrared",
            "spectroscopy",
            "spectrum",
            "mass spectrometry",
            "absorption",
            "wavelength shift",
        ),
    ),
    # --- physics -------------------------------------------------------------
    Concept(
        "work-energy",
        "Work & energy",
        "physics",
        (
            "work",
            "energy",
            "kinetic energy",
            "potential energy",
            "conservation of energy",
            "joule",
            "power",
            "work energy",
        ),
    ),
    Concept(
        "electrostatics",
        "Electrostatics",
        "physics",
        (
            "electrostatic",
            "coulomb",
            "electric field",
            "electric charge",
            "point charge",
            "electric potential",
            "voltage",
            "capacitor",
        ),
    ),
    Concept(
        "circuits",
        "Circuits",
        "physics",
        (
            "circuit",
            "resistor",
            "resistance",
            "current",
            "ohm",
            "voltage",
            "capacitance",
            "series",
            "parallel",
            "ammeter",
        ),
    ),
    Concept(
        "optics",
        "Optics",
        "physics",
        (
            "optics",
            "lens",
            "mirror",
            "refraction",
            "reflection",
            "focal length",
            "image",
            "diffraction",
            "converging",
            "diverging",
        ),
    ),
    Concept(
        "kinematics-dynamics",
        "Kinematics & dynamics",
        "physics",
        (
            "velocity",
            "acceleration",
            "force",
            "newton",
            "momentum",
            "friction",
            "projectile",
            "motion",
            "kinematics",
            "torque",
            "gravity",
            "mass",
        ),
    ),
    Concept(
        "fluids",
        "Fluids",
        "physics",
        (
            "fluid",
            "pressure",
            "buoyancy",
            "density",
            "bernoulli",
            "viscosity",
            "hydrostatic",
            "flow rate",
            "archimedes",
        ),
    ),
    Concept(
        "thermal-physics",
        "Thermal physics",
        "physics",
        (
            "temperature",
            "thermal",
            "heat transfer",
            "thermodynamics",
            "entropy",
            "ideal gas",
            "conduction",
            "convection",
        ),
    ),
    Concept(
        "waves-sound",
        "Waves & sound",
        "physics",
        (
            "wave",
            "sound",
            "frequency",
            "wavelength",
            "doppler",
            "resonance",
            "amplitude",
            "oscillation",
            "harmonic",
            "pitch",
        ),
    ),
    Concept(
        "magnetism",
        "Magnetism & electromagnetism",
        "physics",
        (
            "magnetic",
            "magnetism",
            "magnetic field",
            "induction",
            "electromagnetic",
            "solenoid",
            "flux",
            "lorentz",
        ),
    ),
    Concept(
        "astronomy",
        "Astronomy & modern physics",
        "physics",
        (
            "astronomy",
            "star",
            "planet",
            "orbit",
            "galaxy",
            "universe",
            "telescope",
            "cosmic",
            "nuclear",
            "radioactive",
            "relativity",
            "photon",
            "mars",
            "moon",
            "earth",
            "comet",
            "supernova",
            "constellation",
            "planetary",
            "brightness",
            "solar",
        ),
    ),
    # --- psychology ----------------------------------------------------------
    Concept(
        "sensory-processing",
        "Sensation & perception",
        "psychology",
        (
            "sensory",
            "sensation",
            "perception",
            "perceptual",
            "threshold",
            "stimulus",
            "retina",
            "cochlea",
            "weber",
            "signal detection",
            "transduction",
        ),
    ),
    Concept(
        "associative-learning",
        "Learning",
        "psychology",
        (
            "conditioning",
            "classical conditioning",
            "operant",
            "reinforcement",
            "punishment",
            "pavlov",
            "skinner",
            "extinction",
            "learning",
            "stimulus response",
            "reflex",
        ),
    ),
    Concept(
        "memory",
        "Memory",
        "psychology",
        (
            "memory",
            "encoding",
            "retrieval",
            "recall",
            "forgetting",
            "working memory",
            "long term memory",
            "amnesia",
            "rehearsal",
            "recognition",
        ),
    ),
    Concept(
        "cognition-language",
        "Cognition & language",
        "psychology",
        (
            "cognition",
            "cognitive",
            "problem solving",
            "language",
            "reasoning",
            "decision making",
            "heuristic",
            "intelligence",
            "thinking",
            "concept formation",
        ),
    ),
    Concept(
        "emotion-motivation",
        "Emotion & motivation",
        "psychology",
        (
            "emotion",
            "emotional",
            "motivation",
            "drive",
            "arousal",
            "stress",
            "reward",
            "affect",
            "hunger",
            "instinct",
        ),
    ),
    Concept(
        "developmental-psychology",
        "Developmental psychology",
        "psychology",
        (
            "development",
            "developmental",
            "piaget",
            "attachment",
            "adolescence",
            "erikson",
            "cognitive development",
            "child development",
            "moral development",
        ),
    ),
    Concept(
        "personality",
        "Personality",
        "psychology",
        (
            "personality",
            "trait",
            "freud",
            "psychoanalytic",
            "psychodynamic",
            "ego",
            "self concept",
            "temperament",
            "big five",
        ),
    ),
    Concept(
        "psychological-disorders",
        "Psychological disorders",
        "psychology",
        (
            "disorder",
            "depression",
            "anxiety",
            "schizophrenia",
            "psychotherapy",
            "mental illness",
            "diagnosis",
            "phobia",
            "bipolar",
            "psychopathology",
        ),
    ),
    Concept(
        "biological-psychology",
        "Biological bases of behavior",
        "psychology",
        (
            "neuron",
            "neurotransmitter",
            "dopamine",
            "serotonin",
            "brain",
            "cortex",
            "amygdala",
            "hippocampus",
            "nervous system",
            "hormone",
            "synapse",
        ),
    ),
    Concept(
        "social-psychology",
        "Social psychology",
        "psychology",
        (
            "attitude",
            "conformity",
            "obedience",
            "prejudice",
            "attribution",
            "bias",
            "group behavior",
            "persuasion",
            "stereotype",
            "bystander",
        ),
    ),
    # --- sociology -----------------------------------------------------------
    Concept(
        "theoretical-approaches",
        "Theoretical approaches",
        "sociology",
        (
            "functionalism",
            "conflict theory",
            "symbolic interactionism",
            "paradigm",
            "durkheim",
            "weber",
            "marx",
            "social theory",
            "macrosociology",
            "microsociology",
        ),
    ),
    Concept(
        "social-class",
        "Social class & stratification",
        "sociology",
        (
            "social class",
            "stratification",
            "socioeconomic",
            "social mobility",
            "inequality",
            "poverty",
            "status",
            "caste",
            "wealth",
            "class",
        ),
    ),
    Concept(
        "social-institutions",
        "Social institutions",
        "sociology",
        (
            "institution",
            "family",
            "religion",
            "education system",
            "government",
            "economy",
            "bureaucracy",
            "healthcare system",
            "marriage",
        ),
    ),
    Concept(
        "demographics",
        "Demographics & population",
        "sociology",
        (
            "demographic",
            "population growth",
            "urbanization",
            "migration",
            "fertility",
            "mortality",
            "census",
            "demography",
            "aging population",
        ),
    ),
    Concept(
        "culture-socialization",
        "Culture & socialization",
        "sociology",
        (
            "culture",
            "cultural",
            "norm",
            "socialization",
            "social role",
            "values",
            "belief",
            "subculture",
            "deviance",
            "social control",
        ),
    ),
    Concept(
        "race-gender",
        "Race, ethnicity & gender",
        "sociology",
        (
            "race",
            "ethnicity",
            "gender",
            "discrimination",
            "minority",
            "sexism",
            "racism",
            "ethnic",
            "gender role",
            "feminism",
        ),
    ),
)


# --- text helpers (self-contained; no anki import needed) --------------------

_LETTERS = "ABCDEF"


def _normalized_options(raw: Any) -> list[str]:
    """Collapse whitespace per option and drop empties, preserving order."""
    out: list[str] = []
    for opt in raw or []:
        text = " ".join(str(opt).split())
        if text:
            out.append(text)
    return out


def _correct_index(correct: Any, num_options: int) -> int:
    """Resolve ``correct`` to a 0-based option index, or -1 if unresolvable.

    Accepts a letter (A-F, case-insensitive) or a 1-based number. Mirrors
    ``anki.speedrun.correct_index`` so the curated bank always imports cleanly.
    """
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


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric content tokens (len >= 3) for keyword overlap."""
    tokens: set[str] = set()
    current: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            current.append(ch)
        elif current:
            tokens.add("".join(current))
            current = []
    if current:
        tokens.add("".join(current))
    return {t for t in tokens if len(t) >= 3}


def _looks_like_passage(stem: str) -> bool:
    """True for multi-paragraph / passage-style stems that don't fit an MCQ."""
    if "Passage:" in stem or "Passage —" in stem or "Passage -" in stem:
        return True
    if "Table —" in stem or "Figure:" in stem:
        return True
    # Two or more blank-line-separated paragraphs in a long stem reads as a
    # passage dump rather than a discrete question.
    if stem.count("\n\n") >= 2 and len(stem) > 300:
        return True
    return False


def _trim_stem(stem: str) -> str:
    """Light, lossless cleanup: strip surrounding whitespace and collapse runs
    of blank lines. Never truncates content mid-sentence."""
    lines = [line.rstrip() for line in stem.strip().splitlines()]
    cleaned: list[str] = []
    blank = False
    for line in lines:
        if line:
            cleaned.append(line)
            blank = False
        elif not blank:
            cleaned.append("")
            blank = True
    return "\n".join(cleaned).strip()


# --- concept assignment ------------------------------------------------------

_CONCEPTS_BY_TOPIC: dict[str, tuple[Concept, ...]] = {}
for _c in CONCEPT_TAXONOMY:
    _CONCEPTS_BY_TOPIC.setdefault(_c.topic, ())
    _CONCEPTS_BY_TOPIC[_c.topic] = _CONCEPTS_BY_TOPIC[_c.topic] + (_c,)


def assign_concept(item: dict[str, Any]) -> str:
    """Deterministically map a question to a concept slug within its topic.

    Scores each of the topic's concepts by how many of its keywords appear in
    the question text (stem + options + explanation); returns the best-scoring
    slug, breaking ties by taxonomy order. Returns ``""`` when nothing matches
    (the item stays valid, topic-only practice).
    """
    topics = item.get("topics", [])
    topic = topics[0] if topics else ""
    concepts = _CONCEPTS_BY_TOPIC.get(topic)
    if not concepts:
        return ""
    text = " ".join(
        [
            str(item.get("stem", "")),
            " ".join(str(o) for o in item.get("options", [])),
            str(item.get("explanation", "")),
        ]
    )
    lowered = text.lower()
    toks = _tokens(text)
    best_slug = ""
    best_score = 0
    for concept in concepts:
        score = 0
        for kw in concept.keywords:
            if " " in kw:
                if kw in lowered:
                    score += 1
            elif kw in toks:
                score += 1
        if score > best_score:
            best_score = score
            best_slug = concept.id
    return best_slug


# --- validity + ordering -----------------------------------------------------


def _drop_reason(item: dict[str, Any]) -> str | None:
    """Return a malformed-drop reason, or ``None`` if the item is well-formed.

    Well-formed only checks structural validity (rule a); length/passage checks
    that only bar an item from the *served* pool are handled separately.
    """
    stem = str(item.get("stem", "")).strip()
    if not stem:
        return "empty-stem"
    if len(stem) < MIN_STEM_CHARS:
        return "short-stem"
    options = _normalized_options(item.get("options"))
    if len(options) < MIN_OPTIONS:
        return "few-options"
    if len({o.lower() for o in options}) < len(options):
        return "duplicate-options"
    if _correct_index(item.get("correct"), len(options)) < 0:
        return "unresolvable-correct"
    return None


def _servable_reason(item: dict[str, Any]) -> str | None:
    """Return why a well-formed item can't be *served*, or ``None`` if it can."""
    stem = str(item.get("stem", ""))
    if len(stem) > MAX_SERVED_STEM_CHARS:
        return "over-length"
    if _looks_like_passage(stem):
        return "passage-style"
    return None


def _quality_key(item: dict[str, Any]) -> tuple[bool, bool, str]:
    """Stable per-item quality order: explanation first, then OpenMCAT (the
    concept+explanation-rich spine), then uid for full determinism."""
    has_expl = bool(str(item.get("explanation", "")).strip())
    is_openmcat = item.get("origin") == "openmcat"
    return (not has_expl, not is_openmcat, str(item.get("uid", "")))


def _difficulty_band(item: dict[str, Any]) -> str:
    try:
        b = float(item.get("difficulty_b", 0.0))
    except (TypeError, ValueError):
        b = 0.0
    return "-" if b < 0 else ("+" if b > 0 else "0")


def _difficulty_interleave(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin easy/medium/hard so a capped slice isn't all one difficulty,
    preserving quality order within each band."""
    bands: dict[str, list[dict[str, Any]]] = {"0": [], "-": [], "+": []}
    for it in items:
        bands[_difficulty_band(it)].append(it)
    order = ("0", "-", "+")
    out: list[dict[str, Any]] = []
    cursors = {k: 0 for k in order}
    remaining = len(items)
    while remaining:
        for k in order:
            if cursors[k] < len(bands[k]):
                out.append(bands[k][cursors[k]])
                cursors[k] += 1
                remaining -= 1
    return out


def _topic_ordered(items: list[dict[str, Any]], topic: str) -> list[dict[str, Any]]:
    """Deterministically order one topic's candidates so a top-N slice is a
    concept-spread, quality-first, difficulty-spread selection.

    Buckets by assigned ``concept`` (taxonomy order; the no-concept bucket last),
    orders each bucket quality-first then difficulty-interleaved, then
    round-robins the buckets so no single concept dominates the early slots.
    """
    concept_ids = [c.id for c in _CONCEPTS_BY_TOPIC.get(topic, ())]
    buckets: dict[str, list[dict[str, Any]]] = {cid: [] for cid in concept_ids}
    buckets[""] = []
    for it in items:
        buckets.setdefault(it.get("concept", ""), []).append(it)
    for key, bucket in buckets.items():
        bucket.sort(key=_quality_key)
        buckets[key] = _difficulty_interleave(bucket)

    bucket_order = [cid for cid in concept_ids if buckets.get(cid)]
    if buckets.get(""):
        bucket_order.append("")
    out: list[dict[str, Any]] = []
    cursors = {k: 0 for k in bucket_order}
    remaining = sum(len(buckets[k]) for k in bucket_order)
    while remaining:
        for k in bucket_order:
            bucket = buckets[k]
            if cursors[k] < len(bucket):
                out.append(bucket[cursors[k]])
                cursors[k] += 1
                remaining -= 1
    return out


def _blueprint_weights(blueprint: dict[str, Any]) -> dict[str, float]:
    return {t["name"]: float(t["weight"]) for t in blueprint.get("topics", [])}


def _allocate(
    weights: dict[str, float], available: dict[str, int], target_total: int
) -> dict[str, int]:
    """Per-topic served target = weight-proportional share of ``target_total``
    (floored so no topic vanishes), capped by what actually exists."""
    total_w = sum(weights.values()) or 1.0
    out: dict[str, int] = {}
    for topic, w in weights.items():
        raw = max(round(w / total_w * target_total), MIN_TOPIC_FLOOR)
        out[topic] = min(available.get(topic, 0), raw)
    return out


# --- the curation entrypoint -------------------------------------------------


def curate_bank(
    questions: list[dict[str, Any]], blueprint: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Curate a raw question list into a blueprint-shaped, concept-mapped bank.

    Pure and deterministic: the same input list always yields the same
    ``(curated_questions, report)``. See the module docstring for the rules.
    """
    blueprint = blueprint if blueprint is not None else DEFAULT_BLUEPRINT
    weights = _blueprint_weights(blueprint)
    topics = list(weights)

    before_served = sum(1 for q in questions if q.get("pool") != "heldout")
    before_heldout = len(questions) - before_served
    before_by_topic: dict[str, int] = {}
    before_longest = 0
    for q in questions:
        before_longest = max(before_longest, len(str(q.get("stem", ""))))
        for t in q.get("topics", []):
            before_by_topic[t] = before_by_topic.get(t, 0) + 1

    dropped: dict[str, int] = {}

    def _drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    # Pass 1: keep only well-formed items, normalize them, and assign a concept.
    served_by_topic: dict[str, list[dict[str, Any]]] = {t: [] for t in topics}
    heldout_by_topic: dict[str, list[dict[str, Any]]] = {t: [] for t in topics}
    for raw in questions:
        reason = _drop_reason(raw)
        if reason:
            _drop(reason)
            continue
        item = dict(raw)
        item["stem"] = _trim_stem(str(item.get("stem", "")))
        item["options"] = _normalized_options(item.get("options"))
        item["concept"] = assign_concept(item)
        item_topics = item.get("topics", [])
        topic = item_topics[0] if item_topics else ""
        if topic not in weights:
            _drop("off-blueprint-topic")
            continue
        if raw.get("pool") == "heldout":
            heldout_by_topic[topic].append(item)
            continue
        servable = _servable_reason(item)
        if servable:
            _drop(servable)
            continue
        served_by_topic[topic].append(item)

    # Pass 2: allocate served slots toward the blueprint and select per topic.
    available = {t: len(served_by_topic[t]) for t in topics}
    served_slots = _allocate(weights, available, TARGET_SERVED_TOTAL)

    curated: list[dict[str, Any]] = []
    served_topic_counts: dict[str, int] = {}
    served_concept_counts: dict[str, int] = {}
    selected_served: dict[str, list[dict[str, Any]]] = {}
    for topic in topics:
        ordered = _topic_ordered(served_by_topic[topic], topic)
        chosen = ordered[: served_slots[topic]]
        selected_served[topic] = chosen
        # everything valid-but-not-selected is trimmed away
        dropped["not-selected"] = dropped.get("not-selected", 0) + (
            len(ordered) - len(chosen)
        )
        for it in chosen:
            it = dict(it)
            it["pool"] = "served"
            curated.append(it)
            served_topic_counts[topic] = served_topic_counts.get(topic, 0) + 1
            concept = it.get("concept") or "(none)"
            served_concept_counts[concept] = served_concept_counts.get(concept, 0) + 1

    # Pass 3: proportional heldout per topic (fraction of curated served).
    heldout_slots: dict[str, int] = {}
    heldout_topic_counts: dict[str, int] = {}
    for topic in topics:
        want = round(HELDOUT_FRACTION * len(selected_served[topic]))
        slots = min(len(heldout_by_topic[topic]), max(want, 0))
        heldout_slots[topic] = slots
        ordered = _topic_ordered(heldout_by_topic[topic], topic)
        for it in ordered[:slots]:
            it = dict(it)
            it["pool"] = "heldout"
            curated.append(it)
            heldout_topic_counts[topic] = heldout_topic_counts.get(topic, 0) + 1
        dropped["heldout-trimmed"] = dropped.get("heldout-trimmed", 0) + (
            len(heldout_by_topic[topic]) - slots
        )

    after_served = sum(1 for q in curated if q["pool"] == "served")
    served_with_concept = sum(
        1 for q in curated if q["pool"] == "served" and q.get("concept")
    )
    after_longest = max(
        (len(q["stem"]) for q in curated if q["pool"] == "served"), default=0
    )

    report: dict[str, Any] = {
        "before": {
            "total": len(questions),
            "served": before_served,
            "heldout": before_heldout,
            "by_topic": dict(sorted(before_by_topic.items())),
            "longest_stem": before_longest,
        },
        "after": {
            "total": len(curated),
            "served": after_served,
            "heldout": len(curated) - after_served,
            "served_by_topic": dict(sorted(served_topic_counts.items())),
            "heldout_by_topic": dict(sorted(heldout_topic_counts.items())),
            "served_by_concept": dict(sorted(served_concept_counts.items())),
            "longest_served_stem": after_longest,
            "concept_coverage_pct": round(100.0 * served_with_concept / after_served, 1)
            if after_served
            else 0.0,
        },
        "dropped": dict(sorted(dropped.items())),
        "served_targets": dict(sorted(served_slots.items())),
        "heldout_targets": dict(sorted(heldout_slots.items())),
        "available_served": dict(sorted(available.items())),
    }
    return curated, report


# --- whole-bank + taxonomy helpers -------------------------------------------


def _counts_block(questions: list[dict[str, Any]]) -> dict[str, int]:
    by_origin: dict[str, int] = {}
    by_pool: dict[str, int] = {}
    by_topic: dict[str, int] = {}
    by_concept: dict[str, int] = {}
    for q in questions:
        by_origin[q.get("origin", "unknown")] = (
            by_origin.get(q.get("origin", "unknown"), 0) + 1
        )
        by_pool[q.get("pool", "served")] = by_pool.get(q.get("pool", "served"), 0) + 1
        for t in q.get("topics", []):
            by_topic[t] = by_topic.get(t, 0) + 1
        if concept := q.get("concept"):
            by_concept[concept] = by_concept.get(concept, 0) + 1
    counts = {"total": len(questions)}
    counts.update({f"origin:{k}": v for k, v in sorted(by_origin.items())})
    counts.update({f"pool:{k}": v for k, v in sorted(by_pool.items())})
    counts.update({f"topic:{k}": v for k, v in sorted(by_topic.items())})
    counts.update({f"concept:{k}": v for k, v in sorted(by_concept.items())})
    return counts


def build_curated_bank(
    bank: dict[str, Any], blueprint: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Curate a whole loaded bank dict, returning ``(new_bank, report)``.

    Preserves every top-level field except ``questions`` and ``counts``, so the
    schema version, attribution and (deterministic) ``generated_at`` are kept
    verbatim — re-running on the same input is byte-stable.
    """
    curated, report = curate_bank(bank.get("questions", []), blueprint)
    new_bank = dict(bank)
    new_bank["questions"] = curated
    new_bank["counts"] = _counts_block(curated)
    return new_bank, report


def build_taxonomy_json() -> dict[str, Any]:
    """The canonical concept-taxonomy contract: an ordered ``concepts`` list of
    ``{id, label, topic}`` grouped by blueprint topic order. This is what other
    workers consume — keyword rules stay internal to this module."""
    topic_order = [t["name"] for t in DEFAULT_BLUEPRINT["topics"]]
    ordered = sorted(CONCEPT_TAXONOMY, key=lambda c: (topic_order.index(c.topic),))
    # ``sorted`` is stable, so within-topic taxonomy order is preserved.
    return {
        "schema_version": 1,
        "note": (
            "Canonical MCAT concept taxonomy for the Speedrun fork. Each concept "
            "is a stable kebab-case slug within one blueprint topic; curated "
            "served questions are mapped to these slugs (concept:: tags) so "
            "coverage, gating and scoring share one vocabulary. Generated by "
            "tools/speedrun/curate.py from CONCEPT_TAXONOMY."
        ),
        "concepts": [{"id": c.id, "label": c.label, "topic": c.topic} for c in ordered],
    }


# --- reproducible IO ---------------------------------------------------------


def _bank_gz_bytes(bank: dict[str, Any]) -> bytes:
    """Serialize a bank dict to reproducible gzip bytes (mtime=0, level 9)."""
    import io

    payload = json.dumps(bank, ensure_ascii=False).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as fh:
        fh.write(payload)
    return buf.getvalue()


def _taxonomy_bytes(taxonomy: dict[str, Any]) -> bytes:
    return (json.dumps(taxonomy, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# Vendored data paths, relative to this file, in both repos. The Android backend
# mirror vendors byte-identical COPIES of the desktop data files.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BANK_REL = Path("pylib") / "anki" / "data" / "speedrun_question_bank.json.gz"
_CONCEPTS_REL = Path("pylib") / "anki" / "data" / "speedrun_concepts.json"
#: Sibling Android backend repo that mirrors the desktop data files.
_ANDROID_BACKEND = _REPO_ROOT.parent / "Anki-Android-Backend" / "anki"


def _target_roots() -> list[Path]:
    roots = [_REPO_ROOT]
    if (_ANDROID_BACKEND / _BANK_REL).exists():
        roots.append(_ANDROID_BACKEND)
    return roots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Read the vendored bank, curate it offline, and write the curated "
        "bank + concept taxonomy back to every repo (byte-identically).",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Print the curation report without writing anything.",
    )
    args = parser.parse_args()

    bank_path = _REPO_ROOT / _BANK_REL
    with gzip.open(bank_path, "rt", encoding="utf-8") as fh:
        bank = json.load(fh)

    new_bank, report = build_curated_bank(bank)
    print(json.dumps(report, indent=2))

    if args.report_only:
        return
    if not args.in_place:
        print("\nNothing written (pass --in-place to regenerate the vendored files).")
        return

    bank_bytes = _bank_gz_bytes(new_bank)
    taxonomy_bytes = _taxonomy_bytes(build_taxonomy_json())
    for root in _target_roots():
        gz_path = root / _BANK_REL
        concepts_path = root / _CONCEPTS_REL
        gz_path.parent.mkdir(parents=True, exist_ok=True)
        gz_path.write_bytes(bank_bytes)
        concepts_path.write_bytes(taxonomy_bytes)
        print(f"Wrote {gz_path} ({len(bank_bytes)} bytes)")
        print(f"Wrote {concepts_path} ({len(taxonomy_bytes)} bytes)")


if __name__ == "__main__":
    main()
