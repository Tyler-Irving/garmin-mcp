"""Resolve free-text exercise names to Garmin's (category, exerciseName) enums.

Garmin strength steps reference an exercise by a fixed two-level enum
(``category`` + ``exerciseName``) drawn from ``data/exercise_taxonomy.json``
(47 categories, 1510 exercises, scraped from Garmin Connect). A name that does
not match exactly still uploads (HTTP 200) but renders as a generic "Unknown"
step with no animation on the watch — a silent failure. So we resolve up front
and surface the match + confidence so a human can veto bad mappings in preview.

The matcher is deterministic (no network, no LLM): alias expansion + token
overlap + difflib ratio over the normalised enum strings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Literal

_TAXONOMY_PATH = Path(__file__).parent / "data" / "exercise_taxonomy.json"

Confidence = Literal["exact", "high", "medium", "low", "none"]

# Common gym shorthand → words that appear in Garmin's enum tokens. Applied
# during normalisation before tokenising. Keys are matched as whole words.
# NB: do NOT split compound enum tokens (e.g. "pulldown" stays one word, since
# the enum is LAT_PULLDOWN); only map to Garmin's actual vocabulary.
_ALIASES: dict[str, str] = {
    "db": "dumbbell",
    "bb": "barbell",
    "kb": "kettlebell",
    "ohp": "overhead press",
    "rdl": "romanian deadlift",
    "sldl": "stiff leg deadlift",
    "bss": "bulgarian split squat",
    "cgbp": "close grip bench press",
    "ez": "ez bar",
    "pushdown": "pressdown",  # Garmin's vocabulary uses "pressdown"
    "tricep": "triceps",
    "bicep": "biceps",
    "calves": "calf",
    "ext": "extension",
    "raises": "raise",
    "curls": "curl",
    "rows": "row",
    "presses": "press",
}

# Noise words dropped from the query before scoring.
_STOP = {"the", "a", "an", "of", "with", "and", "to", "or", "your", "x"}

# Hand-curated mappings for names the fuzzy matcher gets wrong or that have no
# clean catalog entry but a clear best substitute. Keyed by the normalised,
# space-joined token string (see ``_normalise``). Deliberately conservative:
# movements with no honest Garmin equivalent (e.g. Pallof / anti-rotation) are
# left to flag for review rather than silently mapped to a different movement.
_OVERRIDES: dict[str, tuple[str, str]] = {
    "rear delt fly": ("LATERAL_RAISE", "BENT_OVER_LATERAL_RAISE"),
    "suitcase carry": ("CARRY", "FARMERS_CARRY"),
}

# Categories that are not mainstream free-weight/machine lifting. A correct
# exerciseName landing in one of these is usually a worse choice than the same
# movement in a standard category, so we discount their score.
_JUNK_CATEGORIES: frozenset[str] = frozenset(
    {
        "BANDED_EXERCISES",
        "SANDBAG",
        "SUSPENSION",
        "TIRE",
        "SLED",
        "SLEDGE_HAMMER",
        "BATTLE_ROPE",
        "LADDER",
        "FLOOR_CLIMB",
        "STAIR_STEPPER",
        "ELLIPTICAL",
        "INDOOR_BIKE",
        "BIKE_OUTDOOR",
        "RUN",
        "RUN_INDOOR",
        "CARDIO",
        "PLYO",
    }
)
_JUNK_PENALTY = 0.82


@dataclass(frozen=True)
class ResolvedExercise:
    query: str
    category: str | None
    exercise_name: str | None
    confidence: Confidence
    score: float
    matched_label: str | None  # human-readable, e.g. "Barbell Bench Press"

    @property
    def is_usable(self) -> bool:
        """True if confident enough to send to Garmin without manual review."""
        return self.confidence in ("exact", "high")


def _normalise(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)  # drop punctuation incl. "↔", "/"
    tokens: list[str] = []
    for raw in text.split():
        word = _ALIASES.get(raw, raw)
        for part in word.split():
            if part and part not in _STOP:
                tokens.append(part)
    return tokens


def _enum_tokens(enum_name: str) -> list[str]:
    return [t for t in enum_name.lower().split("_") if t and t not in _STOP]


def _label(enum_name: str) -> str:
    return enum_name.replace("_", " ").title()


def _score(query_tokens: list[str], cand_tokens: list[str]) -> float:
    """Blend token-set overlap with a sequence ratio over the joined strings."""
    if not query_tokens or not cand_tokens:
        return 0.0
    qset, cset = set(query_tokens), set(cand_tokens)
    overlap = len(qset & cset)
    # Coverage of the query (did we explain the user's words?) and of the
    # candidate (did we not bring in a much longer/odd variant?).
    q_cov = overlap / len(qset)
    c_cov = overlap / len(cset)
    token_score = (2 * q_cov + c_cov) / 3
    seq = SequenceMatcher(None, " ".join(query_tokens), " ".join(cand_tokens)).ratio()
    return 0.7 * token_score + 0.3 * seq


@lru_cache(maxsize=1)
def _index() -> list[tuple[str, str, list[str]]]:
    """Return [(category, exercise_name, cand_tokens)] for every catalog entry."""
    data = json.loads(_TAXONOMY_PATH.read_text())
    out: list[tuple[str, str, list[str]]] = []
    for category, cat in data["categories"].items():
        for exercise_name in cat["exercises"]:
            out.append((category, exercise_name, _enum_tokens(exercise_name)))
    return out


def _confidence(score: float) -> Confidence:
    if score >= 0.99:
        return "exact"
    if score >= 0.72:
        return "high"
    if score >= 0.55:
        return "medium"
    if score >= 0.40:
        return "low"
    return "none"


def resolve_exercise(query: str) -> ResolvedExercise:
    """Best-effort map a free-text exercise name to a Garmin (category, name)."""
    q_tokens = _normalise(query)

    override = _OVERRIDES.get(" ".join(q_tokens))
    if override is not None:
        category, exercise_name = override
        return ResolvedExercise(
            query=query,
            category=category,
            exercise_name=exercise_name,
            confidence="high",
            score=1.0,
            matched_label=_label(exercise_name),
        )

    best: tuple[float, str, str] | None = None
    for category, exercise_name, cand_tokens in _index():
        s = _score(q_tokens, cand_tokens)
        if category in _JUNK_CATEGORIES:
            s *= _JUNK_PENALTY
        # Tie-break toward shorter (more generic) enum names.
        if best is None or s > best[0] or (s == best[0] and len(exercise_name) < len(best[2])):
            best = (s, category, exercise_name)

    if best is None:
        return ResolvedExercise(query, None, None, "none", 0.0, None)

    score, category, exercise_name = best
    conf = _confidence(score)
    if conf == "none":
        return ResolvedExercise(query, None, None, "none", score, None)
    return ResolvedExercise(
        query=query,
        category=category,
        exercise_name=exercise_name,
        confidence=conf,
        score=round(score, 3),
        matched_label=_label(exercise_name),
    )
