---
status: design
version: 1
updated: 2026-09-25
target: Binary Ninja 6.0 headless
---

# REAPER — Binja API Module Specification

Everything that touches Binary Ninja lives in `reaper/tools/`. This document specifies the exact Binja 6.0 Python API surface used by each tool, so the implementing agent never has to guess at API calls.

**All files in this module:**
- `reaper/tools/hlil_extract.py` — HLILExtractor (deliverable 2.1)
- `reaper/tools/graph_nodes.py` — build_nodes() (deliverable 2.2)
- `reaper/tools/graph_edges.py` — build_edges() (deliverable 2.3)
- `reaper/tools/pin_symbols.py` — pin_symbols() (deliverable 2.4)
- `reaper/tools/graph_analysis.py` — validate_and_order(), compute_resynthesis_groups() (deliverables 2.5, 7.4)
- `reaper/tools/struct_detector.py` — StructAccessDetector (deliverable 4.1)
- `reaper/tools/graph_rebuild.py` — GraphRebuilder (deliverable 4.3)
- `reaper/tools/bndb_writer.py` — BNDBWriter (deliverable 3.4)

---

## 1. Headless Setup

**Environment:** Binja 6.0 headless is NOT pip-installed. It ships its own Python site-packages.

```bash
export PYTHONPATH=$HOME/binja/python
# or wherever the headless license/install lives
```

**Opening a binary:**
```python
import binaryninja

# open_view() does full analysis (linear sweep + recursive descent).
# Blocks until analysis completes. Returns a BinaryView.
bv = binaryninja.open_view("/path/to/binary")

# For headless, no UI. Analysis runs synchronously in open_view().
# If you need to wait for specific analysis:
bv.update_analysis_and_wait()
```

**Saving a BNDB:**
```python
# Save current state (renames, types, tags) to .bndb file
bv.create_database("/path/to/output.bndb")

# Save updates to an existing BNDB
bv.save_auto_snapshot()

# Or save to a specific path
bv.file.save("/path/to/output.bndb")
```

**Reopening from BNDB:**
```python
bv = binaryninja.open_view("/path/to/output.bndb")
```

---

## 2. Functions

```python
# All functions in the binary
for func in bv.functions:
    print(hex(func.start), func.name)

# Get function at exact address
func = bv.get_function_at(0x1400)  # returns Function or None

# Get functions containing an address (for mid-function lookups)
funcs = bv.get_functions_containing(0x1418)  # returns list[Function]

# Function properties
func.start        # int — start address
func.name         # str — display name (settable)
func.symbol.name  # str — symbol name (may differ from func.name)
func.function_type  # FunctionType — return type + param types
func.return_type  # Type object

# Function size (approximate — to end of last basic block)
func_end = max(bb.end for bb in func.basic_blocks) if func.basic_blocks else func.start
func_size = func_end - func.start
```

---

## 3. Variables and Parameters

```python
# Local variables (stack vars, register vars)
for var in func.vars:
    print(var.name, var.type, var.source_type)
    # var.name       — str (settable)
    # var.type       — Type object
    # var.source_type — VariableSourceType enum:
    #   VariableSourceType.StackVariableSourceType
    #   VariableSourceType.RegisterVariableSourceType
    #   VariableSourceType.FlagVariableSourceType

# Parameters (subset of vars that are function arguments)
for param in func.parameter_vars:
    print(param.name, param.type)
    # Same Variable object, but these are the calling-convention params
    # param.index is the parameter position (0-based) — NOT a direct
    # property; use enumerate() or func.parameter_vars.index(param)

# Renaming a variable
var.name = "new_name"
# This is immediate — the BinaryView updates in-place.
# HLIL output for the function will reflect the new name
# on next access (no re-analysis needed for name changes).

# Renaming a function
func.name = "new_name"

# Finding a variable by name within a function
def find_var_by_name(func, name):
    for var in func.vars:
        if var.name == name:
            return var
    return None
```

---

## 4. HLIL Access

