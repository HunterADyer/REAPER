"""Submission protocol — Deliverable 3.3.

Pydantic models for all agent I/O. These double as vLLM structured output
schemas (``model_json_schema()`` -> OpenAI ``response_format`` json_schema —
the only constrained-decoding mechanism the live :8035 build honors, verified
2026-09-27). Also provides
``get_schema()`` and ``parse_response()`` helpers.

Every model here is referenced by agents, dispatchers, and the harness — do
not move models to other modules. The only agent-output models NOT in this
module are ``StructCandidate``/``FieldAccess`` (struct_detector.py) and
``LLMTransientError`` (llm_client.py).
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

TRUTH_LEVELS = Literal[
    "speculation", "inferred", "low_confidence", "mid_confidence", "high_confidence"
]

_TRUTH_LEVEL_VALUES = [
    "speculation", "inferred", "low_confidence", "mid_confidence", "high_confidence"
]


class Rename(BaseModel):
    node_id: str              # Neo4j node id (e.g., "0x1400:var_18")
    llm_name: str             # verbose pothole_case
    canon_name: str           # human-readable
    justification: str        # free-text reasoning


class EvidenceLink(BaseModel):
    address_start: str        # hex
    address_end: str          # hex
    description: str          # what this range shows


class Claim(BaseModel):
    function_address: str
    claim_text: str
    evidence: list[EvidenceLink]


class Submission(BaseModel):
    renames: list[Rename] = []
    claims: list[Claim] = []


class NameDecision(BaseModel):
    """One approve-or-rename decision for a single naming entity.

    Produced by the xhigh RatifyNamesAgent (the deterministic Pass-1 ratify
    pass). Each decision is EVIDENCE-GROUNDED: if a name is disapproved, the
    agent must cite the HLIL/address range and/or graph refs that show the
    entity's real role, then propose exactly ONE replacement. There is no
    critic retry loop — the decision is final and applied once.
    """

    entity: Literal["function", "variable", "argument"] = "variable"
    node_id: str                    # stable id: "0x<fn>:<name>" or "0x<fn>"
    decision: Literal["approve", "rename"] = "approve"
    current_name: str = ""
    llm_name: str = ""              # pothole_case (rename only)
    canon_name: str = ""            # readable (rename only)
    justification: str = ""          # required on rename; optional on approve
    evidence: list[EvidenceLink] = []  # required on rename (graph/HLIL grounding)
    confidence: TRUTH_LEVELS = "mid_confidence"


class RatifyOutput(BaseModel):
    """Batched per-function output of the ratify pass (Pass 1, xhigh)."""

    decisions: list[NameDecision] = []
    function_summary: str = ""


class CriticVerdict(BaseModel):
    truth_level: TRUTH_LEVELS  # only used when accepted=True; ignored on rejection
    accepted: bool
    feedback: str


class TaskContextSpec(BaseModel):
    """Typed context specification — avoids bare dict in structured output."""

    functions: list[str] = []           # hex addresses to include
    include_callees: bool = False
    include_claims: bool = False
    include_neighborhood: bool = False


class TaskSpec(BaseModel):
    """Used by both review agent and investigation agent to create tasks.
    Maps directly to TodoLedger.create_task() parameters."""

    description: str
    start_position: str       # hex address
    goal: str
    graph_refs: list[str] = []
    context_spec: TaskContextSpec = TaskContextSpec()


class ReviewOutput(BaseModel):
    """Review agent output — renames + claims + tasks in one structured response."""

    renames: list[Rename] = []
    claims: list[Claim] = []
    tasks: list[TaskSpec] = []


class InvestigationResult(BaseModel):
    answer: str
    claims: list[Claim] = []
    subtasks: list[TaskSpec] = []  # rich task specs, not bare strings


class Contradiction(BaseModel):
    claim_id_a: int
    claim_id_b: int
    explanation: str


class MergedClaim(BaseModel):
    keep_id: int
    remove_ids: list[int]
    merged_text: str


class StructField(BaseModel):
    offset: int
    name: str
    type_str: str             # C type string, e.g. "struct cJSON*"
    size: int
    confidence: TRUTH_LEVELS


class StructDefinition(BaseModel):
    struct_name: str
    kind: str = Field(
        default="struct",
        description="'struct' or 'union'. A union is emitted when two "
        "observed accesses ALIAS THE SAME BYTE RANGE with different "
        "semantics (the overlap_hint fired); never emit 'union' for plain "
        "disjoint offsets.",
    )
    fields: list[StructField]


class TypeVerdict(BaseModel):
    """LLM verdict on a type-recovery candidate (the cross-function sharing
    gate, design § run-audit.md §10).

    A candidate may span multiple functions because the detector followed
    call-context evidence and concluded they share ONE data type. The verdict
    decides whether that merge is CORRECT and, if so, names it.

    Acceptance is deliberately cautious: an ACCEPTED merge retags struct
    pointer types onto every involved base across all functions — expensive to
    undo if wrong. REJECTING an over-merge (splitting back) is deferred to a
    later pass. A missed merge is trivially fixed later. So the bias is:
    when in doubt, accept the merge but flag uncertainty, rather than reject
    (see prompt: 'wrongly-merged ... harder for a VR llm to get past').
    """

    accepted: bool = Field(
        default=True,
        description="true when the functions really do share ONE data type "
        "and the merge should stand; false when they are different types and "
        "must stay separate",
    )
    rejection_reason: str = Field(
        default="",
        description="when accepted=false, why the candidate should be split "
        "(e.g. unrelated offsets, different base types, two different structs "
        "coincidentally passed to the same parameter)",
    )
    struct_name: str = Field(
        default="",
        description="proposed canonical pothole_case name for the shared "
        "type, e.g. 'cjson_node'. REQUIRED when accepted=true.",
    )
    kind: str = Field(
        default="struct",
        description="'struct' or 'union' when accepted=true",
    )
    fields: list[StructField] = Field(
        default_factory=list,
        description="the UNION of all fields across every involved function "
        "— the COMPLETE layout, never a per-function subset. REQUIRED when "
        "accepted=true; MAY be empty when accepted=false ('no struct').",
    )


class CriticOutcome(BaseModel):
    """Returned by harness evaluate_claim() to caller (not an LLM output)."""

    accepted: bool
    feedback: str = ""


class FunctionSummary(BaseModel):
    """Pass 1 function-level output — name + summary."""

    llm_name: str
    canon_name: str
    summary: str              # one-paragraph purpose description


class ResynthesisResult(BaseModel):
    contradictions: list[Contradiction] = []
    merged_claims: list[MergedClaim] = []
    new_tasks: list[TaskSpec] = []


class MergeResult(BaseModel):
    """Merge agent output — Deliverable 3.5 (design § 3.5).

    Added here because every agent-output model lives in this module.
    """

    status: Literal["applied", "resolved", "rejected"]
    conflicts_resolved: list[dict] = []
    new_tasks: list[int] = []


class MergeDecision(BaseModel):
    """Merge agent conflict-resolution decision (structured LLM output).

    Internal to the merge flow (not part of the public protocol): when a
    submission conflicts with master, the LLM either accepts/resolves each
    conflict (``resolutions``) or rejects the submission entirely
    (``reject=True``), in which case new TODO tasks are created for
    follow-up investigation.
    """

    reject: bool = False
    reasons: list[str] = []
    resolutions: list[dict] = []  # [{node_id, field, value}] — chosen values


def get_schema(model_class) -> dict:
    """Return model_class.model_json_schema() for vLLM structured_output."""
    return model_class.model_json_schema()


def parse_response(model_class, text: str):
    """Parse JSON text into the given Pydantic model.

    Accepts either bare JSON or text with a fenced ```json ...``` block
    (some models wrap structured output in fences). Raises ValidationError
    if the text does not parse into the model.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            # No JSON object present — validating the raw text yields a clear
            # pydantic.ValidationError explaining what the model expects.
            return model_class.model_validate(cleaned)
        data = json.loads(cleaned[start : end + 1])
    return model_class.model_validate(data)


