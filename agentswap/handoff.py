"""Turn a parsed session into the small thing that is actually worth carrying.

This is the part that separates agentswap from a transcoder. A transcoder moves
every byte; a handoff moves the state a new agent needs in order to keep going.
On a real session that is a ~60x difference, and on a long one it is the
difference between working and overflowing the target's context window.

Everything here is deterministic. No API key, no network, no model call -- run
it offline and get the same brief every time. An LLM could write nicer prose,
but it cannot know things the log does not contain, and making the core feature
depend on a paid call would be a bad trade for a tool whose whole purpose is
that you just ran out of credits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ir import Session

# Tool-argument keys that tend to hold a path, in rough priority order.
_PATH_KEYS = ("file_path", "path", "filename", "file", "notebook_path", "target_file")

# Tool names that indicate the agent changed something on disk. Every vendor
# names these differently -- Edit, apply_patch, edit_file, str_replace_editor --
# so match on the verb rather than maintaining a list that is always one
# vendor behind.
_MUTATING_VERBS = (
    "edit", "write", "patch", "create", "replace", "modify",
    "update", "delete", "remove", "rename", "move", "insert", "append",
)


def _is_mutating(name: str) -> bool:
    lowered = name.lower()
    return any(verb in lowered for verb in _MUTATING_VERBS)

MAX_GOAL = 600
MAX_STEP = 600
MAX_LISTED_FILES = 25
MAX_FAILURES = 8

# Compact mode budgets. Past these, pointing at the repo beats transmitting.
COMPACT_FILE_LIMIT = 8
COMPACT_FAILURES = 3
COMPACT_THREAD_HEAD = 1
COMPACT_THREAD_TAIL = 4


# Harness chatter and injected context that is not a human intent. Both CLIs
# file these as user messages, so without filtering the "goal" of a real Codex
# session came out as its AGENTS.md preamble rather than what the user asked.
_NOISE_PREFIXES = (
    "[request interrupted",
    "continue from where you left off",
    "<system-reminder>",
    "<environment_context>",
    "[tool result",
    "# agents.md instructions",
    "# claude.md instructions",
    "<instructions>",
    "caveat: the messages below were generated",
)

_NOISE_MARKERS = ("<INSTRUCTIONS>", "## Available skills", "### Available skills")

# Codex wraps an attached-file prompt around the real request. Keep the request.
_REQUEST_MARKERS = (
    "## My request for Codex:",
    "## My request:",
    "# My request:",
)

# Paths that say nothing about the user's project.
_UNINTERESTING_DIRS = ("/var/folders/", "/tmp/", "/private/var/", "site-packages/", "/.cache/")


def _is_noise(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if any(lowered.startswith(p) for p in _NOISE_PREFIXES):
        return True
    # An instruction preamble that never gets to a real request is injected
    # context. One that does is a wrapped prompt -- keep it, unwrap it later.
    head = stripped[:1500]
    if any(marker in head for marker in _NOISE_MARKERS):
        return not any(marker in stripped for marker in _REQUEST_MARKERS)
    return False


def _unwrap_request(text: str) -> str:
    """Strip a file-attachment preamble off the front of a real request."""
    for marker in _REQUEST_MARKERS:
        if marker in text:
            return text.split(marker, 1)[1].strip()
    return text.strip()


def _looks_like_path(value: str) -> bool:
    if not value or len(value) > 400 or "\n" in value:
        return False
    if "<+" in value:  # a truncated digest, not a real path
        return False
    return "/" in value or "." in value.strip("/")


def _is_interesting(path: str, cwd: str = "") -> bool:
    """Scratch and cache paths say nothing about the project -- unless the
    project itself lives there. Anything under the working directory is always
    kept, or a repo checked out in /tmp would silently lose its own files.
    """
    if cwd and (path.startswith(cwd.rstrip("/") + "/") or path == cwd):
        return True
    return not any(frag in path for frag in _UNINTERESTING_DIRS)


def _extract_paths(name: str, args: dict) -> list[str]:
    """Best-effort recovery of which files a tool call touched."""
    found: list[str] = []
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and _looks_like_path(value):
            found.append(value)
    if found:
        return found

    # apply_patch and friends bury the path inside a patch body. A single call
    # may carry several hunks and several files, and on real data the naive
    # "rest of the line" grab swallowed the diff that follows the filename:
    #
    #   *** Update File: src/train.py @@ def _resolve(self, uri: str) -> str:
    #
    # Cut at the hunk marker, and at a newline, before taking the path.
    for value in args.values():
        if not isinstance(value, str):
            continue
        for marker in ("*** Update File:", "*** Add File:", "*** Delete File:", "--- a/", "+++ b/"):
            if marker not in value:
                continue
            for chunk in value.split(marker)[1:]:
                candidate = chunk.split("\n", 1)[0]
                candidate = candidate.split("@@", 1)[0].strip().strip('"').strip()
                if candidate and _looks_like_path(candidate) and candidate not in found:
                    found.append(candidate)
    return found


@dataclass
class Handoff:
    """The portable state of a session."""

    goal: str = ""
    next_step: str = ""
    source_tool: str = ""
    source_session: str = ""
    cwd: str = ""
    git_branch: str = ""
    model: str = ""
    started: str = ""
    ended: str = ""
    intent_thread: list[str] = field(default_factory=list)
    recent_focus: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    tools_used: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def render(self, compact: bool = True, detail_path: str = "") -> str:
        """The brief, as the message the receiving agent will actually read.

        Compact mode exists because both agents share a filesystem and a git
        repo. Anything the receiving agent can look up locally is waste to
        transmit -- and worse than waste for the file list, which reports what
        the previous agent *attempted* while `git diff` reports what actually
        landed. Carry the irrecoverable (intent, decisions, dead ends), point
        at everything else.
        """
        lines: list[str] = []
        lines.append("# Session handoff")
        lines.append("")
        lines.append(
            f"You are picking up work already in progress. It started in "
            f"`{self.source_tool}` and is being continued here. Nothing below is "
            f"a new request -- it is context."
        )
        lines.append("")

        lines.append("## Original request")
        lines.append(self.goal or "_not recorded_")
        lines.append("")

        if self.recent_focus:
            lines.append("## Where attention had moved")
            lines.append("The session drifted from the opening request. Most recent context:")
            for msg in self.recent_focus:
                lines.append(f"- {msg}")
            lines.append("")

        if self.cwd or self.git_branch:
            lines.append("## Where")
            if self.cwd:
                lines.append(f"- working directory: `{self.cwd}`")
            if self.git_branch:
                lines.append(f"- branch: `{self.git_branch}`")
            lines.append("")

        if self.files_touched:
            lines.append("## Files already changed")
            many = len(self.files_touched)
            if compact and many > COMPACT_FILE_LIMIT:
                lines.append(
                    f"{many} files were edited. Run `git diff --stat` for the real "
                    "list -- it is authoritative, this session's log only records "
                    "what was attempted."
                )
            else:
                lines.append("Confirm against `git diff` -- this is what the previous "
                             "agent attempted, not necessarily what landed.")
                for path in self.files_touched[:MAX_LISTED_FILES]:
                    lines.append(f"- `{path}`")
                extra = many - MAX_LISTED_FILES
                if extra > 0:
                    lines.append(f"- _...and {extra} more_")
            lines.append("")

        # Files that were only read are always recoverable and rarely load-bearing.
        if self.files_read and not compact:
            lines.append("## Files already examined")
            lines.append(", ".join(f"`{p}`" for p in self.files_read[:MAX_LISTED_FILES]))
            lines.append("")

        if self.failures:
            lines.append("## Already tried and failed")
            lines.append("Do not repeat these without a reason to expect a different result.")
            cap = COMPACT_FAILURES if compact else MAX_FAILURES
            for item in self.failures[:cap]:
                lines.append(f"- {item}")
            lines.append("")

        if len(self.intent_thread) > 1:
            lines.append("## What was asked, in order")
            thread = self.intent_thread
            if compact and len(thread) > COMPACT_THREAD_HEAD + COMPACT_THREAD_TAIL:
                head = thread[:COMPACT_THREAD_HEAD]
                tail = thread[-COMPACT_THREAD_TAIL:]
                for i, msg in enumerate(head, 1):
                    lines.append(f"{i}. {msg}")
                lines.append(f"   _...{len(thread) - len(head) - len(tail)} more requests..._")
                start = len(thread) - len(tail) + 1
                for i, msg in enumerate(tail, start):
                    lines.append(f"{i}. {msg}")
            else:
                for i, msg in enumerate(thread, 1):
                    lines.append(f"{i}. {msg}")
            lines.append("")

        lines.append("## Next step")
        lines.append(self.next_step or "_resume from the last request above_")
        lines.append("")
        lines.append(
            "This is the last thing the user *typed*, not necessarily the last "
            "thing that happened. Check the repo state before assuming it is undone."
        )
        lines.append("")

        if self.notes:
            lines.append("## Caveats")
            for note in self.notes:
                lines.append(f"- {note}")
            lines.append("")

        if compact and detail_path:
            lines.append(f"_Fuller detail, if you need it: `{detail_path}`_")

        origin = f"{self.source_tool} session {self.source_session or '(unknown)'}"
        span = f"{self.started} to {self.ended}" if self.started else "unknown span"
        lines.append(f"_Ported by agentswap from {origin}, {span}._")
        return "\n".join(lines)


# Appended when the receiving agent should orient before spending anything.
# You are switching tools because the last one ran out of budget; letting the
# next one auto-continue on a brief nobody checked spends the new budget on an
# unverified premise. One cheap turn buys a correctness check.
CONFIRM_SUFFIX = """