```python
# Get the HLIL function object
hlil = func.hlil
# Returns HighLevelILFunction or None (if analysis failed)

# Iterate top-level instructions
for instr in hlil.instructions:
    print(f"  {hex(instr.address)}: {instr}")
    # instr.address  — int, binary address this instruction maps to
    # str(instr)     — human-readable HLIL text (what decompiler shows)
    # instr.operation — HighLevelILOperation enum value
    # instr.operands — tuple of child expressions/values

# WARNING: Do NOT use hlil.root — it does not exist.
# Use hlil.instructions to iterate.

# Get a specific instruction by index
instr = hlil[0]  # first instruction
instr = hlil[5]  # sixth instruction

# HLIL instruction count
len(hlil)  # number of top-level instructions
```

---

## 5. HLIL Instruction Type Reference

Every HLIL instruction has an `operation` field (a `HighLevelILOperation` enum value).
Each operation type exposes different properties. The implementing agent MUST match
on `instr.operation` and access the correct properties for that operation.

### 5.1 Assignment

```python
from binaryninja import HighLevelILOperation as Op

if instr.operation == Op.HLIL_ASSIGN:
    dest = instr.dest      # HighLevelILInstruction — left side
    src = instr.src        # HighLevelILInstruction — right side
    # Example: var_18 = malloc(0x100)
    # dest.operation == Op.HLIL_VAR, dest.var.name == "var_18"
    # src.operation == Op.HLIL_CALL
```

### 5.2 Variable References

```python
if instr.operation == Op.HLIL_VAR:
    var = instr.var        # Variable object
    # var.name, var.type — the variable being referenced
    # This is a LEAF expression — no children

if instr.operation == Op.HLIL_VAR_INIT:
    var = instr.dest       # Variable being initialized
    src = instr.src        # initializer expression
    # Example: int64_t var_18 = malloc(0x100)

if instr.operation == Op.HLIL_VAR_DECLARE:
    var = instr.var        # Variable being declared (no initializer)
    # Example: int64_t var_18
```

### 5.3 Constants

```python
if instr.operation == Op.HLIL_CONST:
    value = instr.constant  # int — the constant value
    # Example: 0x100

if instr.operation == Op.HLIL_CONST_PTR:
    value = instr.constant  # int — pointer constant (address)
    # Often references strings or global data
    # Check if it's a string: bv.get_string_at(value)

if instr.operation == Op.HLIL_CONST_DATA:
    value = instr.constant_data  # RegisterValue — data constant
    # Less common; used for embedded data references
```

### 5.4 Function Calls

```python
if instr.operation == Op.HLIL_CALL:
    dest = instr.dest      # HighLevelILInstruction — call target
    params = instr.params  # list[HighLevelILInstruction] — arguments

    # Resolve the call target:
    if dest.operation == Op.HLIL_CONST_PTR:
        target_addr = dest.constant
        target_func = bv.get_function_at(target_addr)
        # target_func.name gives the callee name
        # If None — call to external/PLT stub
    elif dest.operation == Op.HLIL_VAR:
        # Indirect call through function pointer
        # Mark as ambiguous — can't statically resolve
        pass
    else:
        # Other indirect call patterns (vtable, etc.)
        pass

    # Match parameters to callee's parameter_vars:
    if target_func:
        for i, arg_expr in enumerate(params):
            if i < len(target_func.parameter_vars):
                callee_param = target_func.parameter_vars[i]
                # arg_expr flows into callee_param
```

### 5.5 Returns

```python
if instr.operation == Op.HLIL_RET:
    src = instr.src        # list[HighLevelILInstruction] — return values
    # Usually len(src) == 1 for single-return functions
    # Can be empty for void returns
    for ret_val in src:
        # ret_val is the expression being returned
        pass
```

### 5.6 Control Flow (no dataflow edges — recurse into bodies)

```python
if instr.operation == Op.HLIL_IF:
    condition = instr.condition   # HighLevelILInstruction — the test
    true_body = instr.true        # int — HLIL instruction index of true branch
    false_body = instr.false      # int — HLIL instruction index of false branch
    # IMPORTANT: true/false are indices, not instruction objects.
    # To get the instruction: hlil[instr.true], hlil[instr.false]
    # Recurse into condition to find dataflow (comparisons, var refs)
    # Recurse into true_body/false_body for assignments/calls within

if instr.operation == Op.HLIL_WHILE:
    condition = instr.condition   # HighLevelILInstruction
    body = instr.body             # int — HLIL instruction index
    # Recurse into both

if instr.operation == Op.HLIL_DO_WHILE:
    condition = instr.condition
    body = instr.body

if instr.operation == Op.HLIL_FOR:
    init = instr.init             # HighLevelILInstruction — loop init
    condition = instr.condition
    update = instr.update         # HighLevelILInstruction — loop increment
    body = instr.body             # int — instruction index
    # init and update may contain assignments — create dataflow edges

if instr.operation == Op.HLIL_SWITCH:
    condition = instr.condition
    cases = instr.cases           # list — case values
    # Recurse into each case body

if instr.operation == Op.HLIL_BLOCK:
    body = instr.body             # list[int] — instruction indices
    # A block of sequential instructions. Recurse into each.
```

