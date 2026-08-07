"""Command line entry point.

    python -m agentswap.cli list
    python -m agentswap.cli inspect <session.jsonl>
    python -m agentswap.cli inspect <session.jsonl> --turns
    python -m agentswap.cli inspect <session.jsonl> --probe
    python -m agentswap.cli inspect <session.jsonl> --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import handoff, readers, writers

DIM_HINT = "\033[2m" if sys.stdout.isatty() else ""


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:,.0f}{unit}" if unit == "B" else f"{n:,.1f}{unit}"
        n /= 1024
    return f"{n:,.1f}TB"


def _locate(reference: str) -> Path | None:
    """Resolve a session reference, reporting ambiguity rather than guessing."""
    matches = readers.find(reference)
    if not matches:
        print(f"no session found matching: {reference}", file=sys.stderr)
        print("try `agentswap list` to see what is available", file=sys.stderr)
        return None
    if len(matches) > 1:
        print(f"{len(matches)} sessions match {reference!r}; using the most recent:",
              file=sys.stderr)
        for path in matches[:5]:
            marker = "->" if path is matches[0] else "  "
            print(f"  {marker} {readers.session_id_of(path)}  {path}", file=sys.stderr)
        print(file=sys.stderr)
    return matches[0]


def cmd_inspect(args: argparse.Namespace) -> int:
    path = _locate(args.path)
    if path is None:
        return 1

    try:
        session = readers.read(path, source=args.source)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(session.to_json())
        return 0

    st = session.stats
    print(f"source        {session.source} {session.agent_version}".rstrip())
    print(f"session       {session.session_id or '(none recorded)'}")
    print(f"cwd           {session.cwd or '(none recorded)'}")
    if session.git_branch:
        print(f"branch        {session.git_branch}")
    if st.get("model"):
        print(f"model         {st['model']}")
    print(f"span          {session.started}  ->  {session.ended}")
    print()
    print(f"file          {_human(st['file_bytes'])} across {st['records']} records")
    print(f"turns         {st['turns']} ({st['user_turns']} from you)")
    print(f"semantic      {_human(st['semantic_bytes'])}")

    comp = st.get("compression")
    if comp:
        pct = 100 * st["semantic_bytes"] / st["file_bytes"]
        print(f"ratio         {comp}x smaller  ({pct:.1f}% of the file carries meaning)")
    if st.get("dropped_media"):
        print(f"media         {st['dropped_media']} attachments dropped")
    if st.get("compactions"):
        print(f"compactions   {st['compactions']} (context was summarized mid-session)")
    for goal in st.get("goals", []):
        print(f"goal          {goal[:100]}")

    if st.get("tools"):
        print()
        print("tools used")
        for name, count in st["tools"].items():
            print(f"  {count:>4}  {name}")

    if args.probe:
        print()
        print("record kinds")
        source_kinds = st.get("item_kinds") or st.get("non_message_records") or {}
        for name, count in source_kinds.items():
            print(f"  {count:>4}  {name}")
        unknown = st.get("unrecognized") or {}
        print()
        if unknown:
            print("UNRECOGNIZED shapes -- the reader skipped these:")
            for name, count in unknown.items():
                print(f"  {count:>4}  {name}")
            print()
            print("  Paste this list back and the adapter can be taught them.")
        else:
            print("unrecognized  none -- every record shape was understood")

    if args.turns:
        print()
        print("transcript")
        for turn in session.turns:
            tag = {"user": "you ", "assistant": "ai  ", "tool": "    "}.get(turn.role, turn.role[:4])
            if turn.sidechain:
                tag = "sub "
            if turn.text:
                print(f"  [{turn.index:>3}] {tag} {turn.text[:110]}".replace("\n", " "))
            for call in turn.tool_calls:
                print(f"  [{turn.index:>3}] {tag}   -> {call.summary[:104]}")
            for res in turn.tool_results:
                flag = "!" if res.is_error else " "
                label = res.name or "result"
                print(f"  [{turn.index:>3}]     {flag}<- {label} ({_human(res.raw_bytes)}) {res.preview[:70]}")

    return 0


def cmd_port(args: argparse.Namespace) -> int:
    path = _locate(args.path)
    if path is None:
        return 1

    try:
        session = readers.read(path, source=args.source)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    brief = handoff.build(session)
    if args.cwd:
        brief.cwd = args.cwd

    if args.show:
        print(brief.render())
        return 0

    from .agents import SPECS

    spec = SPECS.get(args.to)
    original = session.stats.get("file_bytes", 0)

    def summarize(ported: int) -> None:
        print()
        print(f"source        {_human(original)}")
        print(f"handoff       {_human(ported)}")
        if ported:
            print(f"reduction     {original / ported:,.0f}x")
        if brief.files_touched:
            print(f"carrying      {len(brief.files_touched)} changed file(s)")
        if brief.failures:
            print(f"              {len(brief.failures)} recorded failure(s)")
        if brief.notes:
            print(f"caveats       {len(brief.notes)}")

    # Tools that resolve sessions through a private index -- or store them in
    # a format we cannot write -- cannot be handed a file. The brief arrives as
    # an opening prompt instead. Anything without a writer also lands here.
    if spec is None or spec.delivery == "prompt" or args.to not in writers.WRITERS:
        out_dir = Path(brief.cwd or os.getcwd()) / ".agentswap"
        out_file = out_dir / f"handoff-{session.session_id or 'session'}.md"
        detail = out_dir / "handoff.md"
        text = brief.render(compact=True, detail_path=str(detail)) + handoff.CONFIRM_SUFFIX

        print(f"from          {session.source} {path.name}")
        print(f"to            {args.to}  (prompt delivery)")
        if not args.dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_file.write_text(text, encoding="utf-8")
            detail.write_text(brief.render(compact=False), encoding="utf-8")
            print(f"wrote         {out_file}")
        summarize(len(text))
        print()
        binary = spec.binary if spec else args.to
        reason = (
            f"{args.to} resolves session ids through its own index"
            if spec and spec.key == "codex"
            else f"{args.to} stores conversations in a format agentswap cannot write"
        )
        print(f"{reason}, so a")
        print("written session file would never be found. Start it with the brief instead:")
        print()
        print(f"  cd {brief.cwd!r} && {binary} \"$(cat {out_file})\"")
        print()
        print("or let agentswap do it for you:  agentswap run")
        return 0

    writer = writers.WRITERS[args.to]
    root = Path(args.root).expanduser() if args.root else None
    try:
        target, session_id, records = writer.write(brief, root=root, dry_run=args.dry_run)
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    ported = sum(len(json.dumps(r, ensure_ascii=False)) + 1 for r in records)
    verb = "would write" if args.dry_run else "wrote"
    print(f"from          {session.source} {path.name}")
    print(f"to            {args.to}")
    print(f"{verb:<13} {target}")
    print(f"session       {session_id}")
    summarize(ported)

    print()
    if args.dry_run:
        print("dry run -- nothing was written. Re-run without --dry-run to commit,")
        print("or use --show to read the brief itself.")
    else:
        print("resume with:")
        print(f"  {writer.resume_command(session_id, brief.cwd)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from . import session_loop

    cwd = str(Path(args.cwd).expanduser().resolve()) if args.cwd else os.getcwd()
    return session_loop.run(cwd, start=args.with_agent, auto=args.auto)


def cmd_agents(args: argparse.Namespace) -> int:
    from . import agents as agent_mod, ui

    print()
    hidden: list[tuple] = []
    for spec in agent_mod.SPECS.values():
        path, on_path = spec.locate()
        root = spec.session_root()
        seen = len(list(root.glob(spec.session_glob))) if root.exists() else 0

        if path and on_path:
            dot, state = f"{ui.OK}●{ui.RESET}", f"{ui.OK}ready{ui.RESET}"
        elif path:
            dot, state = f"{ui.ALERT}◐{ui.RESET}", f"{ui.ALERT}not on PATH{ui.RESET}"
            hidden.append((spec, path))
        else:
            dot, state = f"{ui.GREY}○{ui.RESET}", f"{ui.GREY}not installed{ui.RESET}"

        route = "session file" if spec.delivery == "session" else "opening prompt"
        if not spec.source:
            route = f"{ui.GREY}read not supported{ui.RESET}"

        print(f"  {dot} {ui.BOLD}{spec.label:<14}{ui.RESET}{ui.GREY}{spec.binary:<8}{ui.RESET}"
              f"{state:<24} {seen:>4} session(s)   {ui.GREY}{route}{ui.RESET}")

        if not path and spec.install_hint:
            print(f"      {ui.GREY}install: {spec.install_hint}{ui.RESET}")

    for spec, path in hidden:
        print()
        print(f"  {ui.ALERT}{spec.label} is installed at {path} but not on your PATH.{ui.RESET}")
        print(f"  {ui.GREY}Add it for this shell:{ui.RESET}")
        print(f"      export PATH=\"{path.parent}:$PATH\"")
        print(f"  {ui.GREY}Or permanently, append that line to ~/.zshrc{ui.RESET}")
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    found = readers.discover()
    if not found:
        roots = ", ".join(str(m.default_root()) for m in readers.READERS.values())
        print(f"no transcripts found under: {roots}")
        return 0

    here = os.getcwd()
    rows = []
    for source, path in found:
        if args.here:
            try:
                if readers.read(path).cwd != here:
                    continue
            except (ValueError, RuntimeError, OSError):
                continue
        rows.append((source, path))
        if len(rows) >= args.limit:
            break

    if not rows:
        print(f"no sessions recorded for {here}")
        return 0

    # The id is the thing you paste into `port` or `inspect`, so lead with it.
    for source, path in rows:
        session_id = readers.session_id_of(path)
        size = _human(path.stat().st_size)
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"{session_id}  {source:<12} {when}  {size:>9}")
        if args.verbose:
            try:
                session = readers.read(path)
                # Go through handoff rather than reading turn 0 directly --
                # otherwise every Codex session previews as its AGENTS.md
                # preamble instead of what the user actually asked for.
                goal = handoff.build(session).goal.replace("\n", " ")[:72]
                print(f"{'':<38}{session.cwd or ''}")
                if goal:
                    print(f"{'':<38}{DIM_HINT}{goal}")
            except (ValueError, RuntimeError, OSError):
                pass

    remaining = len(found) - len(rows)
    if remaining > 0 and not args.here:
        print(f"... and {remaining} more")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentswap")
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="show what is inside a transcript")
    p_inspect.add_argument("path")
    p_inspect.add_argument("--source", choices=sorted(readers.READERS), default=None)
    p_inspect.add_argument("--turns", action="store_true", help="print the turn list")
    p_inspect.add_argument("--probe", action="store_true", help="report record shapes, including unknown ones")
    p_inspect.add_argument("--json", action="store_true", help="emit the IR as JSON")
    p_inspect.set_defaults(func=cmd_inspect)

    p_run = sub.add_parser("run", help="start an agent and carry the session when you switch")
    p_run.add_argument("--with", dest="with_agent", default=None,
                       help="skip the picker and start this agent")
    p_run.add_argument("--cwd", default=None, help="project directory (defaults to here)")
    p_run.add_argument("--auto", action="store_true",
                       help="let the new agent continue immediately instead of confirming first")
    p_run.set_defaults(func=cmd_run)

    p_agents = sub.add_parser("agents", help="show which agent CLIs are available")
    p_agents.set_defaults(func=cmd_agents)

    p_port = sub.add_parser("port", help="carry a session into another tool")
    p_port.add_argument("path")
    from . import agents as _agents

    p_port.add_argument("--to", required=True, choices=_agents.targets())
    p_port.add_argument("--source", choices=sorted(readers.READERS), default=None)
    p_port.add_argument("--dry-run", action="store_true", help="show the plan without writing")
    p_port.add_argument("--show", action="store_true", help="print the brief and exit")
    p_port.add_argument("--cwd", default=None, help="override the target working directory")
    p_port.add_argument("--root", default=None, help="write under this root instead of the real one")
    p_port.set_defaults(func=cmd_port)

    p_list = sub.add_parser("list", help="find local sessions from every known tool")
    p_list.add_argument("--limit", type=int, default=25)
    p_list.add_argument("--here", action="store_true", help="only sessions for this directory")
    p_list.add_argument("-v", "--verbose", action="store_true",
                        help="show the project and opening request for each")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
