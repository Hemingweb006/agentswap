"""Vendor adapters. Each one lowers an on-disk session format into the IR.

Adding a vendor means adding a module here that exposes `read(path) -> Session`,
a `SOURCE` string, and a `default_root()`. Nothing else in the codebase changes.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..ir import Session
from . import antigravity, claude_code, codex

READERS = {
    claude_code.SOURCE: claude_code,
    codex.SOURCE: codex,
    antigravity.SOURCE: antigravity,
}

_CLAUDE_KEYS = {"sessionId", "parentUuid", "isSidechain", "toolUseResult"}
_CODEX_TYPES = {
    "response_item",
    "event_msg",
    "session_meta",
    "turn_context",
    "compacted",
    "inter_agent_communication",
    "world_state",
}

SNIFF_LINES = 40


def detect(path: str | Path) -> str | None:
    """Identify which vendor wrote a transcript by looking at its records.

    Filename and location are unreliable -- people move these files around, and
    both vendors use the .jsonl extension. Sniff the contents instead.
    """
    path = Path(path)
    if path.suffix == ".zst" or path.name.startswith("rollout-"):
        return codex.SOURCE
    # Antigravity logs are identified by where they sit, not by their contents:
    # brain/<conversation-id>/.system_generated/logs/*.jsonl
    if ".system_generated" in path.parts or "brain" in path.parts:
        return antigravity.SOURCE

    claude_hits = 0
    codex_hits = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= SNIFF_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if _CLAUDE_KEYS & record.keys():
                    claude_hits += 1
                rtype = str(record.get("type", "")).lower().replace("-", "_")
                if rtype in _CODEX_TYPES or "payload" in record:
                    codex_hits += 1
    except OSError:
        return None

    if not claude_hits and not codex_hits:
        return None
    return claude_code.SOURCE if claude_hits >= codex_hits else codex.SOURCE


def read(path: str | Path, source: str | None = None) -> Session:
    """Parse a transcript, detecting the vendor unless one is given."""
    source = source or detect(path)
    if source is None:
        raise ValueError(
            f"could not identify the format of {path}. "
            f"Pass --source with one of: {', '.join(sorted(READERS))}"
        )
    reader = READERS.get(source)
    if reader is None:
        raise ValueError(f"no reader for source {source!r}")
    return reader.read(path)


def find(reference: str) -> list[Path]:
    """Every local session matching an id, a partial id, a filename or a path.

    The two vendors bury the id differently -- Claude Code names the file
    `<id>.jsonl`, Codex names it `rollout-<timestamp>-<id>.jsonl` -- so match
    on containment rather than on either layout. Newest first.
    """
    direct = Path(reference).expanduser()
    if direct.exists():
        return [direct]

    stem = direct.name
    for suffix in (".jsonl.zst", ".jsonl"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if not stem:
        return []

    matches = [path for _, path in discover() if stem in path.name]
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches


def resolve(reference: str) -> Path | None:
    """The single best match for a reference, or None."""
    matches = find(reference)
    return matches[0] if matches else None


def session_id_of(path: Path) -> str:
    """Recover the session id from a filename, whichever vendor wrote it."""
    name = path.name
    for suffix in (".jsonl.zst", ".jsonl"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name.startswith("rollout-"):
        # rollout-<ISO timestamp>-<uuid>; the uuid is the last 5 dash groups.
        parts = name.split("-")
        if len(parts) >= 6:
            return "-".join(parts[-5:])
    return name


def discover(roots: list[Path] | None = None) -> list[tuple[str, Path]]:
    """Find local transcripts across every vendor we know about."""
    found: list[tuple[str, Path]] = []
    for name, module in READERS.items():
        root = module.default_root()
        if not root.exists():
            continue
        patterns = getattr(module, "LOG_GLOB", None)
        globs = (patterns,) if patterns else ("**/*.jsonl", "**/*.jsonl.zst")
        for pattern in globs:
            for path in root.glob(pattern):
                found.append((name, path))
    found.sort(key=lambda pair: pair[1].stat().st_mtime, reverse=True)
    return found


__all__ = ["claude_code", "codex", "READERS", "detect", "read", "discover"]