### 5.7 Pointer/Memory Operations

```python
if instr.operation == Op.HLIL_DEREF:
    src = instr.src        # HighLevelILInstruction — pointer being dereferenced
    # Example: *ptr
    # The src might be HLIL_ADD (ptr + offset) for struct access

if instr.operation == Op.HLIL_ADDRESS_OF:
    src = instr.src        # HighLevelILInstruction — value whose address is taken
    # Example: &var

if instr.operation == Op.HLIL_STRUCT_FIELD:
    src = instr.src        # HighLevelILInstruction — struct base
    offset = instr.offset  # int — byte offset of the field
    member_index = instr.member_index  # int or None — struct member index
    # Example: ptr->field_at_0x10
    # If Binja resolved the type: member_index points to the struct definition
    # If unresolved: offset is a raw number, member_index may be None

if instr.operation == Op.HLIL_DEREF_FIELD:
    src = instr.src        # HighLevelILInstruction — struct pointer
    offset = instr.offset  # int — byte offset
    member_index = instr.member_index
    # Same as STRUCT_FIELD but through a pointer dereference
    # Example: *(base + 0x10) rendered as base->field

if instr.operation == Op.HLIL_ARRAY_INDEX:
    src = instr.src        # HighLevelILInstruction — array base
    index = instr.index    # HighLevelILInstruction — index expression
    # Example: arr[i]
```

### 5.8 Arithmetic / Comparison (sub-expressions)

These appear as children of assignments, calls, conditions, etc.
They don't create their own dataflow edges — they contribute to
the parent instruction's dataflow. Walk them depth-first to find
the leaf variables and constants.

```python
# Binary operations
Op.HLIL_ADD      # instr.left, instr.right
Op.HLIL_SUB
Op.HLIL_MUL
Op.HLIL_DIVU     # unsigned divide
Op.HLIL_DIVS     # signed divide
Op.HLIL_MODU
Op.HLIL_MODS
Op.HLIL_AND      # bitwise AND
Op.HLIL_OR       # bitwise OR
Op.HLIL_XOR
Op.HLIL_LSL      # left shift
Op.HLIL_LSR      # logical right shift
Op.HLIL_ASR      # arithmetic right shift

# For all binary ops:
#   instr.left  — HighLevelILInstruction
#   instr.right — HighLevelILInstruction

# Comparison operations
Op.HLIL_CMP_E    # ==
Op.HLIL_CMP_NE   # !=
Op.HLIL_CMP_SLT  # signed <
Op.HLIL_CMP_ULT  # unsigned <
Op.HLIL_CMP_SLE  # signed <=
Op.HLIL_CMP_ULE  # unsigned <=
Op.HLIL_CMP_SGE  # signed >=
Op.HLIL_CMP_UGE  # unsigned >=
Op.HLIL_CMP_SGT  # signed >
Op.HLIL_CMP_UGT  # unsigned >

# For all comparison ops:
#   instr.left  — HighLevelILInstruction
#   instr.right — HighLevelILInstruction

# Unary operations
Op.HLIL_NEG      # -x          instr.src
Op.HLIL_NOT      # ~x / !x     instr.src
Op.HLIL_ZX       # zero-extend instr.src
Op.HLIL_SX       # sign-extend instr.src
Op.HLIL_LOW_PART # low bits    instr.src
```

### 5.9 SSA Phi Nodes

```python
if instr.operation == Op.HLIL_VAR_PHI:
    dest = instr.dest      # Variable — the phi target
    src = instr.src        # list[Variable] — the SSA sources
    # Creates DATAFLOW_ASSIGN from each source to dest
    # Note: phi nodes may not appear in non-SSA HLIL.
    # If using func.hlil (non-SSA), these won't be present.
    # For SSA form: func.hlil.ssa_form
```

