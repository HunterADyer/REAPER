"""Merge agent — Deliverable 3.5.

Handles concurrent submission conflicts via LLM-based resolution. The
pipeline hands this agent the ACCEPTED subset of a submission (renames with
critic-approved claims — rejected claims are filtered by the dispatcher
before this is called).

Flow (design § 3.5):
  1. ``shadow_mgr.diff()`` against the session's checkout (a fresh checkout
     baseline is created if the session has none).
  2. No conflicts → apply → ``MergeResult(status='applied')``.
  3. Conflicts → ask the LLM to resolve or reject:
       - resolve: apply safe mutations + the chosen resolution values
         → ``status='resolved'`` with ``conflicts_resolved``.
       - reject: create new TODO tasks → ``status='rejected'`` with
         ``new_tasks`` populated.
  4. On any successful apply the BinaryView is kept in sync: every rename in
     the submission is pushed to ``bndb_writer.rename_function()`` /
     ``rename_variable()`` so the BNDB never diverges from Neo4j.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reaper.harness.submission import (
    MergeDecision,
    MergeResult,
    Submission,
    get_schema,
    parse_response,
)

log = logging.getLogger(__name__)


class MergeAgent:
    """Resolves concurrent-write conflicts with LLM-assisted merging."""

    def __init__(self, llm_client, shadow_mgr, bndb_writer, ledger, todo, config: dict):
        self.llm = llm_client
        self.shadow_mgr = shadow_mgr
        self.bndb_writer = bndb_writer
        self.ledger = ledger
        self.todo = todo
        self.config = config or {}
        with open(self._prompt_path(), encoding="utf-8") as fh:
            self.prompt = fh.read()

    @staticmethod
    def _prompt_path() -> str:
        return str(
            Path(__file__).resolve().parent.parent
            / "agents" / "prompts" / "merge_agent.txt"
        )

    async def attempt_merge(self, session_id: str, submission: Submission) -> MergeResult:
        """Merge the ACCEPTED subset of a submission.

        ``submission`` must already be filtered by the caller to the accepted
        subset (renames backed by critic-approved claims only).
        """
        renames = list(submission.renames)
        claims = list(submission.claims)

        # Ensure a checkout baseline exists so diff() can compare versions.
        if self.shadow_mgr.get_checkout(session_id) is None:
            root = self._root_function(renames, claims)
            if root:
                await self.shadow_mgr.checkout(root, session_id)

        modified = self._build_modified(session_id, renames, claims)
        result = await self.shadow_mgr.diff(session_id, modified)

        if not result["conflicts"]:
            await self.shadow_mgr.apply(session_id, result["mutations"])
            self._sync_bndb_renames(renames)
            return MergeResult(status="applied")

        decision = await self._resolve(session_id, result["conflicts"])
        if decision.reject:
            new_tasks = await self._create_followup_tasks(result["conflicts"], decision.reasons)
            return MergeResult(status="rejected", new_tasks=new_tasks)

        # Resolved: apply the safe mutations, then override each conflicted
        # field with the LLM-chosen value.
        resolved_mutations = list(result["mutations"])
        applied_resolutions: list[dict] = []
        for res in decision.resolutions or []:
            node_id = res.get("node_id")
            field = res.get("field")
            value = res.get("value")
            if not node_id or not field:
                continue
            replaced = False
            for cm in result["conflicts"]:
                if cm["node_id"] == node_id and cm["field"] == field:
                    resolved_mutations.append(
                        {**cm, "value": value, "conflict": False, "resolved": True}
                    )
                    applied_resolutions.append(
                        {"node_id": node_id, "field": field, "value": value}
                    )
                    replaced = True
                    break
            if not replaced:
                resolved_mutations.append({"node_id": node_id, "field": field, "value": value})
                applied_resolutions.append({"node_id": node_id, "field": field, "value": value})

        await self.shadow_mgr.apply(session_id, resolved_mutations)
        self._sync_bndb_renames(renames)
        return MergeResult(status="resolved", conflicts_resolved=applied_resolutions)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def _resolve(self, session_id: str, conflicts: list[dict]) -> MergeDecision:
        if self.llm is None:
            log.info("no LLM client configured — auto-accepting merge conflicts")
            return MergeDecision(reject=False, reasons=["no LLM client; auto-accepted"])

        context = self._conflict_context(conflicts)
        merge_session = f"merge:{session_id}"
        try:
            await self.llm.create_session(merge_session, self.prompt)
            response = await self.llm.send(
                merge_session,
                context,
                thinking_level=self.config.get("thinking_levels", {}).get("merge", "medium"),
                structured_output=get_schema(MergeDecision),
            )
            return parse_response(MergeDecision, response)
        finally:
            self.llm.destroy_session(merge_session)

    @staticmethod
    def _conflict_context(conflicts: list[dict]) -> str:
        lines = ["CONFLICTS TO RESOLVE:"]
        for i, c in enumerate(conflicts):
            lines.append(
                f"{i}. node_id={c.get('node_id')} field={c.get('field')} "
                f"checkout_value={c.get('checkout_value')!r} "
                f"submitted={c.get('value')!r} master_value={c.get('master_value')!r}"
            )
        return "\n".join(lines)

    async def _create_followup_tasks(
        self, conflicts: list[dict], reasons: list[str]
    ) -> list[int]:
        reason = "; ".join(reasons or ["conflicting submissions need disambiguation"])
        base = ""
        if conflicts:
            root = str(conflicts[0].get("node_id", ""))
            base = root.split(":")[0]
        task_ids: list[int] = []
        for c in conflicts:
            task_id = await self.todo.create_task(
                description=(
                    f"Resolve rename conflict for node {c.get('node_id')} "
                    f"field={c.get('field')}: current master="
                    f"{c.get('master_value')!r} vs submitted={c.get('value')!r}. {reason}"
                ),
                context_spec={"functions": [base], "include_claims": True},
                start_position=base,
                goal="Determine the correct name/value for the conflicted field",
                graph_refs=[str(c.get("node_id"))],
            )
            task_ids.append(task_id)
        return task_ids

    def _build_modified(self, session_id: str, renames, claims) -> dict:
        nodes: dict[str, dict] = {}
        for rn in renames:
            nodes.setdefault(rn.node_id, {})
            nodes[rn.node_id]["llm_name"] = rn.llm_name
            nodes[rn.node_id]["canon_name"] = rn.canon_name
        claim_entries = [
            {
                "kind": "claim",
                "function_address": c.function_address,
                "claim_text": c.claim_text,
                "submitted_by": session_id,
                "evidence": [e.model_dump() for e in c.evidence],
            }
            for c in claims
        ]
        return {"nodes": nodes, "claims": claim_entries}

    def _sync_bndb_renames(self, renames) -> None:
        """Push accepted renames into the BinaryView so it stays in sync."""
        if self.bndb_writer is None:
            return
        for rn in renames:
            try:
                if ":" in rn.node_id:
                    addr_hex = rn.node_id.split(":")[0]
                    self.bndb_writer.rename_variable(
                        int(addr_hex, 16), rn.node_id, rn.llm_name, rn.canon_name
                    )
                else:
                    self.bndb_writer.rename_function(
                        int(rn.node_id, 16), rn.llm_name, rn.canon_name
                    )
            except Exception:
                log.exception("bndb sync failed for node %s", rn.node_id)

    @staticmethod
    def _root_function(renames, claims) -> str | None:
        for rn in renames:
            root = rn.node_id.split(":")[0]
            if root:
                return root
        for c in claims:
            if c.function_address:
                return c.function_address
        return None


__all__ = ["MergeAgent"]

