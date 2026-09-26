"""REAPER — Reverse Engineering Agent Pipeline (RE Stage).

Fully async Python harness that uses LLM agents (via local vLLM) to
reverse-engineer stripped binaries using Binary Ninja HLIL dataflow graphs.

The repository root IS this package: the design doc's ``reaper/...`` paths
(e.g. ``reaper/harness/``) map directly onto this directory. See
``pyproject.toml`` -> ``[tool.setuptools.package-dir]`` for the packaging glue.
"""

__version__ = "0.1.0"