**Important:** By default, `func.hlil` returns **non-SSA** HLIL.
For SSA form (with phi nodes and version-tagged variables), use:
```python
hlil_ssa = func.hlil.ssa_form
for instr in hlil_ssa.instructions:
    ...
```

REAPER uses **non-SSA HLIL** for agent-facing text (more readable)
and **SSA HLIL** for dataflow edge construction (precise def-use chains).

### 5.10 Misc Operations

```python
Op.HLIL_NOP      # no-op — skip
Op.HLIL_BREAK    # break statement — skip (control flow)
Op.HLIL_CONTINUE # continue statement — skip
Op.HLIL_GOTO     # goto — skip
Op.HLIL_LABEL    # label — skip
Op.HLIL_UNREACHABLE  # unreachable marker — skip
Op.HLIL_TRAP     # trap/int3 — skip
Op.HLIL_UNDEF    # undefined value — treat as unknown leaf

# Type casts
Op.HLIL_FLOAT_CONV   # float conversion — instr.src
Op.HLIL_INT_TO_FLOAT # int→float — instr.src
Op.HLIL_FLOAT_TO_INT # float→int — instr.src

# Intrinsics (compiler builtins, syscalls)
Op.HLIL_INTRINSIC    # instr.intrinsic (IntrinsicName), instr.params
```

---

## 6. Recursive Instruction Walker

The edge builder and struct detector need to walk HLIL depth-first.
Here's the canonical pattern:

```python
from binaryninja import HighLevelILOperation as Op

def walk_expr(expr, visitor):
    """Depth-first walk of an HLIL expression tree.
    visitor(expr) is called on every node.
    Returns whatever visitor returns on leaf nodes."""

    visitor(expr)

    op = expr.operation

    # Assignments
    if op == Op.HLIL_ASSIGN:
        walk_expr(expr.dest, visitor)
        walk_expr(expr.src, visitor)

    # Variable init (declaration + assignment)
    elif op == Op.HLIL_VAR_INIT:
        walk_expr(expr.src, visitor)

    # Calls
    elif op == Op.HLIL_CALL:
        walk_expr(expr.dest, visitor)
        for param in expr.params:
            walk_expr(param, visitor)

    # Returns
    elif op == Op.HLIL_RET:
        for val in expr.src:
            walk_expr(val, visitor)

    # Pointer ops
    elif op in (Op.HLIL_DEREF, Op.HLIL_ADDRESS_OF, Op.HLIL_NEG,
                Op.HLIL_NOT, Op.HLIL_ZX, Op.HLIL_SX, Op.HLIL_LOW_PART,
                Op.HLIL_FLOAT_CONV, Op.HLIL_INT_TO_FLOAT,
                Op.HLIL_FLOAT_TO_INT):
        walk_expr(expr.src, visitor)

    # Struct/field access
    elif op in (Op.HLIL_STRUCT_FIELD, Op.HLIL_DEREF_FIELD):
        walk_expr(expr.src, visitor)

    # Array index
    elif op == Op.HLIL_ARRAY_INDEX:
        walk_expr(expr.src, visitor)
        walk_expr(expr.index, visitor)

    # Binary ops (arithmetic, comparison)
    elif op in (Op.HLIL_ADD, Op.HLIL_SUB, Op.HLIL_MUL,
                Op.HLIL_DIVU, Op.HLIL_DIVS, Op.HLIL_MODU, Op.HLIL_MODS,
                Op.HLIL_AND, Op.HLIL_OR, Op.HLIL_XOR,
                Op.HLIL_LSL, Op.HLIL_LSR, Op.HLIL_ASR,
                Op.HLIL_CMP_E, Op.HLIL_CMP_NE,
                Op.HLIL_CMP_SLT, Op.HLIL_CMP_ULT,
                Op.HLIL_CMP_SLE, Op.HLIL_CMP_ULE,
                Op.HLIL_CMP_SGE, Op.HLIL_CMP_UGE,
                Op.HLIL_CMP_SGT, Op.HLIL_CMP_UGT):
        walk_expr(expr.left, visitor)
        walk_expr(expr.right, visitor)

    # Control flow — recurse into sub-expressions only
    elif op == Op.HLIL_IF:
        walk_expr(expr.condition, visitor)
    elif op in (Op.HLIL_WHILE, Op.HLIL_DO_WHILE):
        walk_expr(expr.condition, visitor)
    elif op == Op.HLIL_FOR:
        walk_expr(expr.init, visitor)
        walk_expr(expr.condition, visitor)
        walk_expr(expr.update, visitor)
    elif op == Op.HLIL_SWITCH:
        walk_expr(expr.condition, visitor)

    # Intrinsics
    elif op == Op.HLIL_INTRINSIC:
        for param in expr.params:
            walk_expr(param, visitor)

    # Leaves: HLIL_VAR, HLIL_CONST, HLIL_CONST_PTR, HLIL_CONST_DATA,
    #         HLIL_VAR_DECLARE, HLIL_NOP, HLIL_UNDEF, etc.
    # No children — visitor already called above.
```

