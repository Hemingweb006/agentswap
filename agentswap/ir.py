"""Vendor-neutral intermediate representation for agent sessions.

Every reader lowers a vendor's on-disk session format into these types, and
every writer raises them back into some other vendor's format. Nothing outside
`readers/` and `writers/` is allowed to know what Claude Code or Codex actually
put on disk -- that is the whole point. Their schemas change without notice, so
the blast radius of a change has to stay inside one adapter file.

Design rule: the IR carries *meaning*, never *payload*. Tool results and
attachments are recorded as digests plus a byte count, never as content. On a
real session that is the difference between 5.7 MB and 42 KB.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


MAX_DIGEST = 400


_PATCH_MARKERS = ("*** Update File:", "*** Add File:", "*** Delete File:", "--- a/", "+++ b/")


def compact_patch(value: str) -> str | None:
    """Reduce a patch body to just the lines naming files.

    Patch payloads routinely run to tens of kilobytes, so the digest limit
    truncates them -- and on real data that silently ate every file after the
    first, because the filenames are spread through the body rather than
    bunched at the front. Keeping only the file markers preserves what a
    handoff needs at a fraction of the size.
    """
    if not isinstance(value, str) or not any(m in value for m in _PATCH_MARKERS):
        return None
    kept = [
        line.strip()
        for line in value.splitlines()
        if any(line.lstrip().startswith(m) for m in _PATCH_MARKERS)
    ]
    return "\n".join(kept) if kept else None


def digest(value: Any, limit: int = MAX_DIGEST) -> str:
    """Collapse an arbitrary value to a short, human-readable stand-in."""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... <+{len(value) - limit} chars>"


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""

    @property
    def summary(self) -> str:
        if not self.args:
            return self.name
        inner = ", ".join(f"{k}={v}" for k, v in self.args.items())
        return f"{self.name}({inner})"


@dataclass
class ToolResult:
    call_id: str = ""
    name: str = ""
    preview: str = ""
    raw_bytes: int = 0
    is_error: bool = False


@dataclass
class Turn:
    index: int
    role: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    timestamp: str = ""
    uid: str = ""
    parent: str = ""
    sidechain: bool = False
    dropped_media: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.text or self.tool_calls or self.tool_results)


@dataclass
class Session:
    """A single agent conversation, normalized."""

    source: str = ""
    session_id: str = ""
    cwd: str = ""
    git_branch: str = ""
    agent_version: str = ""
    started: str = ""
    ended: str = ""
    turns: list[Turn] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def user_turns(self) -> list[Turn]:
        """Turns a human actually typed.

        Claude Code files tool results as role="user" messages, so a naive
        role filter overcounts badly -- 108 instead of 11 on the session this
        was built against. A real human turn carries text and no tool result.
        """
        return [
            t
            for t in self.turns
            if t.role == "user" and not t.sidechain and t.text and not t.tool_results
        ]

    def tool_histogram(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for turn in self.turns:
            for call in turn.tool_calls:
                counts[call.name] = counts.get(call.name, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def semantic_bytes(self) -> int:
        """Bytes we would actually carry across a handoff."""
        total = 0
        for turn in self.turns:
            total += len(turn.text)
            for call in turn.tool_calls:
                total += len(call.summary)
            for res in turn.tool_results:
                total += len(res.preview)
        return total

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
