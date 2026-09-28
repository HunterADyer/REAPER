"""Fake Binary Ninja API for unit-testing REAPER tools without Binja.

Binja 6.0 headless is NOT pip-installable and may be absent. Every Binja-touching
tool guards its import behind ``reaper.tools._compat`` and compares instruction
operations against ``Op`` (the real ``HighLevelILOperation`` when present, else
the identical fallback enum). This module builds fake HLIL/function/view objects
whose operations ARE the ``Op`` members, so tool logic and tests behave the same
with or without Binja installed. Test-support code only.
"""

from __future__ import annotations

import copy

from reaper.tools._compat import (
    Op,
    SymbolType,
    TypeClass,
    VariableSourceType,
)


class FakeVariable:
    """Mirror of a Binja Variable (binja-module §§ 3, 12.4)."""

    def __init__(self, name, type_str="$unknown", source=None, index=None,
                 identifier=None):
        self.name = name
        self.type = FakeType(type_str)
        self.type_str = type_str
        self.source_type = source or VariableSourceType.StackVariableSourceType
        self.index = index
        # Binja 6.0 has variable.identifier; two references to the same var
        # share it even though they are distinct Python objects.
        self.identifier = identifier if identifier is not None else name

    def clone(self):
        return copy.deepcopy(self)

    def __repr__(self):
        return f"<FakeVariable {self.name} : {self.type_str}>"


class FakeType:
    """Duck-typed stand-in for Binja's Type (binja-module § 9)."""

    def __init__(self, type_str="$unknown", type_class=None, target=None,
                 structure=None, members=None):
        self.type_str = type_str
        self.type_class = type_class
        if type_class is None and type_str:
            if type_str.endswith("*"):
                self.type_class = TypeClass.PointerTypeClass
            elif type_str.startswith("struct"):
                self.type_class = TypeClass.StructureTypeClass
            elif type_str.startswith("("):
                self.type_class = TypeClass.FunctionTypeClass
            elif type_str in ("int64_t", "int32_t", "int16_t", "int8_t",
                              "uint64_t", "uint32_t", "uint16_t", "uint8_t",
                              "bool", "char", "size_t"):
                self.type_class = TypeClass.IntegerTypeClass
            else:
                self.type_class = TypeClass.VoidTypeClass
        self.target = target
        self.structure = structure
        self.members = members or []

    def __str__(self):
        return self.type_str

    def __eq__(self, other):
        return isinstance(other, FakeType) and self.type_str == other.type_str


class FakeStringRef:
    """Mirror of Binja's StringReference (binja-module § 7)."""

    def __init__(self, start, value, str_type="Ascii", length=None):
        self.start = start
        self.value = value
        self.type = str_type
        self.length = length if length is not None else len(value) + 1


class FakeHighLevelILInstruction:
    """Mirror of a Binja HighLevelIL expression (binja-module §§ 5-6).

    ``operation`` is an ``Op`` member (real enum or identical fallback), so
tools comparing ``instr.operation == Op.HLIL_CALL`` work unchanged. Children
are attributes matching the real API surface REAPER touches: ``src``, ``dest``,
``params``, ``left``, ``right``, ``condition``, ``index``, ``init``, ``update``,
``var``, ``constant``, ``operands``. ``str(instr)`` renders decompiler-like text.
    """

    def __init__(self, operation, address=0, **children):
        self.operation = operation
        self.address = address
        self.var = None
        self.src = None
        self.dest = None
        self.params = ()
        self.left = None
        self.right = None
        self.condition = None
        self.index = None
        self.init = None
        self.update = None
        self.constant = None
        self.operands = ()
        self.hlil = None  # set by FakeHighLevelILFunction on append
        self.true = None
        self.false = None
        for key, value in children.items():
            setattr(self, key, value)

    def __str__(self):
        cls = self.operation.name if hasattr(self.operation, "name") else str(self.operation)
        return f"{cls} @ {hex(self.address)}"

    def __repr__(self):
        return f"<fake {self}>"

    def operands_of(self):
        """Return the child expressions as a flat tuple (for tests only)."""
        return self.operands


class FakeHighLevelILFunction:
    """Mirror of func.hlil (binja-module § 4). ``instructions`` is the only
    sanctioned iteration surface - there is no ``root`` in Binja 6.0."""

    def __init__(self, instructions=None):
        self.instructions = list(instructions or [])
        for ins in self.instructions:
            ins.hlil = self

    @property
    def ssa_form(self):
        return self  # fakes are non-SSA; keep the attr for interface parity

    def __getitem__(self, key):
        # binja-module § 12.3: HLIL_IF true/false are instruction INDICES
        if isinstance(key, int) and -len(self.instructions) <= key < len(self.instructions):
            return self.instructions[key]
        raise KeyError(key)

    def __len__(self):
        return len(self.instructions)

    def append(self, instruction):
        instruction.hlil = self
        self.instructions.append(instruction)


