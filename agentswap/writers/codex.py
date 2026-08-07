"""Write a resumable Codex session from a Handoff.

Codex discovers sessions by scanning ~/.codex/sessions/YYYY/MM/DD/ for
rollout-<timestamp>-<uuid>.jsonl files. Each line is a RolloutLine wrapping a
tagged RolloutItem, so a minimal resumable session needs three of them:
session_meta to establish identity and cwd, turn_context for model settings,
and one event_msg carrying the brief as a user message.

UNVERIFIED, and with one known risk beyond the usual: Codex keeps a SQLite index
(state_5.sqlite) alongside the rollout files and the desktop app reads from it.
The docs describe a filesystem backfill on StateRuntime init, so a dropped-in
file should get indexed eventually -- but if `codex resume` does not list the
session, an unindexed file is the first thing to suspect.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..handoff import Handoff

TARGET = "codex"

DEFAULT_MODEL = "gpt-5-codex"

# Fallback only. A hardcoded version is a lie that ages badly -- the value below
# was a guess, and the first real machine we ran against was on
# 0.100.0-alpha.10. Prefer whatever the user's own sessions report.
FALLBACK_VERSION = "0.48.2"


def default_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def detect_version(root: Path | None = None) -> str:
    """Read the installed CLI version out of the newest local session.

    Writing a session stamped with a version the user does not run invites
    subtle mismatches, so copy what their own tool most recently wrote.
    """
    root = root or default_root()
    if not root.exists():
        return FALLBACK_VERSION
    candidates = sorted(
        root.glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for path in candidates[:5]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for _ in range(20):
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = record.get("payload")
                    if isinstance(payload, dict) and payload.get("cli_version"):
                        return str(payload["cli_version"])
        except OSError:
            continue
    return FALLBACK_VERSION


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _line(dt: datetime, kind: str, payload: dict) -> dict:
    return {"timestamp": _stamp(dt), "type": kind, "payload": payload}


def build_records(
    handoff: Handoff,
    session_id: str,
    when: datetime | None = None,
    version: str | None = None,
) -> list[dict]:
    when = when or _now()
    cwd = handoff.cwd or os.getcwd()

    meta = {
        "id": session_id,
        "source": "cli",
        "cwd": cwd,
        "cli_version": version or FALLBACK_VERSION,
        "originator": "agentswap",
    }
    if handoff.git_branch:
        meta["git_branch"] = handoff.git_branch

    return [
        _line(when, "session_meta", meta),
        _line(
            when,
            "turn_context",
            {
                "model": handoff.model or DEFAULT_MODEL,
                "approval_policy": "on-request",
                "sandbox_policy": "workspace-write",
                "cwd": cwd,
            },
        ),
        _line(when, "event_msg", {"type": "UserMessage", "message": handoff.render()}),
    ]


def target_path(session_id: str, root: Path | None = None, when: datetime | None = None) -> Path:
    when = when or _now()
    root = root or default_root()
    stamp = when.strftime("%Y-%m-%dT%H-%M-%S")
    return root / when.strftime("%Y") / when.strftime("%m") / when.strftime("%d") / f"rollout-{stamp}-{session_id}.jsonl"


def write(
    handoff: Handoff,
    root: Path | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
) -> tuple[Path, str, list[dict]]:
    """Emit the rollout file. Returns (path, session_id, records)."""
    session_id = session_id or str(uuid.uuid4())
    when = _now()
    path = target_path(session_id, root, when)
    records = build_records(handoff, session_id, when, version=detect_version(root))

    if dry_run:
        return path, session_id, records

    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path, session_id, records


def resume_command(session_id: str, cwd: str) -> str:
    return f"cd {cwd!r} && codex resume {session_id}"
