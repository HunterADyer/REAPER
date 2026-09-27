"""Struct access detector — Deliverable 4.1 (models defined here; see design § 4.1).

Per the submission protocol, ``StructCandidate`` and ``FieldAccess`` are the ONLY
agent-output models that live OUTSIDE ``reaper/harness/submission.py`` — they are
imported from here by ``type_recovery.py`` (4.2), ``context.py`` (3.1) and the
pipeline runner. Do not move them.

The fully implemented ``StructAccessDetector`` (pure analysis, no LLM) is filled
in by the 4.1 implementation; the models below are complete and stable.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FieldAccess(BaseModel):
    """A single struct field access site discovered in HLIL (design § 4.1).

    ``instruction_address`` is a single instruction, NOT a range — the
    ContextAssembler retrieves exactly one HLIL instruction for it.
    """

    offset: int
    size: int
    access_type: str = Field(description="'read' or 'write'")
    function_address: str = Field(description="hex address of the containing function")
    instruction_address: str = Field(description="hex — single instruction, not a range")


class StructCandidate(BaseModel):
    """Group of struct field accesses that likely share one struct type."""

    candidate_id: str = Field(description="unique identifier")
    base_type_hint: str = Field(description="Binja's current type guess for the base pointer")
    accesses: list[FieldAccess]
    functions_involved: list[str] = Field(description="hex addresses")


class StructAccessDetector:
    """Scan HLIL for pointer+offset patterns indicating struct field accesses.

    Pure analysis — no LLM. Implemented in Deliverable 4.1. Walk patterns:
      - ``*(base + offset)``            — Deref of Add
      - ``base->field``                 — HighLevelILStructField / DerefField
      - ``*(base + N)`` where N const   — Deref of Add with constant offset

    Grouping strategy (design § 4.1):
      WITHIN a function: group by base variable (same base = same struct).
      ACROSS functions: group only when Binja assigns the SAME type to the base
        pointers. If Binja can't determine the type, do NOT cross-function
        group — separate candidates. False negatives acceptable; false positive
        grouping is worse.
    """

    def __init__(self, extractor):
        self.extractor = extractor

    def find_struct_accesses(self) -> list[StructCandidate]:
        # Implemented in 4.1 — walks self.extractor's functions' HLIL.
        return []


__all__ = ["StructCandidate", "FieldAccess", "StructAccessDetector"]
