"""Rubric registry — the single source of truth for every scoring rubric.

A :class:`Rubric` fully describes how to judge ONE scored entity of a given
dimension (function name, variable name, struct/datatype layout, ...): the
prompt template, the 0-10 bands, and which item fields it reads. Consumers
(the JudgeEngine, heavy-test cases, CLIs) never inline prose — they look up a
rubric by id and let it build the question. Adding a new scoring dimension is
a one-place change: register a Rubric here.

Never edit the bands of a rubric in place. Each rubric is versioned; bump
``version`` when the wording/bands change so cached scores from the old rubric
are invalidated (cache keys carry the rubric id INCLUDING its version).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RubricLevel:
    """One scoring band of a rubric (used by tools that introspect rubrics)."""

    lo: int
    hi: int
    label: str
    description: str


@dataclass(frozen=True)
class Rubric:
    """One named, versioned criterion for scoring a recovered entity."""

    id: str                 # stable dimension id, e.g. "function-name"
    version: int            # bump on ANY wording/band change -> cache invalidation
    label: str              # human label, e.g. "recovered function name"
    band_label: str         # what the entity IS, used in the intro, e.g. "function"
    bands: str              # the explicit 0-10 grading bands (inline text)
    prompt: str             # instructions to the judge (pre-bands system intent)
    fields: tuple[str, ...] # item fields referenced by format_question, e.g.
                            # ("true", "recovered"); drives schema hints

    def format_question(self, item: dict[str, Any]) -> str:
        """Build the single-turn judge question from a scored item.

        The item carries ``key`` (stable cache identity) plus the fields this
        rubric declares. Unknown/extra fields are ignored — rubrics are
        decoupled from the evidence sources that produce items.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class _NameRubric(Rubric):
    """Rubric for scoring a single recovered NAME against its original."""

    plan: str = field(
        init=False,
        default=(
            "Decide using ONLY the two values shown. Every value below is one "
            "score you must assign to the RECOVERED value relative to the "
            "original. Do not penalize for formatting, abbreviation, or "
            "stylistic choices that keep the same meaning."
        ),
    )

    def format_question(self, item: dict[str, Any]) -> str:
        true = str(item.get("true", "") or "")
        recovered = str(item.get("recovered", "") or "(none)")
        return (
            f"ORIGINAL {self.band_label}: `{true}`\n"
            f"RECOVERED {self.band_label}: `{recovered}`\n"
            f"{self.plan}\n"
            f"Score the RECOVERED {self.band_label}'s semantic equivalence to "
            f"the ORIGINAL {self.band_label} using this rubric:\n{self.bands}\n"
            'Respond with JSON: {"score": <0-10 int>, "justification": <str>}.'
        )


@dataclass(frozen=True)
class _StructRubric(Rubric):
    """Rubric for scoring a recovered DATATYPE (struct layout) vs original."""

    plan: str = field(
        init=False,
        default=(
            "You are comparing a RECOVERED struct layout against the ORIGINAL "
            "C struct of the same object. Each layout is a list of fields "
            "(offset, name, type). Score how faithfully the recovered layout "
            "captures the original: matching fields at matching offsets with "
            "semantically equivalent names and compatible types is full credit. "
            "Missing, spurious, misplaced, or mis-typed fields lose credit "
            "proportionally. Never penalize for underscore/casing differences "
            "in field names if the meaning is identical."
        ),
    )

    def format_question(self, item: dict[str, Any]) -> str:
        true = _layout(item.get("true"))
        recovered = _layout(item.get("recovered"))
        return (
            f"ORIGINAL struct `{item.get('name', '?')}`:\n{true}\n"
            f"RECOVERED struct `{item.get('recovered_name', item.get('name', '?'))}`:\n"
            f"{recovered or '(no layout recovered)'}\n"
            f"{self.plan}\n"
            f"Score the RECOVERED datatype's fidelity using this rubric:\n{self.bands}\n"
            'Respond with JSON: {"score": <0-10 int>, "justification": <str>}.'
        )


def _layout(value: Any) -> str:
    """Render a struct layout (list of field dicts) as readable text."""
    if not value:
        return "(empty)"
    lines = []
    for f in value or []:
        if not isinstance(f, dict):
            continue
        lines.append(
            f"  +0x{int(f.get('offset', 0) or 0):x}  "
            f"{f.get('name') or f.get('type_str') or '?'}  "
            f"{f.get('type_str') or ''}"
        )
    return "\n".join(lines) if lines else "(empty)"


# ---------------------------------------------------------------------------
# Bands text (shared) — the canonical 0-10 semantic-equivalence rubric.
# ---------------------------------------------------------------------------

_NAME_BANDS = """
  10  exact match (textually identical or effectively identical)
  8-9 semantically equivalent: same intent/role, different wording or style
  5-7 related but incomplete: generic, partial, or only vaguely captures the role
  2-4 wrong or misleading, but plausible (wrong domain / wrong entity)
  0   no rename, blank, or completely unrelated to the true role
"""

_STRUCT_BANDS = """
  10  exact layout equivalent (every field at the correct offset/name/type)
  8-9 all real fields identified, names/types semantically equivalent
       (some formatting differences, no structural errors)
  5-7 core fields identified but layout partially wrong (missing/misplaced
       fields, wrong types, incorrect offsets for a subset)
  2-4 layout recognizable but largely wrong (most fields missing or at wrong
       offsets, types incompatible)
  0   no layout recovered, or completely unrelated to the real struct
"""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def _function_rubric() -> Rubric:
    r = _NameRubric(
        id="function-name", version=1, label="recovered function name",
        band_label="function", bands=_NAME_BANDS,
        prompt="You are a strict, fair grading judge of reverse-engineering "
               "name recovery.",
        fields=("true", "recovered"),
    )
    return r


def _variable_rubric() -> Rubric:
    r = _NameRubric(
        id="variable-name", version=1, label="recovered variable name",
        band_label="variable", bands=_NAME_BANDS,
        prompt="You are a strict, fair grading judge of reverse-engineering "
               "variable name recovery.",
        fields=("true", "recovered"),
    )
    return r


def _struct_rubric() -> Rubric:
    r = _StructRubric(
        id="datatype", version=1, label="recovered datatype (struct layout)",
        band_label="struct layout", bands=_STRUCT_BANDS,
        prompt="You are a strict, fair grading judge of reverse-engineering "
               "type recovery.",
        fields=("true", "recovered", "name", "recovered_name"),
    )
    return r


#: Registry. Map rubric id -> Rubric. External code must NOT construct Rubric
#: objects directly — always go through :data:`RUBRICS`/:func:`get_rubric`.
RUBRICS: dict[str, Rubric] = {
    r.id: r for r in (_function_rubric(), _variable_rubric(), _struct_rubric())
}


def get_rubric(rubric_id: str) -> Rubric:
    """Return a rubric by id, raising KeyError with a helpful list if absent."""
    if rubric_id not in RUBRICS:
        raise KeyError(
            f"unknown rubric {rubric_id!r}; registered rubrics: "
            f"{sorted(RUBRICS)}"
        )
    return RUBRICS[rubric_id]


__all__ = ["Rubric", "RUBRICS", "get_rubric"]
