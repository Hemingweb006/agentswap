"""Writers raise a Handoff back into some vendor's on-disk session format.

Each module exposes `TARGET`, `write(handoff, ...) -> (path, session_id, records)`,
`default_root()` and `resume_command(session_id, cwd)`.
"""

from . import claude_code, codex

WRITERS = {
    claude_code.TARGET: claude_code,
    codex.TARGET: codex,
}

__all__ = ["claude_code", "codex", "WRITERS"]
