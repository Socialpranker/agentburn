"""agentburn CLI.

Usage:
  agentburn                 # profile Hermes Agent, last 30 days
  agentburn --days 7
  agentburn --db /path/to/state.db
  agentburn --night 23-7    # custom "while you slept" window
  agentburn --json
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .adapters import ADAPTERS, detect
from .analyze import analyze
from .recommend import recommend
from .report import render_json, render_terminal


def parse_night(s: str) -> tuple:
    try:
        a, b = s.split("-")
        a, b = int(a), int(b)
        if not (0 <= a <= 23 and 0 <= b <= 24):
            raise ValueError
        return (a, b)
    except ValueError:
        raise argparse.ArgumentTypeError("night window must look like 0-8 or 23-7")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="agentburn",
        description="Where does your AI agent burn money? Local profiler, zero deps, nothing leaves your machine.",
    )
    ap.add_argument("--agent", default=None, choices=sorted(ADAPTERS), help="adapter to use (default: autodetect)")
    ap.add_argument("--db", default=None, help="path to the agent state database")
    ap.add_argument("--days", type=int, default=30, help="analysis window in days (default 30; 0 = all time)")
    ap.add_argument("--night", type=parse_night, default=(0, 8), help="'while you slept' window, e.g. 0-8 (local time)")
    ap.add_argument("--dumps-dir", default=None, help="directory with request_dump_*.json for input composition")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--version", action="version", version=f"agentburn {__version__}")
    args = ap.parse_args(argv)

    name = args.agent
    if name is None:
        found = detect()
        if args.db:
            name = "hermes"  # explicit db implies the (currently single) schema
        elif not found:
            print(
                "agentburn: no supported agent data found on this machine.\n"
                "  looked for: ~/.hermes/state.db (Hermes Agent)\n"
                "  pass --db /path/to/state.db if it lives elsewhere.\n"
                "  adapters for OpenClaw and Claude Code are on the roadmap.",
                file=sys.stderr,
            )
            return 2
        else:
            name = found[0]

    adapter = ADAPTERS[name]
    try:
        snap = adapter.load(db_path=args.db, days=args.days or None, dumps_dir=args.dumps_dir)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"agentburn: {e}", file=sys.stderr)
        return 2

    a = analyze(snap, night_window=args.night)
    recs = recommend(a)
    if args.json:
        print(render_json(a, recs))
    else:
        color = sys.stdout.isatty() and not args.no_color
        print(render_terminal(a, recs, color=color))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
