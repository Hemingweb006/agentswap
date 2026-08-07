<p align="center">
  <img src="docs/banner.png" alt="agentswap" width="720">
</p>

<h3 align="center">Move a coding-agent session between tools without re-explaining your project.</h3>

<p align="center">
  <a href="https://pypi.org/project/agentswap/"><img alt="PyPI" src="https://img.shields.io/pypi/v/agentswap?color=2dd4a7&labelColor=1f2430"></a>
  <a href="https://pypi.org/project/agentswap/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/agentswap?color=2dd4a7&labelColor=1f2430"></a>
  <a href="https://github.com/hemingweb006/agentswap/actions/workflows/tests.yml"><img alt="Tests" src="https://img.shields.io/github/actions/workflow/status/hemingweb006/agentswap/tests.yml?label=tests&color=2dd4a7&labelColor=1f2430"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-2dd4a7?labelColor=1f2430"></a>
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-none-2dd4a7?labelColor=1f2430">
  <a href="https://github.com/hemingweb006/agentswap/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/hemingweb006/agentswap?color=2dd4a7&labelColor=1f2430"></a>
</p>

---

You burn through your credits on one agent, switch to another, and spend the next
twenty minutes re-describing what you were building, what you already changed, and
which three fixes you already tried and abandoned.

None of that was lost. It is sitting in a JSONL file on your disk. Nobody reads it.

```
You've hit your usage limit. Upgrade to Plus to continue using Codex,
or try again at Aug 28th, 2026 8:51 PM.
```

Three weeks. Or one keystroke.

<p align="center">
  <img src="docs/demo.png" alt="agentswap carrying a session from Codex into Antigravity after a usage limit" width="820">
</p>

## Install

```bash
pip install agentswap
```

Zero dependencies. A tool whose entire premise is *"you just ran out of credits"*
should not need credits, an API key, or a network connection to run.

## Use it

```bash
cd your-project
agentswap run
```

Pick an agent. Work normally — it owns your terminal completely. When it stops,
agentswap offers to carry the session somewhere else.

```
╭─ WALL ───────────────────────────────────────────────────╮
│ Codex hit a usage limit                                  │
│ matched "usage limit" in the session it just wrote       │
╰──────────────────────────────────────────────────────────╯

  ▸ [s] switch  carry this session over
    [q] quit

  ❯ continue elsewhere? [s]

╭─ CARRIED ────────────────────────────────────────────────╮
│ 55.6KB  ──▶  957B    59x smaller                         │
│ into a new Antigravity session, as its opening message   │
│ it will confirm what it understands, then wait for you   │
╰──────────────────────────────────────────────────────────╯
```

You keep going. Nothing was re-explained.

## Supported

| Tool | Read from | Receives work as | Status |
|:--|:--|:--|:--|
| **Claude Code** | `~/.claude/projects/<slug>/<id>.jsonl` | a session it resumes | verified |
| **Codex** | `~/.codex/sessions/**/rollout-*.jsonl[.zst]` | an opening prompt | verified |
| **Antigravity** | `<appdata>/brain/<id>/.system_generated/logs/` | an opening prompt | verified |

## The idea

**Don't port the transcript. Reconstruct the handoff.**

Session files are enormous and almost entirely worthless. Measured on a real 7.7MB
Claude Code transcript:

| | bytes | ~tokens | |
|:--|--:|--:|:--|
| the transcript | 7,745,780 | 1,936,000 | 84% of it was base64 image attachments |
| a full brief | 8,202 | 2,050 | 944x smaller |
| **what agentswap sends** | **4,160** | **1,040** | **1,862x smaller** |

Existing tools transcode a session verbatim into the target's schema — moving
megabytes to carry kilobytes of meaning. And they do it on exactly the long
sessions where it hurts, which either overflow the target's context or bill you
for the entire history on your first message. Which is the cost you switched to
avoid.

The honest comparison isn't brief-versus-transcript, though. It's **brief versus
rediscovery**: with no handoff, the new agent re-greps and re-reads your repo for
10–20 turns — 50k–200k tokens — to learn what 1,000 tokens could have told it.

### Transmit only the irrecoverable

Both agents share a filesystem and a git repo. Listing 23 changed files costs ~500
tokens to say something `git diff --stat` says for free — and says *better*, since
it reports what actually landed instead of what the previous agent attempted.

So the brief points at the repo for anything the repo already knows, and spends its
budget on what nothing else can recover:

- the original request, and separately **where attention had actually moved** — long
  sessions drift, and pretending message #1 is still the goal is a lie
