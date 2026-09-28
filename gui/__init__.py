"""reaper.gui — live "Run Cockpit" monitoring + run launcher.

A local web UI (FastAPI + WebSocket) that hosts and monitors a REAPER
pipeline run live: rename board, active agent dialogues (including model
reasoning/thinking traces), claims & task queue, call graph, LLM health, a
filterable trace feed, and loop/limit counters. Runs standalone (--detach)
so a long run can be monitored from anywhere via the browser or the pure-JSON
pull APIs (/api/state, /api/events?after=...).

See docs/skills/telemetry.md for the event/instrumentation contract this
server consumes.
"""