async def send_structured(
    llm,
    session_id: str,
    message: str,
    model_class,
    thinking_level: str = "low",
    retries: int = 1,
):
    """Send ``message`` with the structured schema for ``model_class`` and parse it.

    Retries ``retries`` times (default 1) with a short recovery prompt after a
    ``ValidationError``. A truncate/format slip by vLLM is much more likely than
    a genuinely unanswerable question, so re-prompting the SAME session (which
    already holds the full context + the prior invalid attempt) is cheap and
    materially reduces payloads that "silently parse partially."

    ``llm`` may be any object exposing ``send(session_id, message, ...,
    structured_output=...)`` (the real client or the test ``StubLLM``).

    Raises the last ``ValidationError`` if every attempt fails to parse.
    """
    last_error: ValidationError | None = None
    for attempt in range(int(retries) + 1):
        response = await llm.send(
            session_id,
            message,
            thinking_level=thinking_level,
            structured_output=get_schema(model_class),
        )
        try:
            return parse_response(model_class, response)
        except ValidationError as exc:  # pragma: no cover - exercised via StubLLM
            last_error = exc
            message = (
                "That response did not match the required JSON schema. "
                "Return ONLY valid JSON matching the schema exactly."
            )
    if last_error is not None:
        raise last_error
    return model_class.model_validate({})  # pragma: no cover - defensive


__all__ = [
    "TRUTH_LEVELS",
    "_TRUTH_LEVEL_VALUES",
    "Rename",
    "EvidenceLink",
    "Claim",
    "Submission",
    "CriticVerdict",
    "CriticOutcome",
    "TaskContextSpec",
    "TaskSpec",
    "ReviewOutput",
    "InvestigationResult",
    "Contradiction",
    "MergedClaim",
    "StructField",
    "StructDefinition",
    "TypeVerdict",
    "FunctionSummary",
    "ResynthesisResult",
    "MergeResult",
    "MergeDecision",
    "NameDecision",
    "RatifyOutput",
    "get_schema",
    "parse_response",
    "send_structured",
]