**Usage in edge builder:**
```python
def collect_var_refs(expr):
    """Collect all Variable references in an expression tree."""
    refs = []
    def visitor(node):
        if node.operation == Op.HLIL_VAR:
            refs.append(node.var)
    walk_expr(expr, visitor)
    return refs
```

---

## 7. String References

```python
# Method 1: BinaryView string list (fast, complete)
for string_ref in bv.strings:
    # string_ref.start  — int, address of the string data
    # string_ref.length — int, byte length
    # string_ref.type   — StringType enum (Ascii, Utf8, Utf16, etc.)
    value = bv.read(string_ref.start, string_ref.length).decode('utf-8', errors='replace')

# Method 2: Get string at a specific address
s = bv.get_string_at(addr)  # returns StringReference or None
if s:
    value = bv.read(s.start, s.length).decode('utf-8', errors='replace')

# Method 3: Find string refs within a function's HLIL
# Walk instructions, find HLIL_CONST_PTR where the constant
# points to a known string address.
def get_string_refs_in_func(bv, func):
    known_strings = {s.start: s for s in bv.strings}
    refs = []
    for instr in func.hlil.instructions:
        def check_string(node):
            if node.operation == Op.HLIL_CONST_PTR:
                addr = node.constant
                if addr in known_strings:
                    s = known_strings[addr]
                    val = bv.read(s.start, s.length).decode('utf-8', errors='replace')
                    refs.append({"address": hex(addr), "value": val})
        walk_expr(instr, check_string)
    return refs
```

---

## 8. Symbols

```python
from binaryninja import SymbolType

# Imported functions (from dynamic linking — malloc, printf, etc.)
for sym in bv.get_symbols_of_type(SymbolType.ImportedFunctionSymbol):
    print(sym.name, hex(sym.address))
    # These are PLT stubs or IAT entries

# Library functions (matched by Binja's signature libraries)
for sym in bv.get_symbols_of_type(SymbolType.LibraryFunctionSymbol):
    print(sym.name, hex(sym.address))

# Regular functions with known names (debug symbols, exports)
for sym in bv.get_symbols_of_type(SymbolType.FunctionSymbol):
    if not sym.name.startswith("sub_"):
        print(sym.name, hex(sym.address))
        # Has a real name — debug info or export table

# Data symbols (named globals)
for sym in bv.get_symbols_of_type(SymbolType.DataSymbol):
    print(sym.name, hex(sym.address))

# All symbol types for reference:
# SymbolType.FunctionSymbol
# SymbolType.ImportedFunctionSymbol
# SymbolType.LibraryFunctionSymbol
# SymbolType.ImportAddressSymbol
# SymbolType.ImportedDataSymbol
# SymbolType.DataSymbol
# SymbolType.ExternalSymbol
```

---

## 9. Type System

### 9.1 Reading Types

```python
# Variable type
var = func.vars[0]
print(var.type)          # Type object — e.g., "int64_t*"
print(str(var.type))     # Human-readable string

# Function return type
print(func.return_type)

# Check if a type is a pointer
if var.type.type_class == TypeClass.PointerTypeClass:
    pointed_to = var.type.target  # Type — what it points to

# Check if a type is a struct
if var.type.type_class == TypeClass.StructureTypeClass:
    struct_type = var.type.structure
    for member in struct_type.members:
        print(f"  offset={member.offset} name={member.name} type={member.type}")

# Type classes for reference:
from binaryninja import TypeClass
# TypeClass.VoidTypeClass
# TypeClass.BoolTypeClass
# TypeClass.IntegerTypeClass
# TypeClass.FloatTypeClass
# TypeClass.PointerTypeClass
# TypeClass.ArrayTypeClass
# TypeClass.StructureTypeClass
# TypeClass.EnumerationTypeClass
# TypeClass.FunctionTypeClass
# TypeClass.NamedTypeReferenceTypeClass
```