class FakeFunction:
    """Mirror of a Binja Function (binja-module §§ 2-4)."""

    def __init__(self, address, name=None, instructions=None, variables=None,
                 parameters=None, return_type="void", symbol_name=None,
                 is_imported=False, func_addr_override=None):
        self.start = address
        self._name = name or f"sub_{address:x}"
        self.original_name = self._name
        self.symbol = FakeSymbol(symbol_name or self._name, address,
                                 SymbolType.FunctionSymbol)
        self._imported = is_imported
        self.func_addr_override = func_addr_override
        self.vars = list(variables or [])
        self.parameter_vars = list(parameters or [])
        self.return_type = FakeType(return_type)
        hlil_func = FakeHighLevelILFunction(instructions)
        self.hlil = hlil_func
        for ins in hlil_func.instructions:
            ins.hlil = hlil_func
        self.basic_blocks = []

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def symbol_object(self):
        return self.symbol

    def __repr__(self):
        return f"<FakeFunction {self.name} @ {hex(self.start)}>"

    @classmethod
    def simple(cls, address, name, instructions=None, variables=None,
               parameters=None, return_type="void", func_addr_override=None,
               is_imported=False):
        return cls(address, name=name, instructions=instructions,
                   variables=variables, parameters=parameters,
                   return_type=return_type,
                   func_addr_override=func_addr_override, is_imported=is_imported)


class FakeSymbol:
    """Mirror of Binja Symbol (binja-module § 8)."""

    def __init__(self, name, address, symbol_type=SymbolType.FunctionSymbol):
        self.name = name
        self.address = address
        self.type = symbol_type


class FakeBinaryView:
    """Mirror of a Binja BinaryView (binja-module §§ 1, 7, 8)."""

    def __init__(self, functions=None, strings=None, data=None):
        self.functions = list(functions or [])
        self._strings = list(strings or [])
        self._data = dict(data or {})  # address -> bytes

    @property
    def strings(self):
        return list(self._strings)

    def get_string_at(self, address):
        for s in self._strings:
            if s.start == address:
                return s
        return None

    def read(self, address, length):
        s = self.get_string_at(address)
        if s is not None:
            return s.value.encode("utf-8", "replace")
        return self._data.get(address, b"AUTO")[:length]

    def get_function_at(self, address):
        for f in self.functions:
            if f.start == address:
                return f
        return None

    def get_functions_containing(self, address):
        return [f for f in self.functions if f.start <= address]

    def get_symbols_of_type(self, symbol_type):
        out = []
        for f in self.functions:
            if f.symbol.type == symbol_type:
                out.append(f.symbol)
        return out


class FakeExtractor:
    """Duck-typed stand-in for ``reaper.tools.hlil_extract.HLILExtractor``.

    Implements the public interface the graph builders / pipeline touch
    (see design § 2.1 and docs/skills/cross-references.md). Because the graph
    constructors never import the real extractor, tests hand this fake to
    ``build_nodes`` / ``build_edges`` / ``pin_symbols`` directly. The fake
    functions carry real-ish ``FakeFunction`` objects so the HLIL-walking tools
    (graph_edges, struct_detector) can exercise realistic instruction trees;
    serialized lookups (list_functions, get_variables, get_string_refs, ...)
    return the same data the real extractor would.
    """

    def __init__(self, bv=None):
        self.bv = bv or FakeBinaryView()

    @classmethod
    def with_functions(cls, functions, strings=None, data=None):
        bv = FakeBinaryView(functions=functions, strings=strings, data=data)
        return cls(bv)

    def add_function(self, function):
        self.bv.functions.append(function)
        return self

    def set_strings(self, strings):
        self.bv._strings = list(strings)
        return self

    # -- HLILExtractor public interface (serialized + object access) ---------
    def list_functions(self):
        out = []
        for f in self.bv.functions:
            if f.basic_blocks:
                size = max(bb.end for bb in f.basic_blocks) - f.start
            else:
                size = len(f.hlil.instructions)
            out.append({"address": f.start, "name": f.name, "size": size,
                        "imported": bool(getattr(f, "_imported", False))})
        return out

    def get_function(self, address=None, name=None):
        for f in self.bv.functions:
            if address is not None and f.start == address:
                return f
            if name is not None and f.name == name:
                return f
        return None

    def get_functions(self):
        return list(self.bv.functions)

    def get_function_hlil(self, address):
        f = self.get_function(address=address)
        return f.hlil if f is not None else None

    def get_variables(self, address=None, name=None):
        funcs = []
        if address is not None or name is not None:
            f = self.get_function(address=address, name=name)
            funcs = [f] if f is not None else []
        else:
            funcs = self.bv.functions
        out = []
        for func in funcs:
            for v in func.vars:
                out.append({"name": v.name, "type": v.type_str,
                            "source": v.source_type.name, "identifier": v.identifier})
        return out

    def get_parameters(self, address=None, name=None):
        funcs = []
        if address is not None or name is not None:
            f = self.get_function(address=address, name=name)
            funcs = [f] if f is not None else []
        else:
            funcs = self.bv.functions
        out = []
        for func in funcs:
            for i, p in enumerate(func.parameter_vars):
                out.append({"name": p.name, "type": p.type_str, "index": i})
        return out

    def get_function_signature(self, address):
        f = self.get_function(address=address)
        if f is None:
            return None
        return {"name": f.name, "return_type": str(f.return_type),
                "params": [p.type_str for p in f.parameter_vars]}

    def get_string_refs(self, address=None):
        """Resolve HLIL_CONST_PTR against known strings -> [{address, value}]."""
        from reaper.tools._compat import walk_expr
        known = {s.start: s for s in self.bv._strings}
        refs = []
        for f in self.bv.functions:
            if address is not None and f.start != address:
                continue
            for ins in f.hlil.instructions:
                def _grab(node):
                    if node.operation == Op.HLIL_CONST_PTR and node.constant in known:
                        refs.append({"address": hex(node.constant),
                                     "value": known[node.constant].value})
                walk_expr(ins, _grab)
        return refs


