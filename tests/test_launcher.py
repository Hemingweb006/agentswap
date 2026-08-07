"""Tests for the process side: agent detection, session discovery, quota reading."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentswap import agents, handoff, readers  # noqa: E402
from agentswap.handoff import _is_interesting  # noqa: E402
from agentswap.ir import Session, Turn, ToolResult  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("You've reached your usage limit. Resets at 18:00.", "usage limit"),
        ("Error 429: Too Many Requests", "429"),
        ("insufficient_quota: your credit balance is too low", "quota"),
        ("rate_limit_exceeded", "rate_limit"),
        ("all good, tests passing", None),
        ("", None),
    ],
)
def test_quota_markers_are_read_from_the_session_not_the_terminal(text, expected):
    session = Session(source="codex", turns=[Turn(index=0, role="assistant", text=text)])
    assert agents.looks_like_quota_exhaustion(session) == expected


def test_quota_marker_is_found_inside_a_tool_result():
    turn = Turn(index=0, role="tool")
    turn.tool_results.append(ToolResult(preview="HTTP 429 too many requests"))
    session = Session(source="codex", turns=[turn])
    assert agents.looks_like_quota_exhaustion(session) is not None


def test_a_healthy_real_session_is_not_flagged():
    session = readers.read(FIXTURES / "rollout-real-shape.jsonl")
    assert agents.looks_like_quota_exhaustion(session) is None


def test_only_the_tail_of_a_session_is_examined():
    turns = [Turn(index=0, role="assistant", text="we hit a rate limit earlier but recovered")]
    turns += [Turn(index=i, role="assistant", text="fine") for i in range(1, 40)]
    session = Session(source="codex", turns=turns)
    assert agents.looks_like_quota_exhaustion(session, tail=5) is None


def _spec_rooted_at(key: str, root: Path) -> agents.AgentSpec:
    """A copy of a spec pointed at a temp dir.

    AgentSpec is frozen on purpose, so tests build a replacement rather than
    mutating the shared global -- otherwise one test silently reroutes every
    test that runs after it.
    """
    return replace(agents.SPECS[key], session_root=lambda: root)


def test_finds_the_session_written_after_launch(tmp_path):
    spec = _spec_rooted_at("codex", tmp_path)
    day = tmp_path / "2026" / "08" / "07"
    day.mkdir(parents=True)

    stale = day / "rollout-old.jsonl"
    stale.write_text(
        json.dumps({"timestamp": "t", "type": "session_meta",
                    "payload": {"id": "old", "cwd": "/tmp/proj"}}) + "\n",
        encoding="utf-8",
    )
    os.utime(stale, (time.time() - 9999, time.time() - 9999))

    launched_at = time.time()
    fresh = day / "rollout-new.jsonl"
    fresh.write_text(
        json.dumps({"timestamp": "t", "type": "session_meta",
                    "payload": {"id": "new", "cwd": "/tmp/proj"}}) + "\n",
        encoding="utf-8",
    )

    found = agents.find_session_after(spec, "/tmp/proj", launched_at)
    assert found is not None and found.name == "rollout-new.jsonl"


def test_no_session_found_returns_none(tmp_path):
    spec = _spec_rooted_at("codex", tmp_path / "nothing")
    assert agents.find_session_after(spec, "/tmp/proj", time.time()) is None


def test_project_files_survive_even_when_the_project_is_in_a_scratch_dir():
    # /tmp is filtered as scratch, but not when it is where the project lives.
    assert _is_interesting("/tmp/proj/payments/retry.py", cwd="/tmp/proj") is True
    assert _is_interesting("/tmp/unrelated/thing.py", cwd="/home/me/proj") is False
    assert _is_interesting("/var/folders/xx/cache.json", cwd="/home/me/proj") is False
    assert _is_interesting("/home/me/proj/src/main.py", cwd="/home/me/proj") is True


def test_every_spec_that_can_receive_has_a_route():
    for spec in agents.SPECS.values():
        if spec.delivery == "session":
            assert spec.target and spec.resume, f"{spec.key} cannot be written to"
        else:
            assert spec.prompt_args, f"{spec.key} has no prompt route"


# --------------------------------------------------------------------------
# Delivery modes. Verified against real CLIs:
#   claude --resume <id>  finds a written session file      -> "session"
#   codex resume <id>     "No saved session found with ID"  -> "prompt"
# --------------------------------------------------------------------------

def test_claude_code_receives_a_written_session_file():
    spec = agents.SPECS["claude-code"]
    assert spec.delivery == "session"
    assert spec.target == "claude-code"
    assert spec.resume("abc") == ["--resume", "abc"]


def test_codex_receives_a_prompt_because_resume_needs_its_sqlite_index():
    spec = agents.SPECS["codex"]
    assert spec.delivery == "prompt"
    assert spec.target is None, "writing a rollout file for codex is a dead end"
    assert spec.prompt_args("the brief") == ["the brief"]


def test_prompt_delivery_passes_the_brief_as_one_argv_element():
    # Not a shell string -- a brief containing quotes or newlines must not need
    # escaping, and must never be word-split.
    brief = 'line one\nline "two"\n$(echo pwned)'
    assert agents.SPECS["codex"].prompt_args(brief) == [brief]


def test_prompt_delivery_carries_the_brief_in_argv_not_a_file():
    from agentswap import session_loop

    source = FIXTURES / "rollout-real-shape.jsonl"
    carried = session_loop._carry(source, agents.SPECS["codex"], "/tmp/proj")
    assert carried.session_id is None
    assert len(carried.launch_args) == 1
    assert "# Session handoff" in carried.launch_args[0]
    assert carried.before > carried.after


# --------------------------------------------------------------------------
# Detection must distinguish "you don't have it" from "your shell can't see it"
# --------------------------------------------------------------------------

def test_binary_found_on_path_is_reported_as_on_path(tmp_path, monkeypatch):
    binary = tmp_path / "faketool"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    spec = replace(agents.SPECS["antigravity"], binary="faketool")
    path, on_path = spec.locate()
    assert path == binary and on_path is True
    assert spec.present_but_hidden is False


def test_binary_in_a_known_dir_but_off_path_is_found_and_flagged(tmp_path, monkeypatch):
    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    binary = bindir / "faketool"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")

    spec = replace(
        agents.SPECS["antigravity"], binary="faketool", extra_bin_dirs=(str(bindir),)
    )
    path, on_path = spec.locate()
    assert path == binary, "installer dropped it somewhere PATH does not cover"
    assert on_path is False
    assert spec.present_but_hidden is True


def test_a_genuinely_absent_binary_reports_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    spec = replace(
        agents.SPECS["antigravity"], binary="definitely-not-here", extra_bin_dirs=()
    )
    assert spec.locate() == (None, False)
    assert spec.present_but_hidden is False


def test_a_non_executable_file_does_not_count_as_installed(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "faketool").write_text("not executable", encoding="utf-8")
    monkeypatch.setenv("PATH", "/nonexistent")

    spec = replace(
        agents.SPECS["antigravity"], binary="faketool", extra_bin_dirs=(str(bindir),)
    )
    assert spec.locate() == (None, False)


def test_antigravity_carries_a_real_install_command():
    hint = agents.SPECS["antigravity"].install_hint
    assert "antigravity.google/cli/install.sh" in hint


def test_every_agent_knows_where_installers_hide_things():
    for spec in agents.SPECS.values():
        assert "~/.local/bin" in spec.extra_bin_dirs, spec.key


@pytest.mark.parametrize("exit_code", [0, 1])
def test_switch_is_the_default_however_the_agent_exited(tmp_path, monkeypatch, exit_code):
    """Enter should carry the session on, not quit.

    Reaching this prompt means the user launched through `agentswap run`, so
    continuing elsewhere is the intent -- whether the agent hit a wall or was
    exited cleanly. Quit stays one keystroke away.
    """
    from agentswap import session_loop

    # can_receive consults the real binary, so give it real ones to find.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex"):
        stub = bindir / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))

    seen = {}

    def fake_ask(prompt, choices, default, title=""):
        seen["default"] = default
        seen["choices"] = list(choices)
        return "q"

    monkeypatch.setattr(session_loop, "_ask", fake_ask)
    monkeypatch.setattr(session_loop.agents, "launch", lambda *a, **k: exit_code)
    monkeypatch.setattr(session_loop.agents, "find_session_after",
                        lambda *a, **k: FIXTURES / "rollout-real-shape.jsonl")

    session_loop.run(str(tmp_path), start="claude-code")
    assert seen["default"] == "s", "Enter must switch, not quit"
    assert seen["choices"] == ["s", "q"]


# --------------------------------------------------------------------------
# port must offer every agent that can receive, not just those with a writer
# --------------------------------------------------------------------------

def test_every_agent_is_a_valid_port_target():
    """Regression: --to rejected antigravity.

    The CLI derived its target list from the writers dict, but writers only
    cover session delivery. Prompt-delivery targets were unreachable from
    `port` even though `run` carried to them fine.
    """
    targets = agents.targets()
    for key in agents.SPECS:
        assert key in targets, f"{key} cannot be reached by `agentswap port`"


def test_targets_are_capability_not_availability(monkeypatch):
    # You should be able to prepare a handoff for a tool you have not
    # installed yet, so PATH must not filter the list.
    monkeypatch.setenv("PATH", "/nonexistent")
    assert "antigravity" in agents.targets()
    assert "claude-code" in agents.targets()


def test_port_and_run_produce_the_same_brief(tmp_path):
    """Both paths must send compact-plus-confirm, not two different briefs."""
    from agentswap import cli, session_loop

    source = FIXTURES / "rollout-real-shape.jsonl"
    carried = session_loop._carry(source, agents.SPECS["antigravity"], str(tmp_path))
    via_run = carried.launch_args[0]

    rc = cli.main(["port", str(source), "--to", "antigravity", "--cwd", str(tmp_path)])
    assert rc == 0
    written = next((tmp_path / ".agentswap").glob("handoff-*.md")).read_text()

    assert "Do not start work yet" in via_run
    assert "Do not start work yet" in written
    assert "# Session handoff" in written
