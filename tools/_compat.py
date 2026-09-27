"""Binary Ninja import compatibility helper — NOT a deliverable.

Binja 6.0 headless is not pip-installable; it is expected on
``PYTHONPATH=$HOME/binja-headless/python`` (see docs/binja-module.md § 1).
This module guards every ``binaryninja`` import so that:

* the rest of the package imports cleanly even when Binja is absent, and
* tools raise a clear, actionable ``RuntimeError`` only if a class that
  genuinely needs a live BinaryView is instantiated without Binja.

It also provides **fallback enums** so the pure-Python logic (graph edge
mapping, struct detector instruction walking) can be unit-tested with fake
HLIL objects: wherever REAPER compares ``instr.operation == Op.HLIL_ASSIGN``,
``Op`` resolves to the REAL ``HighLevelILOperation`` when Binja is present and
to :class:`FallbackOp` otherwise. Because both enums share identical member
names, tool code and tests are written against ``Op`` and work in either mode.
"""

from __future__ import annotations

import enum
from typing import Any

# Lazily resolved: only import (and pay the import cost) if Binja is present.
binaryninja: Any = None

try:  # pragma: no cover - exercised only when Binja is installed
    import binaryninja as _bn

    binaryninja = _bn
    from binaryninja import (  # type: ignore
        HighLevelILOperation as Op,
        SymbolType,
        TypeClass,
        VariableSourceType,
    )

    BINJA_AVAILABLE = True
except ImportError:  # Binja not on PYTHONPATH — use the fallback enums
    Op = None  # type: ignore[assignment]
    SymbolType = None  # type: ignore[assignment]
    TypeClass = None  # type: ignore[assignment]
    VariableSourceType = None  # type: ignore[assignment]
    BINJA_AVAILABLE = False


# ---------------------------------------------------------------------------
# Fallback enums (used only when Binja is absent, for unit tests)
# ---------------------------------------------------------------------------

if Op is None:

    class FallbackOp(enum.Enum):
        """Mirror of the HighLevelILOperation members REAPER touches.

        Member names intentionally match the real ``HighLevelILOperation`` so
        tool code written against ``Op`` works identically with and without
        Binja. Tests construct fake instructions whose ``.operation`` is a
        ``FallbackOp`` member.
        """

        # assignments / declarations
        HLIL_ASSIGN = "HLIL_ASSIGN"
        HLIL_VAR_INIT = "HLIL_VAR_INIT"
        HLIL_VAR_DECLARE = "HLIL_VAR_DECLARE"
        HLIL_VAR_PHI = "HLIL_VAR_PHI"
        # leaves
        HLIL_VAR = "HLIL_VAR"
        HLIL_CONST = "HLIL_CONST"
        HLIL_CONST_PTR = "HLIL_CONST_PTR"
        HLIL_CONST_DATA = "HLIL_CONST_DATA"
        HLIL_NOP = "HLIL_NOP"
        HLIL_UNDEF = "HLIL_UNDEF"
        # function calls / returns
        HLIL_CALL = "HLIL_CALL"
        HLIL_TAILCALL = "HLIL_TAILCALL"
        HLIL_RET = "HLIL_RET"
        HLIL_INTRINSIC = "HLIL_INTRINSIC"
        # pointer / memory operations
        HLIL_DEREF = "HLIL_DEREF"
        HLIL_DEREF_FIELD = "HLIL_DEREF_FIELD"
        HLIL_STRUCT_FIELD = "HLIL_STRUCT_FIELD"
        HLIL_ADDRESS_OF = "HLIL_ADDRESS_OF"
        HLIL_ARRAY_INDEX = "HLIL_ARRAY_INDEX"
        HLIL_MEM_PHI = "HLIL_MEM_PHI"
        HLIL_UNIMPL_MEM_PHI = "HLIL_UNIMPL_MEM_PHI"
        # control flow (no dataflow edges — recurse into bodies)
        HLIL_IF = "HLIL_IF"
        HLIL_WHILE = "HLIL_WHILE"
        HLIL_DO_WHILE = "HLIL_DO_WHILE"
        HLIL_FOR = "HLIL_FOR"
        HLIL_SWITCH = "HLIL_SWITCH"
        HLIL_BLOCK = "HLIL_BLOCK"
        HLIL_GOTO = "HLIL_GOTO"
        HLIL_BREAK = "HLIL_BREAK"
        HLIL_CONTINUE = "HLIL_CONTINUE"
        # arithmetic (sub-expressions)
        HLIL_ADD = "HLIL_ADD"
        HLIL_SUB = "HLIL_SUB"
        HLIL_MUL = "HLIL_MUL"
        HLIL_DIVU = "HLIL_DIVU"
        HLIL_DIVS = "HLIL_DIVS"
        HLIL_MODU = "HLIL_MODU"
        HLIL_MODS = "HLIL_MODS"
        HLIL_AND = "HLIL_AND"
        HLIL_OR = "HLIL_OR"
        HLIL_XOR = "HLIL_XOR"
        HLIL_LSL = "HLIL_LSL"
        HLIL_LSR = "HLIL_LSR"
        HLIL_ASR = "HLIL_ASR"
        # comparison
        HLIL_CMP_E = "HLIL_CMP_E"
        HLIL_CMP_NE = "HLIL_CMP_NE"
        HLIL_CMP_SLT = "HLIL_CMP_SLT"
        HLIL_CMP_ULT = "HLIL_CMP_ULT"
        HLIL_CMP_SLE = "HLIL_CMP_SLE"
        HLIL_CMP_ULE = "HLIL_CMP_ULE"
        HLIL_CMP_SGE = "HLIL_CMP_SGE"
        HLIL_CMP_UGE = "HLIL_CMP_UGE"
        HLIL_CMP_SGT = "HLIL_CMP_SGT"
        HLIL_CMP_UGT = "HLIL_CMP_UGT"
        HLIL_TEST_BIT = "HLIL_TEST_BIT"
        # unary / casts
        HLIL_NEG = "HLIL_NEG"
        HLIL_NOT = "HLIL_NOT"
        HLIL_ZX = "HLIL_ZX"
        HLIL_SX = "HLIL_SX"
        HLIL_LOW_PART = "HLIL_LOW_PART"
        HLIL_FLOAT_CONV = "HLIL_FLOAT_CONV"
        HLIL_INT_TO_FLOAT = "HLIL_INT_TO_FLOAT"
        HLIL_FLOAT_TO_INT = "HLIL_FLOAT_TO_INT"
        HLIL_FLOAT_NEG = "HLIL_FLOAT_NEG"
        HLIL_FLOAT_ABS = "HLIL_FLOAT_ABS"
        HLIL_FLAG = "HLIL_FLAG"
        HLIL_FLAG_PHI = "HLIL_FLAG_PHI"
        HLIL_SYSCALL = "HLIL_SYSCALL"

    Op = FallbackOp