# ---------------------------------------------------------------------------
# Builder helpers - idiomatic way to assemble fake instruction trees.
# ---------------------------------------------------------------------------

def instr(operation, address=0, **children):
    return FakeHighLevelILInstruction(operation, address=address, **children)


def var(name, address=0, type_str="$unknown", **kw):
    v = FakeVariable(name, type_str=type_str)
    return FakeHighLevelILInstruction(Op.HLIL_VAR, address=address, var=v, **kw)


def const(value, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_CONST, address=address, constant=value)


def const_ptr(value, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_CONST_PTR, address=address, constant=value)


def const_data(value, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_CONST_DATA, address=address, constant=value)


def var_init(name, src, address=0, type_str="$unknown"):
    v = FakeVariable(name, type_str=type_str)
    return FakeHighLevelILInstruction(Op.HLIL_VAR_INIT, address=address, var=v, src=src)


def assign(dest, src, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_ASSIGN, address=address, dest=dest, src=src)


def call(dest, params, address=0, target_func=None, target_addr=None):
    """Call instruction. target_func/-addr record the resolved callee; the real
    extractor resolves direct calls the same way (indirect => target=None)."""
    target = {"name": target_func, "address": target_addr} if target_func else None
    return FakeHighLevelILInstruction(Op.HLIL_CALL, address=address,
                                      dest=dest, params=tuple(params),
                                      const=target_addr, target_func=target)


def hlil_ret(*src, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_RET, address=address, src=tuple(src))


def deref(src, address=0, size=None, **kw):
    ins = FakeHighLevelILInstruction(Op.HLIL_DEREF, address=address, src=src)
    if size is not None:
        ins.size = size
    for k, v in kw.items():
        setattr(ins, k, v)
    return ins


def address_of(src, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_ADDRESS_OF, address=address, src=src)


def struct_field(src, member, offset=0, index=None, address=0, type_str="$unknown"):
    field = FakeVariable(member, type_str=type_str)
    return FakeHighLevelILInstruction(Op.HLIL_STRUCT_FIELD, address=address,
                                      var=field, src=src, index=index,
                                      member=member, offset=offset, type_ref=field)


def deref_field(src, member, offset=0, index=None, address=0, type_str="$unknown"):
    field = FakeVariable(member, type_str=type_str)
    return FakeHighLevelILInstruction(Op.HLIL_DEREF_FIELD, address=address,
                                      var=field, src=src, index=index,
                                      member=member, offset=offset)


def add(left, right, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_ADD, address=address, left=left, right=right)


def sub(left, right, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_SUB, address=address, left=left, right=right)


def hlil_if(condition, address=0, true=None, false=None):
    return FakeHighLevelILInstruction(Op.HLIL_IF, address=address,
                                      condition=condition, true=true, false=false)


def hlil_while(condition, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_WHILE, address=address, condition=condition)


def hlil_block(*instructions, address=0):
    return FakeHighLevelILInstruction(Op.HLIL_BLOCK, address=address,
                                      operands=tuple(instructions))


def fake_binary(functions=None, strings=None, data=None):
    return FakeBinaryView(functions=functions or [], strings=strings or [],
                          data=data or {})


__all__ = [
    "FakeVariable", "FakeType", "FakeStringRef", "FakeHighLevelILInstruction",
    "FakeHighLevelILFunction", "FakeFunction", "FakeSymbol", "FakeBinaryView",
    "FakeExtractor",
    "instr", "var", "const", "const_ptr", "const_data", "var_init", "assign",
    "call", "hlil_ret", "deref", "address_of", "struct_field", "deref_field",
    "add", "sub", "hlil_if", "hlil_while", "hlil_block", "fake_binary",
]
