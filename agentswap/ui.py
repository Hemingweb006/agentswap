"""Terminal presentation: banner, boxes, colour.

Three rules everything here follows:

1. **Never emit escape codes into a pipe.** Colour is resolved once, at import,
   from isatty plus NO_COLOR plus TERM. `agentswap list | grep` must stay clean.
2. **Never assume 80 columns.** The banner has a compact fallback and boxes
   clamp to the real width, so a split pane does not produce ragged garbage.
3. **Never let decoration hide the number.** The interesting thing on screen is
   the compression ratio and which tool is about to run; the frame exists to
   make those findable, not to compete with them.
"""

from __future__ import annotations

import os
import shutil
import sys

# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------

def _colour_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("AGENTSWAP_FORCE_COLOR"):
        return True
    if os.environ.get("TERM", "") == "dumb":
        return False
    return sys.stdout.isatty()


COLOUR = _colour_enabled()


def _c(code: str) -> str:
    return code if COLOUR else ""


RESET = _c("\033[0m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")

CYAN = _c("\033[36m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
RED = _c("\033[31m")
MAGENTA = _c("\033[35m")
GREY = _c("\033[90m")

ACCENT = CYAN
ALERT = YELLOW
OK = GREEN
BAD = RED


def width() -> int:
    return max(48, min(shutil.get_terminal_size((80, 24)).columns, 100))


def plain_len(text: str) -> int:
    """Visible length, ignoring escape sequences."""
    out, i = 0, 0
    while i < len(text):
        if text[i] == "\033":
            while i < len(text) and text[i] != "m":
                i += 1
            i += 1
            continue
        out += 1
        i += 1
    return out


# --------------------------------------------------------------------------
# Banner
# --------------------------------------------------------------------------

BANNER = r"""    ___   _____________   ______________       _____    ____
   /   | / ____/ ____/ | / /_  __/ ___/ |     / /   |  / __ \
  / /| |/ / __/ __/ /  |/ / / /  \__ \| | /| / / /| | / /_/ /
 / ___ / /_/ / /___/ /|  / / /  ___/ /| |/ |/ / ___ |/ ____/
/_/  |_\____/_____/_/ |_/ /_/  /____/ |__/|__/_/  |_/_/"""

BANNER_WIDTH = 61

COMPACT_BANNER = r"""  _   _____ ___ _  _ _____ _____      ___   ___
 /_\ / __| __| \| |_   _/ __\ \    / /_\ | _ \
/ _ \ (_ | _|| .` | | | \__ \\ \/\/ / _ \|  _/
\_/ \_\___|___|_|\_| |_| |___/ \_/\_/_/ \_\_|"""


def banner(subtitle: str = "", version: str = "") -> str:
    cols = width()
    art = BANNER if cols >= BANNER_WIDTH + 2 else COMPACT_BANNER
    if cols < 48:
        art = "agentswap"

    lines = [f"{ACCENT}{BOLD}{line}{RESET}" for line in art.split("\n")]
    out = ["", *lines]
    if subtitle:
        tail = f"{GREY}{subtitle}{RESET}"
        if version:
            tail += f"{GREY}  ·  {version}{RESET}"
        out.append(f" {tail}")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Boxes
# --------------------------------------------------------------------------

TL, TR, BL, BR, H, V = "╭", "╮", "╰", "╯", "─", "│"


def rule(label: str = "", colour: str = "") -> str:
    cols = width()
    colour = colour or GREY
    if not label:
        return f"{colour}{H * cols}{RESET}"
    head = f"{H}{H} {label} "
    pad = max(0, cols - plain_len(head))
    return f"{colour}{head}{H * pad}{RESET}"


def box(lines: list[str], title: str = "", colour: str = "", pad: int = 1) -> str:
    """A framed block. Lines may contain colour; framing accounts for it."""
    colour = colour or GREY
    cols = width()
    inner = cols - 2 - (pad * 2)

    body = []
    for line in lines:
        visible = plain_len(line)
        if visible > inner:
            # Trim from the right, preserving any escapes already emitted.
            keep, count, i = [], 0, 0
            while i < len(line) and count < inner - 1:
                if line[i] == "\033":
                    start = i
                    while i < len(line) and line[i] != "m":
                        i += 1
                    i += 1
                    keep.append(line[start:i])
                    continue
                keep.append(line[i])
                count += 1
                i += 1
            line = "".join(keep) + "…"
            visible = plain_len(line)
        body.append(f"{colour}{V}{RESET}{' ' * pad}{line}{' ' * (inner - visible)}{' ' * pad}{colour}{V}{RESET}")

    if title:
        head = f"{TL}{H} {title} "
        top = f"{colour}{head}{H * max(0, cols - plain_len(head) - 1)}{TR}{RESET}"
    else:
        top = f"{colour}{TL}{H * (cols - 2)}{TR}{RESET}"
    bottom = f"{colour}{BL}{H * (cols - 2)}{BR}{RESET}"
    return "\n".join([top, *body, bottom])


def kv(key: str, value: str, key_width: int = 10) -> str:
    return f"{GREY}{key.ljust(key_width)}{RESET}{value}"


def arrow(left: str, right: str, note: str = "") -> str:
    line = f"{BOLD}{left}{RESET}  {ACCENT}──▶{RESET}  {BOLD}{right}{RESET}"
    if note:
        line += f"   {GREEN}{note}{RESET}"
    return line
