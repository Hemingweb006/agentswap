"""Reader for OpenAI Codex CLI rollout files.

Sessions live at ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl.
Cold sessions get zstd-compressed to .jsonl.zst and are only materialized back
to plain JSONL when resumed, so both extensions must be handled.

Each line is a `RolloutLine` wrapping a tagged `RolloutItem`:

  response_item              model output and tool calls (OpenAI Responses shape)
  event_msg                  protocol events: UserMessage, TokenCount, ThreadGoalUpdated
  session_meta               id, source, cwd, model_provider, cli_version, git_*
  turn_context               model, approval_policy, sandbox_policy
  compacted                  items produced by history compaction
  inter_agent_communication  parent/child agent messages
  world_state                environment snapshot

Two structural differences from Claude Code that the IR has to absorb:

1. Roles are not the top-level distinction. Codex tags by *item kind* and the
   role lives inside a `message` item, so a role-based parser reads nothing.
2. Tool calls are `function_call` / `function_call_output` pairs joined by
   `call_id`, and `arguments` is a JSON *string* rather than an object.

WARNING -- this adapter was written from the published rollout schema, not from
a transcript on disk. That is exactly the mistake this project exists to avoid,
so the reader counts every shape it fails to recognize and surfaces the tally in
`stats["unrecognized"]`. Run `inspect --probe` against a real session and the
unknown shapes will name themselves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from ..ir import Session, Turn, ToolCall, ToolResult, compact_patch, digest

SOURCE = "codex"

ARG_KEEP_LIMIT = 200

# Rust serde tags may serialize snake_case or camelCase depending on config;
# accept both rather than betting on one.
_ITEM_ALIASES = {
    "response_item": "response_item",
    "responseitem": "response_item",
    "event_msg": "event_msg",
    "eventmsg": "event_msg",
    "session_meta": "session_meta",
    "sessionmeta": "session_meta",
    "turn_context": "turn_context",
    "turncontext": "turn_context",
    "compacted": "compacted",
    "inter_agent_communication": "inter_agent",
    "interagentcommunication": "inter_agent",
    "world_state": "world_state",
    "worldstate": "world_state",
}

_TEXT_FIELDS = ("text", "input_text", "output_text", "content", "message")


def default_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def _open_lines(path: Path) -> Iterator[str]:
    """Yield raw lines, transparently decompressing zstd rollouts."""
    if path.suffix == ".zst":
        try:
            import zstandard
        except ImportError as exc:
            raise RuntimeError(
                f"{path.name} is zstd-compressed; run `pip install zstandard`"
            ) from exc
        dctx = zstandard.ZstdDecompressor()
        with path.open("rb") as raw, dctx.stream_reader(raw) as stream:
            buffer = b""
            while True:
                chunk = stream.read(1 << 16)
                if not chunk:
                    break
                buffer += chunk
                *complete, buffer = buffer.split(b"\n")
                for line in complete:
                    yield line.decode("utf-8", errors="replace")
            if buffer:
                yield buffer.decode("utf-8", errors="replace")
    else:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            yield from fh


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    for line in _open_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _unwrap(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return (normalized_item_kind, payload) from a RolloutLine."""
    kind = str(record.get("type") or record.get("item_type") or "").lower()
    kind = _ITEM_ALIASES.get(kind.replace("-", "_"), kind)

    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = record.get("item") if isinstance(record.get("item"), dict) else record
    return kind, payload


def _extract_text(content: Any) -> str:
    """Pull display text out of a Responses-API content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in _TEXT_FIELDS:
            if isinstance(content.get(key), str):
                return content[key]
        return ""
    if isinstance(content, list):
        parts = []
        for block in content:
            text = _extract_text(block)
            if text:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """`arguments` arrives as a JSON string on function_call items."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": digest(raw, ARG_KEEP_LIMIT)}
        raw = parsed
    if not isinstance(raw, dict):
        return {"_raw": digest(raw, ARG_KEEP_LIMIT)}
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


def _new_turn(index: int, role: str, timestamp: str) -> Turn:
    return Turn(index=index, role=role, timestamp=timestamp)


def _fingerprint(text: str) -> str:
    """Normalized key for spotting the same message recorded twice."""
    return " ".join(text.split())[:400]


