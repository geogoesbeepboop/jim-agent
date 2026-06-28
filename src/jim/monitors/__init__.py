"""Phase 4 — continuous monitors (the "motley crew").

A monitor re-runs research on a schedule, diffs the fresh facts against its last
baseline, and lets a deterministic crew of triggers + a materiality gate decide
whether anything changed. Only a material change pays for an LLM update and
pushes to subscribers — every output stays general and fully cited.

Public surface::

    from jim.monitors import create_monitor, run_monitor_once, MonitorScheduler
"""

from jim.monitors.create import create_monitor, parse_watch_spec
from jim.monitors.engine import run_monitor_once
from jim.monitors.models import Monitor, MonitorRun, Signal, TriggerSpec
from jim.monitors.scheduler import MonitorScheduler

__all__ = [
    "Monitor",
    "MonitorRun",
    "Signal",
    "TriggerSpec",
    "create_monitor",
    "parse_watch_spec",
    "run_monitor_once",
    "MonitorScheduler",
]
