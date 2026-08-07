"""agentswap -- move a coding-agent session between tools without re-explaining it.

Status: reader stage only. Run `python -m agentswap.cli inspect <session.jsonl>`
to see what is actually inside a transcript.
"""

from .ir import Session, Turn, ToolCall, ToolResult

__version__ = "0.1.0"
__all__ = ["Session", "Turn", "ToolCall", "ToolResult"]
