"""Tests for the Antigravity adapter and the terminal UI.

The Antigravity reader has never seen a real file -- it is inferred from the
documented layout. These tests pin the inference so that when a real log does
turn up and contradicts it, the diff is obvious.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentswap import handoff, readers, ui  # noqa: E402
from agentswap.readers import antigravity  # noqa: E402

AG_LOG = (
    Path(__file__).parent
    / "fixtures" / "ag" / "brain" / "conv-7f2a" / ".system_generated" / "logs" / "session.jsonl"
)


def test_detected_by_location_not_contents():
    # Antigravity logs are plain JSONL with no vendor marker inside, so the
    # brain/<id>/.system_generated/ path is the only reliable signal.
    assert readers.detect(AG_LOG) == "antigravity"


def test_conversation_id_comes_from_the_directory_name():
    assert readers.read(AG_LOG).session_id == "conv-7f2a"


def test_metadata_is_gathered_from_lifecycle_records():
    session = readers.read(AG_LOG)
    assert session.cwd == "/Users/reda/dev/api"
    assert session.agent_version == "1.4.0"
    assert session.git_branch == "main"


def test_both_message_shapes_are_understood():
    # `content: "..."` and `parts: [{text: "..."}]` both appear in the wild.
    session = readers.read(AG_LOG)
    texts = [t.text for t in session.turns if t.text]
    assert "migrate the auth middleware off express-session" in texts
    assert "Looking at the middleware first." in texts


def test_model_role_maps_to_assistant():
    session = readers.read(AG_LOG)
    assert any(t.role == "assistant" for t in session.turns)
    assert not any(t.role == "model" for t in session.turns)


def test_tool_calls_and_results_are_paired_off():
    session = readers.read(AG_LOG)
    tools = session.tool_histogram()
    assert tools == {"read_file": 1, "edit_file": 1}
    assert any(t.tool_results for t in session.turns)


def test_metadata_records_are_not_reported_as_failures():
    # session_start and telemetry carry no content. Flagging them would train
    # people to ignore the probe, which is the opposite of its purpose.
    session = readers.read(AG_LOG)
    assert session.stats["unrecognized"] == {}
    assert "session_start" in session.stats["item_kinds"]
    assert "telemetry" in session.stats["item_kinds"]


def test_content_with_an_unknown_role_is_reported(tmp_path):
    log = tmp_path / "brain" / "x" / ".system_generated" / "logs" / "s.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        '{"kind":"message","role":"mystery_participant","content":"who am i"}\n',
        encoding="utf-8",
    )
    session = readers.read(log)
    assert session.stats["unrecognized"] == {"message/role=mystery_participant": 1}


def test_it_produces_a_usable_handoff():
    brief = handoff.build(readers.read(AG_LOG))
    assert brief.goal == "migrate the auth middleware off express-session"
    assert brief.next_step == "now update the tests too"
    assert any("auth.ts" in p for p in brief.files_touched)


def test_adapter_declares_that_it_is_unvalidated():
    assert "never validated" in readers.read(AG_LOG).stats["adapter_status"]


def test_antigravity_is_a_source_but_not_a_session_target():
    from agentswap import agents

    spec = agents.SPECS["antigravity"]
    assert spec.source == "antigravity"
    assert spec.delivery == "prompt", "protobuf in SQLite is out of reach"
    assert spec.target is None


def test_several_install_layouts_are_probed():
    assert len(antigravity.CANDIDATE_ROOTS) >= 3
    assert any(".gemini" in str(p) for p in antigravity.CANDIDATE_ROOTS)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def test_plain_len_ignores_escape_sequences():
    assert ui.plain_len("\033[31mred\033[0m") == 3
    assert ui.plain_len("plain") == 5


def test_boxes_stay_rectangular_even_with_colour_inside():
    lines = ui.box(["\033[31mcoloured\033[0m", "plain"], title="T").split("\n")
    widths = {ui.plain_len(line) for line in lines}
    assert len(widths) == 1, f"ragged box: {widths}"


def test_overlong_lines_are_trimmed_not_wrapped():
    box = ui.box(["x" * 500])
    widths = {ui.plain_len(line) for line in box.split("\n")}
    assert len(widths) == 1
    assert "…" in box


@pytest.mark.parametrize("env", [{"NO_COLOR": "1"}, {"TERM": "dumb"}])
def test_colour_is_disabled_when_the_environment_says_so(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTSWAP_FORCE_COLOR", raising=False)
    assert ui._colour_enabled() is False


def test_banner_has_a_narrow_fallback(monkeypatch):
    monkeypatch.setattr(ui, "width", lambda: 50)
    assert "AGENTSWAP" not in ui.banner()
    monkeypatch.setattr(ui, "width", lambda: 90)
    assert len(ui.banner().split("\n")) > 4
