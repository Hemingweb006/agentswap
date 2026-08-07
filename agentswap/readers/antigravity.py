"""Reader for Google Antigravity CLI conversations.

Antigravity stores a conversation two ways. The authoritative store is protobuf
BLOBs inside SQLite:

    ~/.gemini/antigravity-cli/conversations/*.db
    ~/.gemini/antigravity/conversations/*.db

and alongside it a plain JSONL transcript of the same interactions:

    <appdata>/brain/<conversation-id>/.system_generated/logs/*.jsonl

We read the JSONL. Decoding protobuf out of someone's live database to save a
few fields is a bad trade, and the log carries what a handoff needs.

UNVERIFIED -- unlike the other two adapters, this one has never seen a real
file. Antigravity was not installed on any machine this was developed against,
so the record shape below is inferred from the documented structure and from
what every agent log of this kind has in common. It is written to be loud
rather than lossy: anything it does not recognize is counted and surfaced by
`inspect --probe`. If you have Antigravity, run that and the gaps will name
themselves.

Antigravity is a source only. It is not a switch target, because writing a
resumable conversation means generating protobuf into SQLite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from ..ir import Session, Turn, ToolCall, ToolResult, compact_patch, digest

SOURCE = "antigravity"

ARG_KEEP_LIMIT = 200

# Several install layouts are documented; probe all of them and use whichever
# exists rather than betting on one.
CANDIDATE_ROOTS = (
    Path.home() / ".gemini" / "antigravity-cli",
    Path.home() / ".gemini" / "antigravity",
    Path.home() / ".antigravity",
    Path.home() / "Library" / "Application Support" / "Antigravity",
)

LOG_GLOB = "brain/*/.system_generated/logs/*.jsonl"

_USER_ROLES = {"user", "human", "USER"}
_ASSISTANT_ROLES = {"assistant", "model", "agent", "ASSISTANT", "MODEL"}

_TEXT_KEYS = ("text", "content", "message", "body", "value", "parts")
_ROLE_KEYS = ("role", "author", "speaker", "type")
_TOOL_CALL_KEYS = ("tool_call", "toolCall", "function_call", "functionCall", "action")
_TOOL_RESULT_KEYS = ("tool_result", "toolResult", "function_response", "functionResponse", "observation")


def default_root() -> Path:
    for root in CANDIDATE_ROOTS:
        if root.exists():
            return root
    return CANDIDATE_ROOTS[0]


def conversation_dbs() -> list[Path]:
    """The SQLite stores, listed but not parsed. Useful for diagnostics."""
    found: list[Path] = []
    for root in CANDIDATE_ROOTS:
        conv = root / "conversations"
        if conv.exists():
            found.extend(sorted(conv.glob("*.db")))
    return found


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
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


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, "", [], {}):
            return record[key]
    return None


def _extract_text(value: Any) -> str:
    """Pull display text out of whatever nesting the log uses."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            if key in value:
                text = _extract_text(value[key])
                if text:
                    return text
        return ""
    if isinstance(value, list):
        return "\n".join(t for t in (_extract_text(v) for v in value) if t)
    return ""


def _clean_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": digest(raw, ARG_KEEP_LIMIT)}
    if not isinstance(raw, dict):
        return {"_raw": digest(raw, ARG_KEEP_LIMIT)} if raw else {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str):
            patch = compact_patch(value)
            if patch is not None:
                out[key] = patch
            else:
                out[key] = value if len(value) <= ARG_KEEP_LIMIT else digest(value, ARG_KEEP_LIMIT)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = digest(value, ARG_KEEP_LIMIT)
    return out


def _normalize_role(value: Any) -> str:
    role = str(value or "").strip()
    if role in _USER_ROLES:
        return "user"
    if role in _ASSISTANT_ROLES:
        return "assistant"
    return role.lower()


def read(path: str | Path) -> Session:
    """Parse one Antigravity conversation log into the vendor-neutral IR."""
    path = Path(path)
    session = Session(source=SOURCE)

    raw_bytes = path.stat().st_size if path.exists() else 0
    record_count = 0
    index = 0
    unrecognized: dict[str, int] = {}
    kinds: dict[str, int] = {}

    # brain/<conversation-id>/.system_generated/logs/<file>.jsonl
    parts = path.parts
    if "brain" in parts:
        pos = parts.index("brain")
        if pos + 1 < len(parts):
            session.session_id = parts[pos + 1]

    for record in iter_records(path):
        record_count += 1

        timestamp = str(
            _first(record, ("timestamp", "time", "created_at", "createdAt")) or ""
        )
        if timestamp:
            if not session.started:
                session.started = timestamp
            session.ended = timestamp

        for attr, keys in (
            ("cwd", ("cwd", "workspace", "working_directory", "workingDirectory")),
            ("session_id", ("conversation_id", "conversationId", "session_id", "id")),
            ("agent_version", ("version", "cli_version", "cliVersion")),
            ("git_branch", ("git_branch", "gitBranch", "branch")),
        ):
            if not getattr(session, attr):
                value = _first(record, keys)
                if isinstance(value, str) and value:
                    setattr(session, attr, value)

        kind = str(_first(record, ("kind", "event", "type")) or "?")
        kinds[kind] = kinds.get(kind, 0) + 1

        role = _normalize_role(_first(record, _ROLE_KEYS))
        text = _extract_text(_first(record, _TEXT_KEYS))
        call = _first(record, _TOOL_CALL_KEYS)
        result = _first(record, _TOOL_RESULT_KEYS)

        if call is not None:
            turn = Turn(index=index, role="assistant", timestamp=timestamp)
            name = ""
            args: Any = call
            if isinstance(call, dict):
                name = str(_first(call, ("name", "tool", "tool_name", "toolName")) or "")
                args = _first(call, ("args", "arguments", "input", "parameters")) or {}
            turn.tool_calls.append(ToolCall(name=name or "tool", args=_clean_args(args)))
            session.turns.append(turn)
            index += 1
            continue

        if result is not None:
            body = result
            if isinstance(result, dict):
                body = _first(result, ("output", "result", "content", "response")) or result
            preview = _extract_text(body) or json.dumps(body, ensure_ascii=False)[:2000]
            turn = Turn(index=index, role="tool", timestamp=timestamp)
            turn.tool_results.append(
                ToolResult(preview=digest(preview), raw_bytes=len(preview))
            )
            session.turns.append(turn)
            index += 1
            continue

        if text.strip() and role in ("user", "assistant"):
            turn = Turn(index=index, role=role, timestamp=timestamp)
            turn.text = text.strip()
            session.turns.append(turn)
            index += 1
            continue

        # Only flag records that carried content we could not place. Metadata,
        # telemetry and lifecycle records legitimately have nothing to extract,
        # and reporting them as failures trains people to ignore the probe --
        # which defeats the point of having one. `item_kinds` still counts
        # every record, so nothing is hidden.
        if text.strip():
            label = f"{kind}/role={role or 'none'}"
            unrecognized[label] = unrecognized.get(label, 0) + 1

    semantic = session.semantic_bytes()
    session.stats = {
        "file_bytes": raw_bytes,
        "records": record_count,
        "turns": len(session.turns),
        "user_turns": len(session.user_turns()),
        "semantic_bytes": semantic,
        "compression": round(raw_bytes / semantic, 1) if semantic else None,
        "dropped_media": 0,
        "item_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "unrecognized": dict(sorted(unrecognized.items(), key=lambda kv: -kv[1])),
        "tools": session.tool_histogram(),
        "adapter_status": "inferred -- never validated against a real file",
    }
    return session