if SymbolType is None:

    class FallbackSymbolType(enum.Enum):
        """Mirror of the SymbolType members REAPER uses (see binja-module § 8)."""

        FunctionSymbol = "FunctionSymbol"
        ImportedFunctionSymbol = "ImportedFunctionSymbol"
        LibraryFunctionSymbol = "LibraryFunctionSymbol"
        ImportAddressSymbol = "ImportAddressSymbol"
        ImportedDataSymbol = "ImportedDataSymbol"
        DataSymbol = "DataSymbol"
        ExternalSymbol = "ExternalSymbol"

    SymbolType = FallbackSymbolType

if TypeClass is None:

    class FallbackTypeClass(enum.Enum):
        """Mirror of TypeClass members (binja-module § 9.1)."""

        VoidTypeClass = "VoidTypeClass"
        BoolTypeClass = "BoolTypeClass"
        IntegerTypeClass = "IntegerTypeClass"
        FloatTypeClass = "FloatTypeClass"
        StructureTypeClass = "StructureTypeClass"
        EnumerationTypeClass = "EnumerationTypeClass"
        PointerTypeClass = "PointerTypeClass"
        ArrayTypeClass = "ArrayTypeClass"
        FunctionTypeClass = "FunctionTypeClass"

    TypeClass = FallbackTypeClass

if VariableSourceType is None:

    class FallbackVariableSourceType(enum.Enum):
        """Mirror of VariableSourceType members (binja-module § 3)."""

        StackVariableSourceType = "StackVariableSourceType"
        RegisterVariableSourceType = "RegisterVariableSourceType"
        FlagVariableSourceType = "FlagVariableSourceType"

    VariableSourceType = FallbackVariableSourceType


_BINJA_HINT = (
    "Binary Ninja headless is required but was not importable. "
    "Ensure it is installed and add its python site-packages to PYTHONPATH, "
    "e.g. export PYTHONPATH=$HOME/binja-headless/python  "
    "(see docs/binja-module.md § 1)."
)


def require_binja(feature: str = "this feature") -> None:
    """Raise RuntimeError unless Binja is importable.

    Call from any method that needs a live BinaryView, so the failure is a
    clear, actionable error instead of an AttributeError deep inside a walker.
    """
    if not BINJA_AVAILABLE:
        raise RuntimeError(f"{feature} requires Binary Ninja headless. {_BINJA_HINT}")


# ---------------------------------------------------------------------------
# Recursive HLIL instruction walker (binja-module.md § 6)
# ---------------------------------------------------------------------------

