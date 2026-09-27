"""Shared standalone-compile support for PrimeVul post-testing.

PrimeVul `func` payloads are C/C++ function bodies extracted from real
projects — they routinely reference project types / functions that do not
exist in a standalone translation unit. This module makes as many of them
compile as possible *without* pulling in project headers:

  * auto-shims opaque types (``FlatpakDir *d`` -> ``typedef struct
    FlatpakDir FlatpakDir;``) so type errors are avoided;
  * downgrades implicit-function-declaration / return-type warnings to
    warnings (gcc 13 defaults some to hard errors);
  * tolerates unused variables/params and int<->pointer conversions.

Functions whose project produces undefined *link* references (e.g. calling
``flatpak_dir_ensure_bundle_rem``) still fail at link time and are skipped —
that is inherent and documented; every sample that does compile is a real,
self-contained stripped binary with original identifiers as ground truth.

Shared by build_dataset.py (sampling-time compile gate) and post_test.py.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

PRE_HEADERS = (
    "<stdlib.h>", "<string.h>", "<stdio.h>", "<limits.h>", "<stdint.h>",
    "<stdbool.h>", "<stddef.h>", "<math.h>", "<errno.h>", "<time.h>",
)

_KEYWORDS = set(
    "auto break case char const continue default do double else enum extern "
    "float for goto if inline int long register return short signed sizeof "
    "static struct switch typedef union unsigned void volatile while"
    .split()
)

_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
# `FlatpakDir self` or `TALLOC_CTX *ctx` -> identifier used as a type.
_TYPE_USE_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]{2,})\s*(\*)?\s+[a-z_][A-Za-z0-9_]*\s*"
    r"(?:\[|[,;)=*])"
)

GCC_EXTRA_FLAGS = [
    "-Wno-implicit-function-declaration", "-Wno-return-type",
    "-Wno-int-conversion", "-Wno-unused-variable", "-Wno-unused-parameter",
    "-Wno-unused-function",
]


def extract_func_name(func: str) -> tuple[str | None, str | None]:
    """Return (name, err). Looks for the function signature's identifier."""
    m = re.search(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", func)
    if not m:
        return None, "no function signature found"
    name = m.group(1)
    if name in _KEYWORDS:
        return None, "bad function name"
    return name, None


def _defined_names(func: str) -> set[str]:
    """Names that are function *definitions* in this TU (can't predeclare)."""
    return {m.group(1) for m in re.finditer(
        r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", func)}


_CALL_RE = re.compile(r"(?<![\w.])([a-z_]\w*)\s*\(")


def build_predecls(func: str) -> str:
    """extern-declare unknown lowercase call targets to avoid implicit decls.

    Only targets that are (a) not keywords, (b) not defined in this TU, and
    (c) not obviously types get a header-less `extern int name(void);`. This
    is harmless for real libc functions we might shadow? No — printf IS
    declared by <stdio.h> with a conflicting prototype, so we must guard each
    name the same way we guard shim typedefs: only when not already defined.
    """
    defined = _defined_names(func) | _KEYWORDS
    lines = []
    for m in _CALL_RE.finditer(func):
        n = m.group(1)
        if n in defined or n in ("size_t",):  # (size_t handled by headers)
            continue
        guard = f"REAPER_PREDECL_{n.upper()}"
        line = (f"#ifndef {guard}\n#define {guard}\n"
                f"extern int {n}(void);\n#endif\n")
        lines.append(line)
    return "".join(dict.fromkeys(lines))


def extract_original_identifiers(func: str) -> list[str]:
    """Non-keyword identifier set, ordered, deduped (heuristic locals)."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in _IDENT_RE.findall(func):
        if tok in _KEYWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def build_shim(func: str) -> str:
    """Generate guarded opaque typedefs for unknown PascalCase types."""
    lines = []
    for m in _TYPE_USE_RE.finditer(func):
        t = m.group(1)
        guard = f"REAPER_SHIM_{t.upper()}"
        lines.append(
            f"#ifndef {guard}\n#define {guard}\ntypedef struct {t} {t};"
            f"\n#endif\n"
        )
    return "".join(dict.fromkeys(lines))  # dedupe, keep order


def build_c(fname: str, func: str) -> str:
    """Assemble a standalone translation unit for a PrimeVul function."""
    headers = "\n".join(f"#include {h}" for h in PRE_HEADERS)
    return (f"{headers}\n{build_shim(func)}\n{func}\n"
            f"int main(void) {{ (void){fname}(); return 0; }}\n")


def compile_standalone(func: str, out_dir: pathlib.Path, sample_id: str,
                       gcc: str = "gcc") -> tuple[bool, pathlib.Path | None, str, str | None]:
    """Compile+strip a standalone function. Returns (ok, bin, msg, fname)."""
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname, err = extract_func_name(func)
    if err or not fname:
        return False, None, err or "no name", None
    src = out_dir / f"{sample_id}.c"
    src.write_text(build_c(fname, func), encoding="utf-8")
    bin_path = out_dir / f"{sample_id}.bin"
    cmd = [gcc, "-O2", "-g", *GCC_EXTRA_FLAGS, str(src),
           "-o", str(bin_path), "-lm", "-lpthread"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return False, None, (r.stderr or r.stdout)[-220:].strip(), fname
    subprocess.run(["strip", str(bin_path)], check=True)
    return True, bin_path, "ok", fname
