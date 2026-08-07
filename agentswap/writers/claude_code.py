"""Write a resumable Claude Code session from a Handoff.

Claude Code discovers sessions by scanning ~/.claude/projects/<slug>/<id>.jsonl,
where <slug> is the working directory with its separators flattened. Drop a
well-formed file there and `claude --resume <id>` will offer it like any other
session -- this is the same trick sessionforge uses.

The emitted session is deliberately tiny: a single user message containing the
brief. We are not reconstructing the old conversation, we are starting a new one
that happens to know everything the old one knew.

UNVERIFIED: the resume handshake has not been exercised end to end from here,
because Claude Code is not installed in the environment this was built in. The
record shape mirrors observed transcripts from version 2.1.219 field for field.
Run `port` with --dry-run first, then try resuming, and report what happens.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..handoff import Handoff

TARGET = "claude-code"

# Fallback only -- prefer whatever the user's own sessions report.
FALLBACK_VERSION = "2.1.219"


def default_root() -> Path:
    return Path.home() / ".claude" / "projects"


def detect_version(root: Path | None = None) -> str:
    """Read the installed Claude Code version out of the newest local session."""
    root = root or default_root()
    if not root.exists():
        return FALLBACK_VERSION
    candidates = sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates[:5]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for _ in range(40):
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict) and record.get("version"):
                        return str(record["version"])
        except OSError:
            continue
    return FALLBACK_VERSION


def slugify_cwd(cwd: str) -> str:
    """Flatten a working directory into Claude Code's project folder name.

    Derived from a real path:

      /Users/reda/Library/Application Support/Claude/local_87a344e6/outputs
      -Users-reda-Library-Application-Support-Claude-local-87a344e6-outputs

    Separators, spaces and underscores all collapse to '-'. Dot handling is
    inferred rather than observed -- no sample path contained one.
    """
    slug = cwd or ""
    for ch in ("/", " ", "_", "."):
        slug = slug.replace(ch, "-")
    return slug


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _envelope(
    session_id: str, cwd: str, branch: str, uid: str, parent: str | None, version: str
) -> dict:
    return {
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": cwd,
        "sessionId": session_id,
        "version": version,
        "gitBranch": branch,
        "uuid": uid,
        "timestamp": _now(),
    }


def build_records(
    handoff: Handoff,
    session_id: str,
    version: str | None = None,
    body: str | None = None,
) -> list[dict]:
    """The JSONL records for a fresh session seeded with the brief.

    `body` lets the caller supply an already-rendered brief -- the session loop
    appends a confirm-or-continue instruction that the writer should not have
    to know about.
    """
    cwd = handoff.cwd or os.getcwd()
    branch = handoff.git_branch or ""
    uid = str(uuid.uuid4())

    record = _envelope(session_id, cwd, branch, uid, None, version or FALLBACK_VERSION)
    record["type"] = "user"
    record["message"] = {
        "role": "user",
        "content": [{"type": "text", "text": body if body is not None else handoff.render()}],
    }
    return [record]


def target_path(handoff: Handoff, session_id: str, root: Path | None = None) -> Path:
    root = root or default_root()
    return root / slugify_cwd(handoff.cwd or os.getcwd()) / f"{session_id}.jsonl"


def write(
    handoff: Handoff,
    root: Path | None = None,
    session_id: str | None = None,
    dry_run: bool = False,
    body: str | None = None,
) -> tuple[Path, str, list[dict]]:
    """Emit the session file. Returns (path, session_id, records).

    Never overwrites: a fresh session id is minted every call, and an existing
    file at the target path is treated as a hard error rather than clobbered.
    """
    session_id = session_id or str(uuid.uuid4())
    path = target_path(handoff, session_id, root)
    records = build_records(handoff, session_id, version=detect_version(root), body=body)

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
    return f"cd {cwd!r} && claude --resume {session_id}"