def read(path: str | Path) -> Session:
    """Parse one Codex rollout file into the vendor-neutral IR."""
    path = Path(path)
    session = Session(source=SOURCE)

    raw_bytes = path.stat().st_size if path.exists() else 0
    record_count = 0
    index = 0
    unrecognized: dict[str, int] = {}
    kinds: dict[str, int] = {}
    goals: list[str] = []
    compactions = 0
    duplicates = 0

    # Codex records a user message twice: once as an `event_msg` protocol event
    # and again as a `response_item` message that actually went to the model.
    # Observed on a real 1.5MB rollout, where every single prompt appeared
    # twice in the reconstructed thread. Keep the first, drop the echo.
    last_user_fingerprint = ""

    for record in iter_records(path):
        record_count += 1
        timestamp = record.get("timestamp", "") or ""
        if timestamp:
            if not session.started:
                session.started = timestamp
            session.ended = timestamp

        kind, payload = _unwrap(record)
        kinds[kind or "?"] = kinds.get(kind or "?", 0) + 1

        if kind == "session_meta":
            session.session_id = payload.get("id") or session.session_id
            session.cwd = payload.get("cwd") or session.cwd
            session.agent_version = payload.get("cli_version") or session.agent_version
            session.git_branch = payload.get("git_branch") or session.git_branch
            continue

        if kind == "turn_context":
            session.stats.setdefault("model", payload.get("model", ""))
            continue

        if kind == "compacted":
            compactions += 1
            continue

        if kind == "event_msg":
            etype = str(payload.get("type") or payload.get("event") or "").lower()
            if "usermessage" in etype.replace("_", ""):
                text = _extract_text(payload.get("message") or payload.get("content"))
                text = text.strip()
                if text:
                    fp = _fingerprint(text)
                    if fp == last_user_fingerprint:
                        duplicates += 1
                    else:
                        last_user_fingerprint = fp
                        turn = _new_turn(index, "user", timestamp)
                        turn.text = text
                        session.turns.append(turn)
                        index += 1
            elif "threadgoal" in etype.replace("_", ""):
                goal = _extract_text(payload.get("goal") or payload.get("message"))
                if goal:
                    goals.append(goal)
            continue

        if kind != "response_item":
            if kind not in ("inter_agent", "world_state"):
                unrecognized[kind or "<no type>"] = unrecognized.get(kind or "<no type>", 0) + 1
            continue

        itype = str(payload.get("type") or "").lower()

        if itype == "message":
            text = _extract_text(payload.get("content")).strip()
            role = payload.get("role", "assistant")
            if text:
                if role == "user":
                    fp = _fingerprint(text)
                    if fp == last_user_fingerprint:
                        duplicates += 1
                        continue
                    last_user_fingerprint = fp
                turn = _new_turn(index, role, timestamp)
                turn.text = text
                session.turns.append(turn)
                index += 1

        elif itype.endswith("_call"):
            # Every hosted tool in the Responses API lands here: function_call,
            # local_shell_call, custom_tool_call, web_search_call,
            # file_search_call, image_generation_call, mcp_call, computer_call.
            # A real session surfaced 35 web_search_call items that an
            # enumerated list had silently skipped, so match on the suffix and
            # stop guessing which tools exist.
            turn = _new_turn(index, "assistant", timestamp)
            turn.tool_calls.append(
                ToolCall(
                    name=payload.get("name") or itype[: -len("_call")] or itype,
                    args=_parse_arguments(
                        payload.get("arguments")
                        or payload.get("input")
                        or payload.get("action")
                        or payload.get("query")
                    ),
                    call_id=payload.get("call_id") or payload.get("id") or "",
                )
            )
            session.turns.append(turn)
            index += 1

        elif itype.endswith("_call_output") or itype.endswith("_call_result"):
            body = payload.get("output")
            text = _extract_text(body)
            if not text and body is not None:
                text = json.dumps(body, ensure_ascii=False)
            turn = _new_turn(index, "tool", timestamp)
            turn.tool_results.append(
                ToolResult(
                    call_id=payload.get("call_id") or "",
                    preview=digest(text),
                    raw_bytes=len(text),
                )
            )
            session.turns.append(turn)
            index += 1

        elif itype == "reasoning":
            # Summaries only; the underlying reasoning is not persisted in full.
            continue

        else:
            unrecognized[f"response_item/{itype or '?'}"] = (
                unrecognized.get(f"response_item/{itype or '?'}", 0) + 1
            )

    _link_result_names(session)

    semantic = session.semantic_bytes()
    session.stats.update(
        {
            "file_bytes": raw_bytes,
            "records": record_count,
            "turns": len(session.turns),
            "user_turns": len(session.user_turns()),
            "semantic_bytes": semantic,
            "compression": round(raw_bytes / semantic, 1) if semantic else None,
            "dropped_media": 0,
            "compactions": compactions,
            "duplicate_user_turns_dropped": duplicates,
            "goals": goals,
            "item_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
            "unrecognized": dict(sorted(unrecognized.items(), key=lambda kv: -kv[1])),
            "tools": session.tool_histogram(),
        }
    )
    return session


def _link_result_names(session: Session) -> None:
    names: dict[str, str] = {}
    for turn in session.turns:
        for call in turn.tool_calls:
            if call.call_id:
                names[call.call_id] = call.name
    for turn in session.turns:
        for res in turn.tool_results:
            if not res.name and res.call_id in names:
                res.name = names[res.call_id]
