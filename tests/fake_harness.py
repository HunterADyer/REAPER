"""Shared test doubles for harness_phase3 (3.1/3.2/3.4/3.5) — test-support only.

Real HLILExtractor (2.1) returns *text* for ``get_function_hlil`` /
``get_function_signature`` and list[dict] for the serialized lookups, and
addresses are hex STRINGS. The ``FakeExtractor`` in fake_binja.py returns
objects and expects int addresses — fine for graph builders, but not for the
string-text contract ContextAssembler must be built against.

``ScriptedExtractor`` bridges that gap: it mimics the REAL 2.1 contract
(string addresses, text HLIL) so ContextAssembler/MergeAgent tests exercise
the exact code paths that run in production, from deterministic scripts.

Also provides ``StubLLM`` (recording, scripted JSON responses) and a
tag-recording ``FakeBinaryView`` for BNDBWriter tests.
"""

from __future__ import annotations

from tests.fake_binja import FakeBinaryView, FakeExtractor


class ScriptedExtractor(FakeExtractor):
    """FakeExtractor conforming to the REAL string/text HLILExtractor contract.

    ``get_function_hlil`` returns text (with ``0xADDR:`` line prefixes) and
    ``get_function_signature`` returns a prototype string — exactly what
    ``reaper.tools.hlil_extract.HLILExtractor`` is specified to return
    (design § 2.1).
    """

    def __init__(self, hlils=None, signatures=None, refs=None, params=None,
                 variables=None, ranges=None, name_map=None, bv=None):
        super().__init__(bv=bv or FakeBinaryView())
        self._hlils = hlils or {}
        self._signatures = signatures or {}
        self._refs = refs or {}
        self._params = params or {}
        self._variables = variables or {}
        self._ranges = ranges or {}
        self._name_map = name_map or {}

    # -- string/text contract (mirrors real HLILExtractor) --------------------

    def get_function_hlil(self, address):
        return self._hlils.get(self._norm(address))

    def get_function_signature(self, address):
        return self._signatures.get(self._norm(address))

    def get_string_refs(self, address=None):
        if address is None:
            out = []
            for refs in self._refs.values():
                out.extend(refs)
            return out
        return self._refs.get(self._norm(address)) or []

    def get_parameters(self, address=None, name=None):
        if address is not None:
            return self._params.get(self._norm(address)) or []
        out = []
        for p in self._params.values():
            out.extend(p)
        return out

    def get_variables(self, address=None, name=None):
        if address is not None:
            return self._variables.get(self._norm(address)) or []
        out = []
        for v in self._variables.values():
            out.extend(v)
        return out

    def get_hlil_range(self, address_start, address_end):
        key = (self._norm(address_start), self._norm(address_end))
        return self._ranges.get(key)

    @staticmethod
    def _norm(address):
        if isinstance(address, int):
            return f"0x{address:x}"
        return str(address)

class _StubLLMSession:
    """Minimal per-session stub state."""

    def __init__(self, system_prompt):
        self.system_prompt = system_prompt
        self.messages = []


class StubLLM:
    """Recording LLM double: create_session/send/destroy_session.

    ``responses`` is a list of raw text (JSON) returned by ``send`` in order.
    Every call is recorded on ``calls`` for assertions. When
    ``structured_model_name`` is set, ``send`` asserts a structured_output
    schema with that title was requested.
    """

    def __init__(self, responses=None, structured_model_name=None):
        self._responses = list(responses or [])
        self._sessions: dict[str, _StubLLMSession] = {}
        self.calls: list[dict] = []
        self.structured_model_name = structured_model_name

    async def create_session(self, session_id, system_prompt):
        self._sessions[session_id] = _StubLLMSession(system_prompt)
        self.calls.append({"op": "create_session", "session_id": session_id})
        return session_id

    async def send(self, session_id, message, thinking_level="low", structured_output=None):
        if self.structured_model_name is not None:
            assert structured_output is not None, "expected structured_output"
            assert structured_output.get("title") == self.structured_model_name, (
                f"expected schema {self.structured_model_name}, got "
                f"{structured_output.get('title')}"
            )
        self.calls.append({
            "op": "send",
            "session_id": session_id,
            "message": message,
            "thinking_level": thinking_level,
            "structured_output": structured_output is not None,
        })
        sess = self._sessions.get(session_id)
        if sess is not None:
            sess.messages.append({"role": "user", "content": message})
        if not self._responses:
            return "{}"
        return self._responses.pop(0)

    def destroy_session(self, session_id):
        self._sessions.pop(session_id, None)
        self.calls.append({"op": "destroy_session", "session_id": session_id})

    async def close(self):
        pass


class TagRecordingBinaryView(FakeBinaryView):
    """FakeBinaryView that records tag creation for BNDBWriter assertions."""

    def __init__(self, functions=None, strings=None, data=None):
        super().__init__(functions=functions, strings=strings, data=data or {})
        self.tags = []

    def create_tag_type(self, name, icon):
        self.tags.append(("create_tag_type", name, icon))
        return f"tagtype:{name}"

    def parse_type_string(self, type_str):
        return (type_str, None)


class RecordingTracer:
    """Recording tracer double: captures every log() call in memory."""

    def __init__(self):
        self.events = []

    async def log(self, event_type, session_id, data):
        self.events.append({
            "event_type": event_type, "session_id": session_id, "data": dict(data or {}),
        })
        return None


def make_extractor_with_tags(**view_kwargs):
    """Build a FakeExtractor backed by a TagRecordingBinaryView."""
    bv = TagRecordingBinaryView(**view_kwargs)
    return FakeExtractor(bv=bv)


__all__ = [
    "ScriptedExtractor",
    "StubLLM",
    "TagRecordingBinaryView",
    "RecordingTracer",
    "make_extractor_with_tags",
]