- decisions and constraints established along the way
- **dead ends already ruled out** ← the expensive one
- the current next step

That third bullet is the whole point. "What am I building" is one sentence you can
retype. What costs you the hour is that the new agent doesn't know you already tried
the obvious three fixes.

### The new agent confirms before it spends anything

By default the receiving agent reads the brief, says what it understands the state
to be, and **waits**.

You are switching because the last tool ran out of budget. Letting the next one
auto-continue on a brief nobody checked spends the new budget on an unverified
premise — and "next step" is the last thing you *typed*, not the last thing that
*happened*. One cheap turn buys a correctness check. Pass `--auto` to skip it.

## Commands

```bash
agentswap run                      # the main thing
agentswap run --with codex         # skip the picker
agentswap run --auto               # don't make the new agent confirm first

agentswap agents                   # which CLIs are installed and ready
agentswap list --here -v           # sessions for this project, with previews

agentswap inspect <id>             # what is actually inside a session
agentswap inspect <id> --turns     # the reconstructed conversation
agentswap inspect <id> --probe     # record shapes, including unknown ones

agentswap port <id> --to codex --show      # read the brief, write nothing
agentswap port <id> --to claude-code       # carry it across
```

**Everything takes a session id, not a path.** Claude Code names the file
`<id>.jsonl`, Codex names it `rollout-<timestamp>-<id>.jsonl`; agentswap searches
both and matches either. A unique prefix is enough, and ambiguous prefixes are
listed rather than silently guessed.

`port` never overwrites. A fresh session id is minted per run and an existing file
at the target path is a hard error.

## How it works

```
readers/          vendor formats  ──▶  one neutral representation
handoff.py        representation  ──▶  the small thing worth carrying
writers/          handoff         ──▶  a session the target resumes
agents.py         which binary, where its sessions live, how to hand it work
session_loop.py   launch, catch the exit, offer the switch, relaunch
```

Everything a vendor knows lives in exactly one file. These formats are internal,
undocumented, and drift without warning, so the blast radius of a change has to stay
inside one adapter.

**Two ways to hand an agent your work.** *Session delivery* writes a file the tool
resumes — only possible when it discovers sessions by scanning the filesystem, as
Claude Code does. *Prompt delivery* launches the tool fresh with the brief as its
first message, which is required when a tool resolves ids through a private index.
Codex indexes threads in SQLite, so a dropped-in rollout is a real, correct,
completely invisible file:

```
$ codex resume 193a0665-805c-4b71-98c8-290f3a761931
ERROR: No saved session found with ID 193a0665-...
```

Forging an index entry means reverse-engineering a schema that ships migrations, and
getting it wrong corrupts your actual thread history. A prompt delivers the same
context with nothing to break.

**It never wraps your terminal.** The agent gets the TTY completely — no
pseudo-terminal, no keystroke interception, nothing to break when a vendor redesigns
their TUI. agentswap acts in the gap after the agent exits. It never parses your
terminal either: to learn what happened it reads the session file the agent just
wrote, which is also how quota detection works.

## Probe mode

These formats are undocumented, so the readers count every record shape they fail to
recognize instead of silently dropping it:

```
$ agentswap inspect 019c5d55 --probe

UNRECOGNIZED shapes -- the reader skipped these:
    35  response_item/web_search_call
```

That output is a bug report. If you see a non-empty list, please open an issue with
it — that exact one led to matching every hosted tool call by suffix rather than
enumerating them.

## What is not verified

Be suspicious of this section's absence in other tools.

- **`codex resume` cannot load a written rollout file**, by design — see above. Codex
  and Antigravity are prompt-delivery only.
- **Antigravity's log schema is inferred**, not read from source. It launches
  correctly with carried context, but if `--probe` reports unknown shapes on your
  conversations, the adapter is missing something.
- **The brief tells the agent to check `git diff`** rather than running it. Turning
  that claim into a verified fact is the next feature.
- **Windsurf, Cursor and Copilot CLI are unsupported.** Four solid adapters beat
  eight broken ones; every adapter is permanent maintenance against a moving target.

## Contributing

The most useful contribution is a `--probe` output that reports unknown shapes, or a
real session file from a tool that misbehaves. Every bug worth fixing in this project
so far came from a real transcript, not from reasoning.

```bash
git clone https://github.com/hemingweb006/agentswap
cd agentswap
pip install -e '.[dev]'
pytest -q
```

Adding a vendor means one module under `readers/` exposing `read(path) -> Session`,
plus an entry in `agents.py`. Nothing else should need to change — and if it does,
that is the bug.

## License

MIT
