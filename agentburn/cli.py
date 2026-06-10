"""agentburn CLI.

Usage:
  agentburn                       # profile, last 30 days
  agentburn --days 7 --night 23-7
  agentburn --share               # anonymized burn card for posting
  agentburn --share --svg card.svg
  agentburn --save-baseline       # snapshot before you optimize…
  agentburn --compare             # …then prove the saving
  agentburn doctor                # diagnose accounting gaps + upstream bug report
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="agentburn",
        description="Where does your AI agent burn money? Local profiler, zero deps, nothing leaves your machine.",
    )
    ap.add_argument("command", nargs="?", choices=["report", "doctor"], default="report",
                    help="report (default) or doctor (accounting health + upstream bug report)")
    ap.add_argument("--agent", default=None, choices=sorted(ADAPTERS), help="adapter to use (default: autodetect)")
    ap.add_argument("--db", default=None, help="path to the agent state database")
    ap.add_argument("--days", type=int, default=30, help="analysis window in days (default 30; 0 = all time)")
    ap.add_argument("--night", type=parse_night, default=(0, 8), help="'while you slept' window, e.g. 0-8 (local)")
    ap.add_argument("--dumps-dir", default=None, help="directory with request_dump_*.json for input composition")
    ap.add_argument("--share", action="store_true", help="print an anonymized burn card (safe to post)")
    ap.add_argument("--svg", default=None, metavar="FILE", help="with --share: also write an SVG card")
    ap.add_argument("--save-baseline", action="store_true", help="save current pace as the optimization baseline")
    ap.add_argument("--compare", action="store_true", help="compare current pace against the saved baseline")
    ap.add_argument("--baseline-file", default=None, help="override baseline location (default ~/.agentburn/baseline.json)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--version", action="version", version=f"agentburn {__version__}")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    name = args.agent
    if name is None:
        if args.db:
            name = "hermes"  # explicit db implies the (currently single) schema
        else:
            found = detect()
            if not found:
                print(
                    "agentburn: no supported agent data found on this machine.\n"
                    "  looked for: ~/.hermes/state.db (Hermes Agent)\n"
                    "  pass --db /path/to/state.db if it lives elsewhere.\n"
                    "  adapters for OpenClaw and Claude Code are on the roadmap.",
                    file=sys.stderr,
                )
                return 2
            name = found[0]

    adapter = ADAPTERS[name]
    try:
        snap = adapter.load(db_path=args.db, days=args.days or None, dumps_dir=args.dumps_dir)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"agentburn: {e}", file=sys.stderr)
        return 2

    color = sys.stdout.isatty() and not args.no_color

    if args.command == "doctor":
        from .doctor import render_doctor

        print(render_doctor(snap, color=color))
        return 0

    a = analyze(snap, night_window=args.night)

    if args.save_baseline:
        from . import baseline

        path = baseline.save(a, args.baseline_file or baseline.DEFAULT_PATH)
        print(f"agentburn: baseline saved → {path}\n"
              "  optimize your config, then run `agentburn --compare` to prove the saving.")
        return 0

    if args.compare:
        from . import baseline

        try:
            base = baseline.load(args.baseline_file or baseline.DEFAULT_PATH)
        except FileNotFoundError:
            print("agentburn: no baseline found — run `agentburn --save-baseline` first.", file=sys.stderr)
            return 2
        print(baseline.render_compare(a, base))
        return 0

    recs = recommend(a)

    if args.share:
        from .share import share_svg, share_text

        print(share_text(a))
        if args.svg:
            with open(args.svg, "w", encoding="utf-8") as f:
                f.write(share_svg(a))
            print(f"\nSVG card → {args.svg}", file=sys.stderr)
        return 0

    if args.json:
        print(render_json(a, recs))
    else:
        print(render_terminal(a, recs, color=color))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
