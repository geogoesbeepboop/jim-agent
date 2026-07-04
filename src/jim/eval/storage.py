"""Persisted eval runs — the harness's memory.

"Is jim improving?" is unanswerable from a single eval printout; it needs a
history. Every ``jim-eval run`` saves one self-contained JSON document under
``EVAL_RUNS_DIR`` (default ``./eval_runs``), stamped with the git commit and the
config that produced it, so any two points in time can be diffed and the results
UI can plot trends. Plain files on purpose: no database dependency (offline-first,
like the rest of jim), diffable, and trivially portable between machines.

A ``BASELINE`` marker file names the run future runs are judged against —
promote a known-good run once, and every ``--compare-baseline`` after that is a
regression check against it.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from jim.config import get_settings

SCHEMA_VERSION = 1
_BASELINE_FILE = "BASELINE"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")  # run ids stay filesystem-safe


def runs_dir() -> Path:
    d = Path(get_settings().eval_runs_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def git_info() -> dict:
    """Best-effort commit stamp; evals still run outside a git checkout."""
    info = {"sha": None, "branch": None}
    for key, args in (
        ("sha", ["git", "rev-parse", "--short=7", "HEAD"]),
        ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
    ):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                info[key] = out.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return info


def new_run_id(now: datetime | None = None, sha: str | None = None) -> str:
    """Timestamp + short commit: sortable by time, attributable to code."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-{sha or 'nogit'}"
    run_id, n = base, 2
    while (runs_dir() / f"{run_id}.json").exists():
        run_id = f"{base}.{n}"
        n += 1
    return run_id


def save_run(run: dict) -> Path:
    run_id = run["run_id"]
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(f"unsafe run id: {run_id!r}")
    path = runs_dir() / f"{run_id}.json"
    path.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
    return path


def list_run_ids() -> list[str]:
    """All persisted run ids, oldest first (ids sort chronologically)."""
    return sorted(p.stem for p in runs_dir().glob("*.json"))


def resolve_run_id(ref: str) -> str:
    """Resolve ``latest`` / ``baseline`` / a unique id prefix to a run id."""
    ids = list_run_ids()
    if ref == "latest":
        if not ids:
            raise FileNotFoundError("no eval runs saved yet — run `jim-eval run` first")
        return ids[-1]
    if ref == "baseline":
        baseline = get_baseline()
        if baseline is None:
            raise FileNotFoundError("no baseline set — run `jim-eval baseline set <run_id>`")
        return baseline
    if ref in ids:
        return ref
    matches = [i for i in ids if i.startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"no eval run matches {ref!r}")
    raise FileNotFoundError(f"{ref!r} is ambiguous: {', '.join(matches[:5])}")


def load_run(ref: str) -> dict:
    run_id = resolve_run_id(ref)
    path = runs_dir() / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def list_runs() -> list[dict]:
    """Light summaries of every run (for the index table and trend charts)."""
    out = []
    for run_id in list_run_ids():
        try:
            run = load_run(run_id)
        except (OSError, json.JSONDecodeError):
            continue  # a torn/foreign file must not break the whole index
        out.append(
            {
                "run_id": run.get("run_id", run_id),
                "label": run.get("label"),
                "started_at": run.get("started_at"),
                "duration_seconds": run.get("duration_seconds"),
                "git": run.get("git", {}),
                "suites_run": sorted((run.get("suites") or {}).keys()),
                "summary": run.get("summary", {}),
            }
        )
    return out


def get_baseline() -> str | None:
    path = runs_dir() / _BASELINE_FILE
    if not path.exists():
        return None
    ref = path.read_text(encoding="utf-8").strip()
    return ref or None


def set_baseline(ref: str) -> str:
    run_id = resolve_run_id(ref)  # only an existing run can be the baseline
    (runs_dir() / _BASELINE_FILE).write_text(run_id + "\n", encoding="utf-8")
    return run_id


def clear_baseline() -> None:
    path = runs_dir() / _BASELINE_FILE
    if path.exists():
        path.unlink()
