// REAPER graph schema — Deliverable 1.2.
//
// Run via reaper.infra.init_db.init_schema (idempotent). All statements use
// IF NOT EXISTS so re-running is safe.

// ---------------------------------------------------------------------------
// Node constraints
// ---------------------------------------------------------------------------

CREATE CONSTRAINT function_address_unique IF NOT EXISTS
FOR (f:Function) REQUIRE f.address IS UNIQUE;

CREATE CONSTRAINT variable_id_unique IF NOT EXISTS
FOR (v:Variable) REQUIRE v.id IS UNIQUE;

CREATE CONSTRAINT argument_id_unique IF NOT EXISTS
FOR (a:Argument) REQUIRE a.id IS UNIQUE;

CREATE CONSTRAINT call_id_unique IF NOT EXISTS
FOR (c:Call) REQUIRE c.id IS UNIQUE;

CREATE CONSTRAINT stringref_id_unique IF NOT EXISTS
FOR (s:StringRef) REQUIRE s.id IS UNIQUE;

// ---------------------------------------------------------------------------
// Node properties (all node types):
//   address         — hex address in binary
//   llm_name        — verbose pothole_case name (null until renamed)
//   canon_name      — human-readable name (null until renamed)
//   pinned          — boolean, true = known symbol, agents cannot rename
//   ambiguous       — boolean, true = unresolved indirect/missing info
//   scc_id          — integer, SCC group ID (null if not in a cycle)
//   traversal_order — integer, topological order for processing (lower first)
//   isolated        — boolean, true = dead/empty function (no callers, no vars)
//
// StringRef properties:
//   value           — the literal string content
//   address         — address where string is referenced
//
// Edge types (structural, for traversal — no taxonomy beyond type):
//   :DATAFLOW_ASSIGN  — variable = expression
//   :DATAFLOW_ARG     — value passed as argument to call
//   :CALL             — function calls function
//   :RETURN           — return value flows to caller
//   :FIELD_OF         — struct field belongs to struct type
//   :DEFERRED_BACK    — back-edge in SCC, deferred for second pass
//   :REFS_STRING      — variable or argument references a string constant
//   :CONTAINS         — function contains variable/argument/call (ownership)

// ---------------------------------------------------------------------------
// Indexes
// ---------------------------------------------------------------------------

CREATE INDEX function_scc_id_index IF NOT EXISTS
FOR (f:Function) ON (f.scc_id);

CREATE INDEX function_traversal_order_index IF NOT EXISTS
FOR (f:Function) ON (f.traversal_order);

CREATE INDEX variable_address_index IF NOT EXISTS
FOR (v:Variable) ON (v.address);

CREATE INDEX argument_address_index IF NOT EXISTS
FOR (a:Argument) ON (a.address);
