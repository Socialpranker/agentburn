"""agentburn CLI.

Usage:
  agentburn                       # profile every agent found on this machine
  agentburn --agent openclaw      # just one
  agentburn --days 7 --night 23-7
  agentburn --share               # anonymized burn card for posting
  agentburn --share --svg card.svg
  agentburn --save-baseline       # snapshot before you optimize…
  agentburn --compare             # …then prove the saving
  agentburn doctor                # diagnose accounting gaps + upstream bug report
  agentburn --budget-night 5 --fail-over   # sentinel: exit 1 when breached (for cron/CI)
  agentburn --json
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .adapters import ADAPTERS, detect
from .analyze import analyze
from .recommend import recommend
from .report import fmt_money, render_json, render_terminal


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
    ap.add_argument("command", nargs="?", choices=["report", "doctor", "why"], default="report",
                    help="report (default) · why (behavioral forensics: loops, storms, idle heartbeats, "
                         "failure burn) · doctor (accounting health)")
    ap.add_argument("--agent", default=None, choices=sorted(ADAPTERS),
                    help="profile one agent (default: every agent detected on this machine)")
    ap.add_argument("--db", default=None, help="explicit path to the agent's data (requires --agent)")
    ap.add_argument("--days", type=int, default=30, help="analysis window in days (default 30; 0 = all time)")
    ap.add_argument("--night", type=parse_night, default=(0, 8), help="'while you slept' window, e.g. 0-8 (local)")
    ap.add_argument("--dumps-dir", default=None, help="hermes: directory with request_dump_*.json")
    ap.add_argument("--share", action="store_true", help="print an anonymized burn card (safe to post)")
    ap.add_argument("--svg", default=None, metavar="FILE", help="with --share: also write an SVG card")
    ap.add_argument("--save-baseline", action="store_true", help="save current pace as the optimization baseline")
    ap.add_argument("--compare", action="store_true", help="compare current pace against the saved baseline")
    ap.add_argument("--baseline-file", default=None, help="override baseline location (default ~/.agentburn/baseline.json)")
    ap.add_argument("--budget-month", type=float, default=None, metavar="USD",
                    help="sentinel: warn when the monthly pace exceeds this")
    ap.add_argument("--budget-night", type=float, default=None, metavar="USD",
                    help="sentinel: warn when the overnight share exceeds this per month")
    ap.add_argument("--fail-over", action="store_true",
                    help="exit with code 1 when a budget is breached (for cron/CI alerts)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--version", action="version", version=f"agentburn {__version__}")
    return ap


def pick_agents(args) -> list:
    if args.agent:
        return [args.agent]
    if args.db:
        print("agentburn: --db needs --agent to know which schema to read "
              f"(one of: {', '.join(sorted(ADAPTERS))}).", file=sys.stderr)
        raise SystemExit(2)
    found = detect()
    if not found:
        print(
            "agentburn: no supported agent data found on this machine.\n"
            "  looked for: ~/.hermes/state.db (Hermes Agent)\n"
            "              ~/.openclaw/agents/*/sessions/sessions.json (OpenClaw)\n"
            "              ~/.claude/projects/*.jsonl (Claude Code)\n"
            "  pass --agent <name> --db <path> if the data lives elsewhere.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return found


def need_single(found: list, what: str) -> str:
    if len(found) > 1:
        print(
            f"agentburn: {len(found)} agents detected ({', '.join(found)}) — "
            f"pass --agent <name> for {what}.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return found[0]


def sentinel_breaches(a, args) -> list:
    out = []
    monthly = a.monthly_projection
    if args.budget_month is not None and monthly is not None and monthly > args.budget_month:
        out.append(
            f"[{a.agent}] monthly pace {fmt_money(monthly, a.cost_basis)} exceeds budget "
            f"{fmt_money(args.budget_month)}"
        )
    if args.budget_night is not None and monthly is not None and (a.total.cost or 0) > 0:
        night_monthly = monthly * (a.night.cost / a.total.cost)
        if night_monthly > args.budget_night:
            out.append(
                f"[{a.agent}] overnight pace {fmt_money(night_monthly, a.cost_basis)}/mo exceeds budget "
                f"{fmt_money(args.budget_night)} ({a.night_window[0]:02d}:00–{a.night_window[1]:02d}:00)"
            )
    return out


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    color = sys.stdout.isatty() and not args.no_color

    single_modes = args.command == "doctor" or args.share or args.save_baseline or args.compare
    # `why`, like `report`, runs across every detected agent
    found = pick_agents(args)
    if single_modes:
        found = [need_single(found, "this mode")]

    def load(name):
        return ADAPTERS[name].load(db_path=args.db, days=args.days or None, dumps_dir=args.dumps_dir)

    try:
        if args.command == "doctor":
            from .doctor import render_doctor

            print(render_doctor(load(found[0]), color=color))
            return 0

        if args.command == "why":
            from .behavior import analyze_behavior, render_behavior

            for n in found:
                print(render_behavior(analyze_behavior(load(n)), color=color))
            return 0

        analyses = [analyze(load(n), night_window=args.night) for n in found]
    except (FileNotFoundError, RuntimeError) as e:
        print(f"agentburn: {e}", file=sys.stderr)
        return 2

    if args.save_baseline:
        from . import baseline

        path = baseline.save(analyses[0], args.baseline_file or baseline.DEFAULT_PATH)
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
        print(baseline.render_compare(analyses[0], base))
        return 0

    if args.share:
        from .share import share_svg, share_text

        print(share_text(analyses[0]))
        if args.svg:
            with open(args.svg, "w", encoding="utf-8") as f:
                f.write(share_svg(analyses[0]))
            print(f"\nSVG card → {args.svg}", file=sys.stderr)
        return 0

    breaches = []
    if args.json:
        import json as _json

        payloads = [_json.loads(render_json(a, recommend(a))) for a in analyses]
        print(_json.dumps(payloads[0] if len(payloads) == 1 else payloads, indent=2, ensure_ascii=False))
        for a in analyses:
            breaches += sentinel_breaches(a, args)
    else:
        for a in analyses:
            print(render_terminal(a, recommend(a), color=color))
            b = sentinel_breaches(a, args)
            for line in b:
                print(("\033[31m" if color else "") + f"   🚨 {line}" + ("\033[0m" if color else ""))
            if b:
                print()
            breaches += b

    if breaches and args.fail_over:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