# Operations whose children live on .left/.right rather than .operands/.src.
_BINARY_OPS = frozenset(
    {
        Op.HLIL_ADD,
        Op.HLIL_SUB,
        Op.HLIL_MUL,
        Op.HLIL_DIVU,
        Op.HLIL_DIVS,
        Op.HLIL_MODU,
        Op.HLIL_MODS,
        Op.HLIL_AND,
        Op.HLIL_OR,
        Op.HLIL_XOR,
        Op.HLIL_LSL,
        Op.HLIL_LSR,
        Op.HLIL_ASR,
        Op.HLIL_CMP_E,
        Op.HLIL_CMP_NE,
        Op.HLIL_CMP_SLT,
        Op.HLIL_CMP_ULT,
        Op.HLIL_CMP_SLE,
        Op.HLIL_CMP_ULE,
        Op.HLIL_CMP_SGE,
        Op.HLIL_CMP_UGE,
        Op.HLIL_CMP_SGT,
        Op.HLIL_CMP_UGT,
    }
)

# Operations whose only child is .src.
_UNARY_SRC_OPS = frozenset(
    {
        Op.HLIL_DEREF,
        Op.HLIL_ADDRESS_OF,
        Op.HLIL_NEG,
        Op.HLIL_NOT,
        Op.HLIL_FLOAT_NEG,
        Op.HLIL_FLOAT_ABS,
        Op.HLIL_ZX,
        Op.HLIL_SX,
        Op.HLIL_LOW_PART,
        Op.HLIL_FLOAT_CONV,
        Op.HLIL_INT_TO_FLOAT,
        Op.HLIL_FLOAT_TO_INT,
    }
)

# Operations whose children are .src + .index
_ARRAY_OPS = frozenset({Op.HLIL_ARRAY_INDEX, Op.HLIL_STRUCT_FIELD, Op.HLIL_DEREF_FIELD})

def _safe_list(value: Any) -> list:
    """Normalize Binja's sometimes-tuple/sometimes-single child accessors."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def walk_expr(expr: Any, visitor) -> None:
    """Depth-first walk of an HLIL expression tree (binja-module.md § 6).

    ``visitor(node)`` is invoked on every node (including leaves) BEFORE
    descending into children. Works on both real Binja instructions and the
    fake instructions from tests/fake_binja.py.

    This is deliberately structural (no import of the live ``func``), so it
    can run with or without Binja installed.
    """
    if expr is None:
        return
    visitor(expr)

    op = expr.operation
    if op in _BINARY_OPS:
        walk_expr(getattr(expr, "left", None), visitor)
        walk_expr(getattr(expr, "right", None), visitor)
        return
    if op in _UNARY_SRC_OPS:
        walk_expr(getattr(expr, "src", None), visitor)
        return
    if op == Op.HLIL_ASSIGN:
        walk_expr(getattr(expr, "dest", None), visitor)
        walk_expr(getattr(expr, "src", None), visitor)
        return
    if op in (Op.HLIL_VAR_INIT, Op.HLIL_VAR_DECLARE):
        walk_expr(getattr(expr, "src", None), visitor)
        return
    if op in (Op.HLIL_CALL, Op.HLIL_TAILCALL, Op.HLIL_INTRINSIC):
        walk_expr(getattr(expr, "dest", None), visitor)
        for param in _safe_list(getattr(expr, "params", None)):
            walk_expr(param, visitor)
        return
    if op == Op.HLIL_RET:
        for val in _safe_list(getattr(expr, "src", None)):
            walk_expr(val, visitor)
        return
    if op in _ARRAY_OPS:
        walk_expr(getattr(expr, "src", None), visitor)
        walk_expr(getattr(expr, "index", None), visitor)
        return
    if op == Op.HLIL_IF:
        walk_expr(getattr(expr, "condition", None), visitor)
        return
    if op in (Op.HLIL_WHILE, Op.HLIL_DO_WHILE):
        walk_expr(getattr(expr, "condition", None), visitor)
        return
    if op == Op.HLIL_FOR:
        walk_expr(getattr(expr, "init", None), visitor)
        walk_expr(getattr(expr, "condition", None), visitor)
        walk_expr(getattr(expr, "update", None), visitor)
        return
    if op == Op.HLIL_SWITCH:
        walk_expr(getattr(expr, "condition", None), visitor)
        return
    # HLIL_VAR_PHI / HLIL_MEM_PHI / HLIL_BLOCK / leaves / anything else:
    # rely on .operands (mirrors instr.operands) if present, otherwise stop.
    operands = getattr(expr, "operands", None)
    for child in _safe_list(operands):
        walk_expr(child, visitor)


__all__ = [
    "binaryninja",
    "Op",
    "SymbolType",
    "TypeClass",
    "VariableSourceType",
    "BINJA_AVAILABLE",
    "require_binja",
    "walk_expr",
    "_BINARY_OPS",
    "_UNARY_SRC_OPS",
]