### 9.2 Creating and Applying Types

```python
from binaryninja import Type, Structure, StructureBuilder

# Parse a C type string
type_obj, name = bv.parse_type_string("struct cJSON { int type; char* valuestring; }")
# type_obj — Type object
# name — str, the type name ("cJSON")

# Define a struct programmatically
sb = StructureBuilder.create()
sb.packed = False
sb.width = 0  # auto-calculate

# Add members at specific offsets
sb.insert(0, Type.int(4), "type")           # int at offset 0
sb.insert(8, Type.pointer(bv.arch, Type.char()), "valuestring")  # char* at offset 8
sb.insert(16, Type.pointer(bv.arch, Type.void()), "next")        # void* at offset 16

struct_type = Type.structure_type(sb)

# Register the type with the BinaryView
bv.define_user_type("cJSON", struct_type)

# Apply a type to a variable
func = bv.get_function_at(0x1400)
var = func.vars[0]
new_type = bv.get_type_by_name("cJSON")
if new_type:
    # Change the variable's type
    func.create_user_var(var, new_type, var.name)
    # After this, HLIL will show struct field accesses instead of
    # raw pointer+offset

# Apply a type to a function's parameter
param_type, _ = bv.parse_type_string("struct cJSON*")
func.parameter_vars[0].type = param_type
# Or change the full function type:
new_func_type = Type.function(func.return_type, [param_type, ...])
func.function_type = new_func_type
```

### 9.3 Effect on HLIL After Type Application

```
BEFORE type recovery:
  0x1400: *(arg1 + 0x10) = 0
  0x1408: if (*(arg1 + 0x18) != 0)
  0x1410: return *(arg1 + 8)

AFTER applying struct cJSON to arg1:
  0x1400: arg1->next = 0
  0x1408: if (arg1->child != 0)
  0x1410: return arg1->valuestring
```

This is why type recovery (Phase 4) runs BEFORE renaming (Phase 5) —
struct field access creates better HLIL for the agents to read,
and creates proper `:FIELD_OF` nodes in the graph.

**Re-reading HLIL after type changes requires no special action.**
Binja updates HLIL lazily. After modifying types, the next access
to `func.hlil.instructions` reflects the new types. If HLIL seems
stale, call:
```python
func.reanalyze()
bv.update_analysis_and_wait()
```

---

## 10. Tags (Storing LLM Names)

Binja tags attach metadata to specific addresses. REAPER uses them
to store the verbose LLM name alongside the human-readable canonical name.

```python
# Create a tag type (once per BinaryView, idempotent)
tag_type = bv.create_tag_type("reaper_llm", "🏷")
# If it already exists, this returns the existing one.
# tag_type.name == "reaper_llm"

# Tag a function with its LLM name
func.create_user_address_tag(func.start, tag_type,
    "json_parse_error_string_buffer_allocation_function")

# Tag a variable (via the instruction address where it's declared/used)
# Variables don't have direct tag support — tag the function address
# with a structured string:
func.create_user_address_tag(func.start, tag_type,
    f"var:{var.name}={llm_name}")

# Read tags back
for tag in func.address_tags:
    if tag.type.name == "reaper_llm":
        print(tag.data)  # the LLM name string

# Alternative: use function-level tags (not address-specific)
func.create_tag(tag_type, "verbose_llm_name_here", True)
# func.tags — all tags on the function
```

---

## 11. Struct Access Detection Patterns

The struct detector (4.1) walks HLIL looking for these Binja-specific patterns:

### Pattern 1: Explicit struct field (Binja already knows the type)
```python
if instr.operation == Op.HLIL_STRUCT_FIELD:
    base = instr.src     # the struct pointer/variable
    offset = instr.offset
    # This means Binja already resolved the type — great
    # Group by base variable within the function
```

### Pattern 2: Deref field (pointer->field)
```python
if instr.operation == Op.HLIL_DEREF_FIELD:
    base = instr.src
    offset = instr.offset
    # Same as STRUCT_FIELD but through explicit dereference
```

