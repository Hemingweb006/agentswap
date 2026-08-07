"""What agentswap knows about each coding agent as a runnable program.

Readers and writers deal with files. This deals with processes: which binary to
invoke, how to ask it to resume, and how to find the session it just wrote.

Design note -- we do NOT wrap the child in a pseudo-terminal. These are
full-screen TUIs; proxying their stdin and stdout means handling raw mode,
resize signals, escape sequences, and a hotkey that does not collide with the
child's own bindings, and it breaks every time a vendor redesigns their
interface. Instead the child inherits the terminal completely and we act in the
gap after it exits. Switching mid-turn is the only thing lost, and nobody needs
that.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import readers
from .ir import Session
from .readers import antigravity as ag_reader
from .writers import claude_code as cc_writer


@dataclass(frozen=True)
class AgentSpec:
    """How to run one agent, and how to hand it a session.

    `delivery` is the important field. There are two ways to give an agent
    work that started somewhere else:

    "session"  Write a session file the tool will resume. Requires the tool to
               discover sessions by scanning the filesystem. Claude Code does.

    "prompt"   Launch the tool fresh with the brief as its opening message.
               Required when the tool resolves session ids through a private
               index rather than the filesystem -- Codex keeps a SQLite thread
               store, so a dropped-in rollout is real but invisible to
               `codex resume`. Forging an index entry means reverse-engineering
               a schema that ships migrations, and getting it wrong corrupts
               the user's actual thread history. Not worth it for a feature
               that a prompt delivers just as well.
    """

    key: str
    label: str
    binary: str
    source: str | None
    delivery: str
    session_root: Callable[[], Path]
    session_glob: str
    target: str | None = None
    resume: Callable[[str], list[str]] | None = None
    prompt_args: Callable[[str], list[str]] | None = None
    scoped_dir: Callable[[str], str] | None = None
    install_hint: str = ""
    extra_bin_dirs: tuple[str, ...] = ()

    def locate(self) -> tuple[Path | None, bool]:
        """Return (path to the binary, is_on_path).

        `shutil.which` alone conflates "you do not have this" with "you have
        this but your shell cannot see it". Installers routinely drop binaries
        in ~/.local/bin, which is not on PATH by default on macOS -- reporting
        that as "not installed" sends people to reinstall something they
        already have.
        """
        found = shutil.which(self.binary)
        if found:
            return Path(found), True
        for directory in self.extra_bin_dirs:
            candidate = Path(directory).expanduser() / self.binary
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate, False
        return None, False

    @property
    def installed(self) -> bool:
        return shutil.which(self.binary) is not None

    @property
    def present_but_hidden(self) -> bool:
        path, on_path = self.locate()
        return path is not None and not on_path

    @property
    def can_receive(self) -> bool:
        if not self.installed:
            return False
        if self.delivery == "session":
            return self.target is not None
        return self.prompt_args is not None


SPECS: dict[str, AgentSpec] = {
    "claude-code": AgentSpec(
        key="claude-code",
        label="Claude Code",
        binary="claude",
        source="claude-code",
        # Verified working: Claude Code discovers sessions by scanning
        # ~/.claude/projects, so a written file resumes like any other.
        delivery="session",
        target="claude-code",
        resume=lambda sid: ["--resume", sid],
        session_root=lambda: Path.home() / ".claude" / "projects",
        session_glob="**/*.jsonl",
        scoped_dir=cc_writer.slugify_cwd,
        install_hint="see https://code.claude.com/docs",
        extra_bin_dirs=("~/.local/bin", "~/.claude/local", "/usr/local/bin", "/opt/homebrew/bin"),
    ),
    "codex": AgentSpec(
        key="codex",
        label="Codex",
        binary="codex",
        source="codex",
        # `codex resume <id>` resolves through a SQLite thread index, so a
        # dropped-in rollout is never found. Verified on a real machine:
        # "No saved session found with ID ...". Deliver as a prompt instead.
        delivery="prompt",
        prompt_args=lambda text: [text],
        session_root=lambda: Path.home() / ".codex" / "sessions",
        session_glob="**/rollout-*.jsonl",
        install_hint="see https://github.com/openai/codex",
        extra_bin_dirs=("~/.local/bin", "/usr/local/bin", "/opt/homebrew/bin"),
    ),
    "antigravity": AgentSpec(
        key="antigravity",
        label="Antigravity",
        binary="agy",
        source="antigravity",
        # Its authoritative store is protobuf inside SQLite, so a written
        # conversation is out of reach -- prompt delivery is the only route.
        delivery="prompt",
        prompt_args=lambda text: [text],
        session_root=ag_reader.default_root,
        session_glob=ag_reader.LOG_GLOB,
        # Native Go binary; the installer drops it in ~/.local/bin, which is
        # frequently absent from PATH on macOS.
        install_hint="curl -fsSL https://antigravity.google/cli/install.sh | bash",
        extra_bin_dirs=(
            "~/.local/bin",
            "~/.antigravity/bin",
            "/usr/local/bin",
            "/opt/homebrew/bin",
        ),
    ),
}


def installed() -> list[AgentSpec]:
    return [spec for spec in SPECS.values() if spec.installed]


def targets() -> list[str]:
    """Agents a session can be carried into, installed or not.

    Capability, not availability -- `port` should let you prepare a handoff for
    a tool you have not installed yet. Every agent has a delivery route, so
    this is every agent; the function exists so the CLI stops deriving its
    target list from the writers dict. That was wrong: writers only cover
    session delivery, so prompt-delivery targets like Antigravity were silently
    unreachable from `port` even though `run` could carry to them fine.
    """
    return sorted(
        key
        for key, spec in SPECS.items()
        if (spec.delivery == "session" and spec.target) or spec.prompt_args
    )


def launch(spec: AgentSpec, cwd: str, extra: list[str] | None = None) -> int:
    """Hand the terminal to the agent and wait. Returns its exit code."""
    cmd = [spec.binary, *(extra or [])]
    try:
        completed = subprocess.run(cmd, cwd=cwd)
    except FileNotFoundError:
        return 127
    except KeyboardInterrupt:
        return 130
    return completed.returncode


def find_session_after(spec: AgentSpec, cwd: str, since: float) -> Path | None:
    """Locate the session the child just wrote.

    Deliberately does not parse the terminal -- we never captured it. Instead
    we look for the newest session file this tool touched after we launched,
    and confirm its recorded cwd matches where we ran.
    """
    root = spec.session_root()
    if not root.exists():
        return None

    search_root = root
    if spec.scoped_dir is not None:
        scoped = root / spec.scoped_dir(cwd)
        if scoped.exists():
            search_root = scoped

    candidates = [
        path
        for path in search_root.glob(spec.session_glob)
        if path.stat().st_mtime >= since - 1
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # Codex is not cwd-scoped on disk, so confirm from the record itself.
    for path in candidates:
        if spec.scoped_dir is not None:
            return path
        try:
            session = readers.read(path)
        except (ValueError, RuntimeError, OSError):
            continue
        if not session.cwd or Path(session.cwd) == Path(cwd):
            return path
    return candidates[0]


# Phrases that show up in a session when the provider cut the agent off. Kept
# broad on purpose: a false positive costs one extra prompt, a false negative
# costs the whole feature.
_QUOTA_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota",
    "insufficient_quota",
    "429",
    "too many requests",
    "you've reached your",
    "you have reached your",
    "credit balance",
    "out of credits",
    "upgrade to continue",
    "resets at",
)


def looks_like_quota_exhaustion(session: Session, tail: int = 12) -> str | None:
    """Return the matched phrase if the session ended on a limit, else None.

    Read from the session rather than from stderr, because the child owned the
    terminal and we never saw its output.
    """
    for turn in reversed(session.turns[-tail:] if tail else session.turns):
        haystacks = [turn.text or ""]
        haystacks += [r.preview or "" for r in turn.tool_results]
        for text in haystacks:
            lowered = text.lower()
            for marker in _QUOTA_MARKERS:
                if marker in lowered:
                    return marker
    return None
