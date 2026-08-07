"""Reader for Claude Code's on-disk session transcripts.

Sessions live at ~/.claude/projects/<slugified-cwd>/<session-id>.jsonl, one JSON
object per line. Everything in this file is derived from observation, not from
documentation -- the format is internal and will drift. When it drifts, this is
the only file that should need touching.

Observed record shape (Claude Code 2.1.219):

  type            assistant | user | last-prompt | queue-operation | mode | attachment
  uuid/parentUuid linked-list threading over the conversation
  message         {role, content, model, usage, ...} on assistant/user records
  cwd, gitBranch, version, sessionId, timestamp
  isSidechain     true for subagent turns
  toolUseResult   sidecar payload attached to some user records

Observed content block types: text, thinking, tool_use, tool_result, image.

Two things worth knowing before you trust this data:

1. `thinking` blocks are encrypted. The `thinking` field is empty and only an
   opaque `signature` survives. Reasoning is NOT portable across tools; it has
   to be reconstructed from visible text and the tool calls actually made.

2. Files are dominated by payload. On the session used to build this reader,
   base64 image attachments were 84.3% of 5.7 MB while visible text was 0.7%.
   We drop payload on the floor and keep a count.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from ..ir import Session, Turn, ToolCall, ToolResult, compact_patch, digest

SOURCE = "claude-code"

MESSAGE_TYPES = {"assistant", "user"}

ARG_KEEP_LIMIT = 200


def default_root() -> Path:
    return Path.home() / ".claude" / "projects"


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield parsed records, skipping corrupt lines rather than dying.

    Real transcripts do contain truncated final lines when a session is killed
    mid-write, so a strict parser is the wrong choice here.
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def _clean_args(raw: Any) -> dict[str, Any]:
    """Keep short scalar arguments verbatim, collapse anything bulky."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            patch = compact_patch(value)
            if patch is not None:
                out[key] = patch
                continue
            out[key] = value if len(value) <= ARG_KEEP_LIMIT else digest(value, ARG_KEEP_LIMIT)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = digest(value, ARG_KEEP_LIMIT)
    return out


def _result_text(content: Any) -> tuple[str, int]:
    """Return (preview, raw_byte_count) for a tool_result body."""
    if isinstance(content, str):
        return digest(content), len(content)
    if isinstance(content, list):
        parts: list[str] = []
        size = 0
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text = block.get("text", "")
                    parts.append(text)
                    size += len(text)
                else:
                    size += len(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return digest(" ".join(parts)), size
    blob = json.dumps(content, ensure_ascii=False) if content is not None else ""
    return digest(blob), len(blob)


def _parse_turn(record: dict[str, Any], index: int) -> Turn | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None

    turn = Turn(
        index=index,
        role=message.get("role", record.get("type", "")),
        timestamp=record.get("timestamp", ""),
        uid=record.get("uuid", ""),
        parent=record.get("parentUuid", "") or "",
        sidechain=bool(record.get("isSidechain")),
    )

    content = message.get("content")
    if isinstance(content, str):
        turn.text = content.strip()
        return turn
    if not isinstance(content, list):
        return turn

    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            texts.append(block.get("text", ""))

        elif btype == "thinking":
            # Encrypted upstream -- nothing recoverable here. Noted, not carried.
            continue

        elif btype == "image":
            turn.dropped_media += 1

        elif btype == "tool_use":
            turn.tool_calls.append(
                ToolCall(
                    name=block.get("name", "?"),
                    args=_clean_args(block.get("input")),
                    call_id=block.get("id", ""),
                )
            )

        elif btype == "tool_result":
            preview, size = _result_text(block.get("content"))
            turn.tool_results.append(
                ToolResult(
                    call_id=block.get("tool_use_id", ""),
                    preview=preview,
                    raw_bytes=size,
                    is_error=bool(block.get("is_error")),
                )
            )

    turn.text = "\n".join(t for t in texts if t).strip()
    return turn


def _link_result_names(session: Session) -> None:
    """Tool results only carry an id; recover the tool name from the call."""
    names: dict[str, str] = {}
    for turn in session.turns:
        for call in turn.tool_calls:
            if call.call_id:
                names[call.call_id] = call.name
    for turn in session.turns:
        for res in turn.tool_results:
            if not res.name and res.call_id in names:
                res.name = names[res.call_id]


def read(path: str | Path) -> Session:
    """Parse one Claude Code transcript into the vendor-neutral IR."""
    path = Path(path)
    session = Session(source=SOURCE)

    raw_bytes = path.stat().st_size if path.exists() else 0
    record_count = 0
    skipped_types: dict[str, int] = {}
    index = 0

    for record in iter_records(path):
        record_count += 1

        # Metadata is sparse: `mode` and `queue-operation` records carry only a
        # sessionId, so each field has to be filled independently as it appears
        # rather than snapshotted off the first record.
        for attr, key in (
            ("session_id", "sessionId"),
            ("cwd", "cwd"),
            ("git_branch", "gitBranch"),
            ("agent_version", "version"),
        ):
            if not getattr(session, attr):
                value = record.get(key)
                if value:
                    setattr(session, attr, value)

        ts = record.get("timestamp")
        if ts:
            if not session.started:
                session.started = ts
            session.ended = ts

        rtype = record.get("type", "")
        if rtype not in MESSAGE_TYPES:
            skipped_types[rtype] = skipped_types.get(rtype, 0) + 1
            continue

        turn = _parse_turn(record, index)
        if turn is None or turn.is_empty:
            continue
        session.turns.append(turn)
        index += 1

    _link_result_names(session)

    semantic = session.semantic_bytes()
    session.stats = {
        "file_bytes": raw_bytes,
        "records": record_count,
        "turns": len(session.turns),
        "user_turns": len(session.user_turns()),
        "semantic_bytes": semantic,
        "compression": round(raw_bytes / semantic, 1) if semantic else None,
        "dropped_media": sum(t.dropped_media for t in session.turns),
        "non_message_records": skipped_types,
        "tools": session.tool_histogram(),
    }
    return session
