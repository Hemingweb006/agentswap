"""Regression tests for bugs found by running against real sessions.

Every test here corresponds to something a synthetic fixture did not catch and
a real transcript did. They are named after the symptom rather than the
function, because the symptom is what a future contributor will recognize when
they reintroduce it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentswap import handoff, readers, writers  # noqa: E402
from agentswap.handoff import _extract_paths, _is_noise, _unwrap_request  # noqa: E402
from agentswap.ir import compact_patch  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
REAL_SHAPE = FIXTURES / "rollout-real-shape.jsonl"
SIMPLE = FIXTURES / "rollout-2026-08-06T09-15-00-2f1a.jsonl"


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def test_detects_codex_from_contents_not_filename(tmp_path):
    moved = tmp_path / "some-random-name.jsonl"
    moved.write_text(REAL_SHAPE.read_text(), encoding="utf-8")
    assert readers.detect(moved) == "codex"


def test_unknown_format_raises_with_a_useful_message(tmp_path):
    junk = tmp_path / "junk.jsonl"
    junk.write_text('{"hello": "world"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="could not identify"):
        readers.read(junk)


# --------------------------------------------------------------------------
# Codex records every user message twice
# --------------------------------------------------------------------------

def test_duplicate_user_messages_are_collapsed():
    session = readers.read(REAL_SHAPE)
    assert session.stats["duplicate_user_turns_dropped"] > 0
    texts = [t.text for t in session.turns if t.role == "user"]
    assert len(texts) == len(set(texts)), "a prompt survived twice"


# --------------------------------------------------------------------------
# Injected context was being read as the user's goal
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "# AGENTS.md instructions for /x <INSTRUCTIONS> ## Skills\n### Available skills\n- a: b",
        "<environment_context> <cwd>/x</cwd> </environment_context>",
        "[Request interrupted by user]",
        "Continue from where you left off.",
    ],
)
def test_injected_context_is_not_treated_as_intent(text):
    assert _is_noise(text) is True


def test_a_real_request_is_never_filtered():
    assert _is_noise("i want to challenge you to solve a 100000 dollar problem") is False


def test_wrapped_request_keeps_the_request_and_drops_the_wrapper():
    wrapped = (
        "# Files mentioned by the user:\n## a.ipynb: /x/a.ipynb\n"
        "## My request for Codex: why didn't you use it?"
    )
    assert _is_noise(wrapped) is False
    assert _unwrap_request(wrapped) == "why didn't you use it?"


def test_goal_is_the_real_request_not_the_preamble():
    brief = handoff.build(readers.read(REAL_SHAPE))
    assert "100000 dollar problem" in brief.goal
    assert "AGENTS.md" not in brief.goal


# --------------------------------------------------------------------------
# Patch bodies: paths came out as diff hunks, and only the first file survived
# --------------------------------------------------------------------------

def test_hunk_marker_is_not_captured_as_part_of_the_path():
    paths = _extract_paths("apply_patch", {"input": "*** Update File: src/train.py @@ def go():"})
    assert paths == ["src/train.py"]


def test_every_file_in_a_multi_file_patch_is_recovered():
    body = (
        "*** Update File: a/one.py\n@@ def x():\n     pass\n"
        "*** Update File: a/two.py\n@@ def y():\n     pass\n"
        "*** Add File: a/three.py\n"
    )
    assert _extract_paths("apply_patch", {"input": body}) == ["a/one.py", "a/two.py", "a/three.py"]


def test_long_patch_bodies_keep_their_file_markers_instead_of_being_clipped():
    body = "*** Update File: a/one.py\n" + ("x" * 5000) + "\n*** Update File: a/two.py\n"
    compacted = compact_patch(body)
    assert "a/one.py" in compacted and "a/two.py" in compacted
    assert len(compacted) < 200, "compaction should shrink the body, not just trim it"


@pytest.mark.parametrize(
    "name,mutates",
    [
        ("Edit", True), ("Write", True), ("MultiEdit", True),      # Claude Code
        ("apply_patch", True), ("str_replace_editor", True),        # Codex
        ("edit_file", True), ("create_file", True),                 # Antigravity
        ("delete_path", True), ("rename_symbol", True),
        ("Read", False), ("Grep", False), ("read_file", False),
        ("exec_command", False), ("web_search", False),
    ],
)
def test_mutating_tools_are_matched_by_verb_not_by_a_hardcoded_list(name, mutates):
    from agentswap.handoff import _is_mutating

    assert _is_mutating(name) is mutates


def test_non_patch_strings_are_left_alone():
    assert compact_patch("just a normal argument") is None


# --------------------------------------------------------------------------
# Hosted tool calls were enumerated instead of matched
# --------------------------------------------------------------------------

def test_every_hosted_tool_call_type_is_understood():
    session = readers.read(REAL_SHAPE)
    assert not session.stats["unrecognized"], session.stats["unrecognized"]
    tools = session.tool_histogram()
    for expected in ("web_search", "file_search", "exec_command", "apply_patch"):
        assert expected in tools, f"{expected} missing from {tools}"


def test_a_genuinely_new_shape_is_reported_rather_than_dropped(tmp_path):
    path = tmp_path / "rollout-x.jsonl"
    path.write_text(
        json.dumps({"timestamp": "t", "type": "response_item",
                    "payload": {"type": "something_brand_new"}}) + "\n",
        encoding="utf-8",
    )
    session = readers.read(path)
    assert session.stats["unrecognized"] == {"response_item/something_brand_new": 1}


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------

def test_claude_code_slug_matches_an_observed_real_path():
    observed_cwd = "/Users/reda/Library/Application Support/Claude/local_87a344e6/outputs"
    expected = "-Users-reda-Library-Application-Support-Claude-local-87a344e6-outputs"
    assert writers.claude_code.slugify_cwd(observed_cwd) == expected


def test_session_id_is_recovered_from_either_vendor_filename():
    claude = Path("/x/.claude/projects/-p/513c20b6-d3f6-4a44-aff0-e59d05250960.jsonl")
    codex = Path("/x/.codex/sessions/2026/02/14/"
                 "rollout-2026-02-14T19-06-19-019c5d55-0732-7a81-b03e-fd0479981afe.jsonl")
    zstd = Path(str(codex) + ".zst")

    assert readers.session_id_of(claude) == "513c20b6-d3f6-4a44-aff0-e59d05250960"
    assert readers.session_id_of(codex) == "019c5d55-0732-7a81-b03e-fd0479981afe"
    assert readers.session_id_of(zstd) == "019c5d55-0732-7a81-b03e-fd0479981afe"


def test_ambiguous_ids_return_every_match_newest_first(monkeypatch, tmp_path):
    import os
    import time

    day = tmp_path / "codex" / "2026" / "02" / "14"
    day.mkdir(parents=True)
    older = day / "rollout-2026-02-14T10-00-00-dupe1234-0000-0000-0000-000000000001.jsonl"
    newer = day / "rollout-2026-02-15T10-00-00-dupe1234-0000-0000-0000-000000000002.jsonl"
    for path in (older, newer):
        path.write_text(SIMPLE.read_text(), encoding="utf-8")
    os.utime(older, (time.time() - 9999, time.time() - 9999))

    monkeypatch.setattr(readers.codex, "default_root", lambda: tmp_path / "codex")
    monkeypatch.setattr(readers.claude_code, "default_root", lambda: tmp_path / "none")

    matches = readers.find("dupe1234")
    assert len(matches) == 2
    assert matches[0] == newer, "most recent should win"
    assert readers.resolve("dupe1234") == newer


def test_a_bare_session_id_resolves_without_a_full_path(monkeypatch, tmp_path):
    # Pasting an id straight out of a tool's output is the obvious thing to
    # try, and it used to fail with "no such file".
    project = tmp_path / "-tmp-proj"
    project.mkdir(parents=True)
    target = project / "abc12345-0000-4444-8888-999999999999.jsonl"
    target.write_text(SIMPLE.read_text(), encoding="utf-8")

    monkeypatch.setattr(readers.claude_code, "default_root", lambda: tmp_path)
    monkeypatch.setattr(readers.codex, "default_root", lambda: tmp_path / "none")

    assert readers.resolve("abc12345-0000-4444-8888-999999999999") == target
    assert readers.resolve("abc12345") == target
    assert readers.resolve(str(target)) == target
    assert readers.resolve("no-such-session-anywhere") is None


@pytest.mark.parametrize("target", ["claude-code", "codex"])
@pytest.mark.parametrize("source", [REAL_SHAPE, SIMPLE])
def test_written_sessions_round_trip(tmp_path, source, target):
    brief = handoff.build(readers.read(source))
    path, session_id, _ = writers.WRITERS[target].write(brief, root=tmp_path)

    assert path.exists()
    reparsed = readers.read(path)
    humans = reparsed.user_turns()
    assert len(humans) == 1
    body = humans[0].text
    assert "# Session handoff" in body
    assert "## Next step" in body
    assert session_id


@pytest.mark.parametrize("target", ["claude-code", "codex"])
def test_writers_refuse_to_clobber_an_existing_session(tmp_path, target):
    brief = handoff.build(readers.read(SIMPLE))
    writer = writers.WRITERS[target]
    _, session_id, _ = writer.write(brief, root=tmp_path)
    with pytest.raises(FileExistsError):
        writer.write(brief, root=tmp_path, session_id=session_id)


@pytest.mark.parametrize("target", ["claude-code", "codex"])
def test_dry_run_writes_nothing(tmp_path, target):
    brief = handoff.build(readers.read(SIMPLE))
    path, _, records = writers.WRITERS[target].write(brief, root=tmp_path, dry_run=True)
    assert records
    assert not path.exists()


def test_version_is_read_from_local_sessions_rather_than_hardcoded(tmp_path):
    day = tmp_path / "2026" / "02" / "14"
    day.mkdir(parents=True)
    (day / "rollout-x.jsonl").write_text(
        json.dumps({"timestamp": "t", "type": "session_meta",
                    "payload": {"id": "1", "cli_version": "0.100.0-alpha.10"}}) + "\n",
        encoding="utf-8",
    )
    assert writers.codex.detect_version(tmp_path) == "0.100.0-alpha.10"


def test_version_falls_back_when_there_is_nothing_to_read(tmp_path):
    assert writers.codex.detect_version(tmp_path / "empty") == writers.codex.FALLBACK_VERSION


# --------------------------------------------------------------------------
# The point of the whole thing
# --------------------------------------------------------------------------

def test_compact_mode_points_at_git_instead_of_listing_many_files():
    from agentswap.handoff import Handoff

    brief = Handoff(goal="g", next_step="n", cwd="/p")
    brief.files_touched = [f"/p/file{i}.py" for i in range(20)]

    compact = brief.render(compact=True)
    full = brief.render(compact=False)

    assert "git diff --stat" in compact
    assert "file7.py" not in compact, "compact mode should not enumerate 20 files"
    assert "file7.py" in full, "full mode should still list them"
    assert len(compact) < len(full)


def test_compact_mode_still_lists_a_short_file_set():
    from agentswap.handoff import Handoff

    brief = Handoff(goal="g", next_step="n")
    brief.files_touched = ["/p/a.py", "/p/b.py"]
    assert "a.py" in brief.render(compact=True)


def test_compact_mode_keeps_the_ends_of_a_long_intent_thread():
    from agentswap.handoff import Handoff

    brief = Handoff(goal="g", next_step="n")
    brief.intent_thread = [f"request number {i}" for i in range(15)]
    compact = brief.render(compact=True)
    assert "request number 0" in compact, "the original ask must survive"
    assert "request number 14" in compact, "the latest ask must survive"
    assert "more requests" in compact


def test_the_confirm_suffix_tells_the_agent_not_to_start():
    from agentswap import handoff as h

    assert "Do not start work yet" in h.CONFIRM_SUFFIX
    assert "wait for my go-ahead" in h.CONFIRM_SUFFIX
    assert "Do not start" not in h.CONTINUE_SUFFIX


def test_the_handoff_is_dramatically_smaller_than_the_transcript():
    session = readers.read(REAL_SHAPE)
    brief = handoff.build(session)
    assert len(brief.render()) < session.stats["file_bytes"]


def test_the_brief_carries_what_the_next_agent_needs():
    brief = handoff.build(readers.read(REAL_SHAPE))
    rendered = brief.render()
    assert brief.goal and brief.next_step
    assert "/Users/reda/Documents/code" in rendered
    assert any("train_medgemma_3d_lora.py" in p for p in brief.files_touched)
    assert any("build_brats_manifest.py" in p for p in brief.files_touched)
