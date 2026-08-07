"""The front door: start an agent, and when it stops, offer to keep going elsewhere.

The loop is deliberately boring:

    pick an agent -> hand over the terminal -> it exits -> find what it wrote
    -> was that a quota wall? -> port the session -> launch the other one

Everything interesting already happened in readers, handoff and writers. This
module only decides *when* to call them, and it is the difference between a
library and something a person actually runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import agents, handoff, readers, ui, writers
from .agents import AgentSpec

DIM = ui.DIM
BOLD = ui.BOLD
RESET = ui.RESET


def _say(text: str = "") -> None:
    print(text, flush=True)


def _rule(label: str = "", colour: str = "") -> None:
    _say(ui.rule(label, colour))


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:,.0f}{unit}" if unit == "B" else f"{n:,.1f}{unit}"
        n /= 1024
    return f"{n:,.1f}TB"


def _ask(prompt: str, choices: dict[str, str], default: str, title: str = "") -> str:
    """Single-key menu. Returns the chosen key."""
    rows = []
    for key, label in choices.items():
        if key == default:
            rows.append(f"{ui.ACCENT}▸{RESET} {ui.BOLD}[{key}]{RESET} {label}")
        else:
            rows.append(f"  {ui.GREY}[{key}]{RESET} {label}")
    _say()
    _say(ui.box(rows, title=title, colour=ui.GREY))
    while True:
        try:
            raw = input(f"  {ui.ACCENT}❯{RESET} {prompt} {ui.GREY}[{default}]{RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _say()
            return "q"
        if not raw:
            return default
        if raw in choices:
            return raw
        if len(raw) > 1:
            matches = [k for k in choices if k.startswith(raw[0])]
            if len(matches) == 1:
                return matches[0]
        _say(f"  {DIM}pick one of: {', '.join(choices)}{RESET}")


def _pick_agent(available: list[AgentSpec], prompt: str, title: str = "") -> AgentSpec | None:
    if not available:
        return None
    if len(available) == 1:
        return available[0]
    choices = {
        str(i + 1): f"{spec.label:<14}{ui.GREY}{spec.binary}{RESET}"
        for i, spec in enumerate(available)
    }
    choices["q"] = "quit"
    answer = _ask(prompt, choices, "1", title=title)
    if answer == "q":
        return None
    return available[int(answer) - 1]


@dataclass
class Carried:
    """The result of moving a session into another tool."""

    launch_args: list[str]
    before: int
    after: int
    session_id: str | None = None

    @property
    def ratio(self) -> float:
        return (self.before / self.after) if self.after else 0.0


def _write_detail(brief, cwd: str) -> Path | None:
    """Park the unabridged brief in the project so nothing is lost to compaction."""
    try:
        out_dir = Path(cwd) / ".agentswap"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "handoff.md"
        out_file.write_text(brief.render(compact=False), encoding="utf-8")
        return out_file
    except OSError:
        return None


def _carry(source_path: Path, target: AgentSpec, cwd: str, auto: bool = False) -> Carried:
    """Move a session into the target tool by whichever route it supports."""
    session = readers.read(source_path)
    brief = handoff.build(session)
    brief.cwd = cwd or brief.cwd

    detail = _write_detail(brief, brief.cwd or cwd)
    text = brief.render(compact=True, detail_path=str(detail) if detail else "")
    text += handoff.CONTINUE_SUFFIX if auto else handoff.CONFIRM_SUFFIX

    before = session.stats.get("file_bytes", 0)

    if target.delivery == "session":
        writer = writers.WRITERS[target.target]
        brief_text = text
        _, session_id, records = writer.write(brief, body=brief_text)
        after = sum(len(str(r)) for r in records)
        return Carried(target.resume(session_id), before, after, session_id)

    # Prompt delivery: no file to forge, the brief is the opening message.
    # Passed as an argv element, so there is no shell quoting to get wrong.
    return Carried(target.prompt_args(text), before, len(text))


def run(cwd: str, start: str | None = None, auto: bool = False) -> int:
    """Interactive loop. Returns a process exit code."""
    available = [s for s in agents.installed() if s.source]
    if not available:
        _say("No supported agent CLIs found on PATH.")
        _say(f"{DIM}Looked for: {', '.join(s.binary for s in agents.SPECS.values())}{RESET}")
        return 1

    from . import __version__

    _say(ui.banner("session portability for coding agents", f"v{__version__}"))
    _say(ui.kv("project", f"{ui.BOLD}{cwd}{RESET}"))
    ready = "  ".join(
        f"{ui.OK}●{RESET} {s.label}" if s.can_receive else f"{ui.GREY}○ {s.label}{RESET}"
        for s in agents.SPECS.values()
        if s.installed
    )
    missing = [s.label for s in agents.SPECS.values() if not s.installed]
    _say(ui.kv("agents", ready or f"{ui.GREY}none{RESET}"))
    if missing:
        _say(ui.kv("", f"{ui.GREY}not installed: {', '.join(missing)}{RESET}"))
    _say()

    if start:
        spec = agents.SPECS.get(start)
        if spec is None or not spec.installed:
            _say(f"{ui.BAD}{start} is not available.{RESET}")
            return 1
        current: AgentSpec | None = spec
    else:
        current = _pick_agent(available, "start with?", title="START")

    pending: Carried | None = None

    while current is not None:
        extra = pending.launch_args if pending else []
        launched_at = time.time()

        suffix = f" {ui.GREY}· with carried context{RESET}" if pending else ""
        _rule(f"{ui.ACCENT}{current.label.upper()}{RESET}{suffix}", ui.GREY)
        _say()

        code = agents.launch(current, cwd, extra)
        pending = None

        _say()
        written = agents.find_session_after(current, cwd, launched_at)

        quota_hit = None
        if written is not None:
            try:
                quota_hit = agents.looks_like_quota_exhaustion(readers.read(written))
            except (ValueError, RuntimeError, OSError):
                quota_hit = None

        if quota_hit:
            _say(ui.box(
                [
                    f"{ui.BOLD}{current.label} hit a usage limit{RESET}",
                    f"{ui.GREY}matched \"{quota_hit}\" in the session it just wrote{RESET}",
                ],
                title=f"{ui.ALERT}WALL{RESET}",
                colour=ui.ALERT,
            ))
        elif code not in (0, 130):
            _say(ui.kv("exited", f"{ui.BAD}{current.label}, code {code}{RESET}"))
        else:
            _say(ui.kv("exited", f"{current.label}"))

        if written is None:
            _say(f"  {DIM}No new session found to carry over.{RESET}")
            return code

        others = [s for s in available if s.key != current.key and s.can_receive]
        if not others:
            _say(f"  {DIM}No other agent installed to switch to.{RESET}")
            return code

        # Switch is always the default. You typed `agentswap run` -- carrying
        # the session on is the thing you came here for, so Enter should do it
        # whether the agent hit a wall or you exited cleanly. Quitting is one
        # keystroke away and costs nothing.
        choice = _ask(
            "continue elsewhere?",
            {"s": f"switch  {ui.GREY}carry this session over{RESET}", "q": "quit"},
            "s",
            title="SWITCH",
        )
        if choice != "s":
            return code

        target = _pick_agent(others, "switch to?", title="TARGET")
        if target is None:
            return code

        try:
            carried = _carry(written, target, cwd, auto=auto)
        except (ValueError, RuntimeError, OSError, FileExistsError) as exc:
            _say(f"Could not carry that session: {exc}")
            return 1

        rows = [
            ui.arrow(
                _human(carried.before),
                _human(carried.after),
                f"{carried.ratio:,.0f}x smaller" if carried.ratio >= 2 else "",
            )
        ]
        if carried.session_id:
            rows.append(f"{ui.GREY}into {target.label} session {carried.session_id}{RESET}")
        else:
            rows.append(f"{ui.GREY}into a new {target.label} session, as its opening message{RESET}")
        if not auto:
            rows.append(f"{ui.GREY}it will confirm what it understands, then wait for you{RESET}")

        _say()
        _say(ui.box(rows, title=f"{ui.OK}CARRIED{RESET}", colour=ui.OK))

        current = target
        pending = carried

    return 0