### Pattern 3: Raw pointer arithmetic (Binja doesn't know the type)
```python
if instr.operation == Op.HLIL_DEREF:
    inner = instr.src
    if inner.operation == Op.HLIL_ADD:
        base = inner.left
        offset_expr = inner.right
        if offset_expr.operation == Op.HLIL_CONST:
            offset = offset_expr.constant
            # *(base + 0x10) — likely a struct field access
            # Group by base variable, record the offset

# Also check for array-index patterns that might be struct accesses:
if instr.operation == Op.HLIL_ARRAY_INDEX:
    if instr.index.operation == Op.HLIL_CONST:
        # arr[constant] — might be field access if arr is a byte pointer
        pass
```

### Cross-function grouping via Binja types
```python
# To check if two variables in different functions are the same type:
func_a_arg = func_a.parameter_vars[0]
func_b_arg = func_b.parameter_vars[0]

if str(func_a_arg.type) == str(func_b_arg.type):
    # Same type string — group their struct accesses together
    pass
elif func_a_arg.type == func_b_arg.type:
    # Type object equality — more precise
    pass
else:
    # Different types or unknown — DON'T cross-function group
    pass
```

---

## 12. API Gotchas for Binja 6.0

1. **`func.hlil.root` does not exist.** Use `func.hlil.instructions`.

2. **`func.hlil` can be None** if the function is too short, a thunk,
   or analysis failed. Always check before iterating.

3. **HLIL_IF true/false branches are instruction INDICES, not objects.**
   Use `hlil[instr.true]` and `hlil[instr.false]` to get the instructions.
   These may be HLIL_BLOCK containing multiple sub-instructions.

4. **Variable identity:** Two references to the same variable in HLIL
   will have `var.name` and `var.type` matching, but they are NOT the
   same Python object. Compare by `var.name` (or `var.identifier` if available).

5. **SSA vs non-SSA:** `func.hlil` is non-SSA. `func.hlil.ssa_form` is SSA.
   Non-SSA is more readable (agents see it). SSA has phi nodes and
   version-tagged variables (better for precise dataflow). REAPER uses
   non-SSA for display, SSA for edge building.

6. **Thread safety:** BinaryView is NOT thread-safe. All Binja API calls
   must happen on the same thread (or use Binja's worker thread API).
   Since REAPER is asyncio (single-threaded), this is fine — but do NOT
   use `asyncio.to_thread()` for Binja calls.

7. **Renaming is immediate.** After `var.name = "x"`, the next read of
   `func.hlil.instructions` shows the new name. No re-analysis needed
   for name changes. Type changes DO trigger re-analysis.

8. **`bv.update_analysis_and_wait()` blocks.** Call it after type
   recovery, not after every rename. It re-runs all analysis passes.

9. **open_view() blocks until initial analysis completes.** For large
   binaries (10k+ functions), this can take minutes. For cJSON (~30 funcs),
   it's instant.

10. **`parse_type_string` returns (Type, str).** The second element is the
    type name. If parsing fails, it raises an exception — catch it.

---

## 13. Module Entry Points

Each tool file exposes one async entry point that the pipeline runner imports:

| File | Entry Point | Signature |
|---|---|---|
| `graph_nodes.py` | `build_nodes` | `async def build_nodes(extractor: HLILExtractor, neo4j_driver) -> None` |
| `graph_edges.py` | `build_edges` | `async def build_edges(extractor: HLILExtractor, neo4j_driver) -> None` |
| `pin_symbols.py` | `pin_symbols` | `async def pin_symbols(extractor: HLILExtractor, neo4j_driver) -> None` |
| `graph_analysis.py` | `validate_and_order` | `async def validate_and_order(neo4j_driver) -> None` |
| `graph_analysis.py` | `compute_resynthesis_groups` | `async def compute_resynthesis_groups(neo4j_driver) -> list[list[str]]` |

The classes (`HLILExtractor`, `BNDBWriter`, `StructAccessDetector`,
`GraphRebuilder`) are imported and instantiated by the pipeline runner.

**All Binja calls are synchronous** (Binja is not async). They run on
the asyncio event loop's thread. Since Binja calls are CPU-bound (not
I/O-bound), this is fine — they're fast for cJSON-sized binaries.
For larger binaries, consider `asyncio.to_thread()` for long-running
Binja operations like `open_view()` — but NOT for BinaryView mutations
(see gotcha #6).