---

**Do not start work yet.** First: confirm in two or three sentences what you
understand the current state to be and what you think the next action is. If
anything above looks stale or contradicts what you find in the repo, say so.
Then wait for my go-ahead."""

CONTINUE_SUFFIX = """

---

Continue from **Next step**. Verify the repo state before assuming any of the
above is still accurate."""


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def build(session: Session) -> Handoff:
    """Derive a Handoff from a parsed session."""
    handoff = Handoff(
        source_tool=session.source,
        source_session=session.session_id,
        cwd=session.cwd,
        git_branch=session.git_branch,
        model=str(session.stats.get("model", "")),
        started=session.started,
        ended=session.ended,
        tools_used=session.tool_histogram(),
    )

    asks = [
        _unwrap_request(t.text)
        for t in session.user_turns()
        if not _is_noise(t.text)
    ]
    # Collapse repeats that survive the reader's own dedupe.
    humans: list[str] = []
    for ask in asks:
        if not humans or " ".join(ask.split())[:200] != " ".join(humans[-1].split())[:200]:
            humans.append(ask)

    if humans:
        handoff.goal = _clip(humans[0], MAX_GOAL)
        handoff.next_step = _clip(humans[-1], MAX_STEP)
        handoff.intent_thread = [_clip(t, 200) for t in humans]
        # Long sessions drift. The opening request is often no longer what the
        # work is about, so the tail of the thread is reported separately
        # rather than pretending message #1 is still the goal.
        if len(humans) > 3:
            handoff.recent_focus = [_clip(t, 200) for t in humans[-3:-1]]

    touched: list[str] = []
    read: list[str] = []
    for turn in session.turns:
        for call in turn.tool_calls:
            paths = _extract_paths(call.name, call.args)
            if not paths:
                continue
            bucket = touched if _is_mutating(call.name) else read
            for path in paths:
                if path not in bucket:
                    bucket.append(path)

    # A file that was written is more interesting than one merely read, and
    # temp/cache paths say nothing about the project either way.
    cwd = session.cwd or ""
    handoff.files_touched = [p for p in touched if _is_interesting(p, cwd)]
    handoff.files_read = [
        p for p in read if p not in touched and _is_interesting(p, cwd)
    ]

    # Errored tool results are the only deterministic signal we have for
    # "this was tried and did not work". It is a proxy for dead ends, not a
    # replacement -- reasoning that ruled an approach out is not recoverable.
    for turn in session.turns:
        for res in turn.tool_results:
            if res.is_error and res.preview:
                label = res.name or "tool"
                entry = f"`{label}` failed: {_clip(res.preview, 180)}"
                if entry not in handoff.failures:
                    handoff.failures.append(entry)

    if session.stats.get("compactions"):
        handoff.notes.append(
            f"The source session was compacted {session.stats['compactions']}x, "
            "so some early detail was already summarized away before this handoff."
        )
    if session.stats.get("dropped_media"):
        handoff.notes.append(
            f"{session.stats['dropped_media']} attachments (images, PDFs) were in the "
            "original session and are not carried over. Re-attach them if they matter."
        )
    if session.source == "claude-code":
        handoff.notes.append(
            "Reasoning is not portable -- Claude Code stores thinking blocks "
            "encrypted, so only visible messages and tool activity survive."
        )
    unknown = session.stats.get("unrecognized") or {}
    if unknown:
        handoff.notes.append(
            f"The reader did not understand {sum(unknown.values())} record(s) in the "
            "source file; something may be missing from this brief."
        )

    return handoff
