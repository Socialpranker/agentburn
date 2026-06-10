"""Terminal + JSON rendering. No deps, ANSI-safe, honest footers."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass

from .analyze import Analysis


def fmt_tokens(n: float) -> str:
    if n is None:
        return "–"
    for suffix, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            v = n / div
            s = f"{v:.0f}" if v >= 100 else f"{v:.1f}" if v >= 10 else f"{v:.2f}"
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s + suffix
    return str(int(n))


def fmt_money(x, basis: str = "") -> str:
    if x is None:
        return "–"
    tag = "~" if basis in ("estimated", "mixed") else ""
    return f"{tag}${x:,.2f}"


class P:
    def __init__(self, color: bool):
        self.c = color

    def b(self, s):  # bold
        return f"\033[1m{s}\033[0m" if self.c else s

    def dim(self, s):
        return f"\033[2m{s}\033[0m" if self.c else s

    def red(self, s):
        return f"\033[31m{s}\033[0m" if self.c else s

    def yellow(self, s):
        return f"\033[33m{s}\033[0m" if self.c else s

    def green(self, s):
        return f"\033[32m{s}\033[0m" if self.c else s

    def cyan(self, s):
        return f"\033[36m{s}\033[0m" if self.c else s


def _bar(share: float, width: int = 18) -> str:
    n = max(0, min(width, round(share * width)))
    return "█" * n + "·" * (width - n)


def _tldr(a: Analysis, recs: list) -> list:
    """Two plain sentences a hurried human actually reads."""
    if a.total.tokens == 0:
        return []
    bits = []
    if a.monthly_projection is not None:
        bits.append(f"≈{fmt_money(a.monthly_projection, a.cost_basis)}/mo pace")
    else:
        bits.append(f"{fmt_tokens(a.total.tokens)} tokens in the window")
    cost_total = a.total.cost or 0.0
    if a.by_source:
        src, b = next(iter(a.by_source.items()))
        share = (b.cost / cost_total) if cost_total > 0 else (
            b.tokens / a.total.tokens if a.total.tokens else 0
        )
        if share >= 0.35:
            bits.append(f"{share:.0%} of it is `{src}`")
    line1 = "; ".join(bits) + "."
    lines = [line1]
    if recs:
        first = recs[0].split(" — ")[0].split(". ")[0].strip()
        lines.append(f"First fix: {first[:130]}{'…' if len(first) > 130 else ''}")
    return lines


def render_terminal(a: Analysis, recs: list, color: bool = True) -> str:
    p = P(color)
    out = []
    basis = a.cost_basis
    days_str = f"last {a.days}d" if a.days else "all time"

    out.append("")
    out.append(p.b(f"🔥 agentburn — {a.agent} · {days_str}"))
    out.append(p.dim(f"   {a.source_path}"))
    out.append("")
    tldr = _tldr(a, recs)
    if tldr:
        out.append("   " + p.b("TL;DR: ") + tldr[0])
        for extra in tldr[1:]:
            out.append("   " + p.yellow(extra))
        out.append("")
    if a.total.tokens == 0:
        out.append(p.yellow(
            "   Nothing recorded in this window. Try `--days 0` (all time), "
            "or `agentburn doctor` to check the agent's accounting."
        ))
        out.append("")
    out.append(
        f"   {p.b(fmt_money(a.total.cost if a.total.cost_known else None, basis))} total"
        f" · {fmt_tokens(a.total.tokens)} tokens · {a.total.sessions} sessions"
        f" · {a.total.api_calls} API calls"
    )
    if a.monthly_projection is not None:
        out.append(
            f"   {p.yellow('≈ ' + fmt_money(a.monthly_projection, basis) + '/month')} at the current pace"
        )
    out.append("")

    cost_total = a.total.cost or 0.0
    if a.by_source:
        out.append(p.b("   WHERE IT BURNS"))
        out.append(p.dim("   which part of your setup spends the money — scheduled jobs, messenger gateways, subagents or you"))
        for src, b in list(a.by_source.items())[:8]:
            share = (b.cost / cost_total) if cost_total > 0 else (b.tokens / a.total.tokens if a.total.tokens else 0)
            out.append(
                f"   {src:<20} {_bar(share)} {share:>4.0%}  "
                f"{fmt_money(b.cost if b.cost_known else None, basis):>10}  "
                f"{fmt_tokens(b.tokens):>7}  {b.sessions} sess"
            )
        out.append("")

    if a.night.sessions > 0:
        share = (a.night.cost / cost_total) if cost_total > 0 else 0
        line = (
            f"   {p.b('🌙 WHILE YOU SLEPT')} ({a.night_window[0]:02d}:00–{a.night_window[1]:02d}:00): "
            f"{fmt_money(a.night.cost if a.night.cost_known else None, basis)}"
            f" ({share:.0%} of spend) · {a.night.sessions} sessions"
        )
        out.append(p.red(line) if share >= 0.25 else line)
        top = next(iter(a.night_by_source), None)
        if top:
            out.append(p.dim(f"      mostly: {top}"))
        out.append("")

    if a.by_model:
        out.append(p.b("   MODELS"))
        for m, b in list(a.by_model.items())[:5]:
            out.append(
                f"   {(m or 'unknown')[:36]:<38} {fmt_money(b.cost if b.cost_known else None, basis):>10}  "
                f"{fmt_tokens(b.tokens):>7}"
            )
        out.append("")

    if a.tools:
        out.append(p.b("   TOP TOOLS"))
        out.append(p.dim("   whose results weigh most in your context — every later request re-pays for them"))
        for t in a.tools[:6]:
            out.append(f"   {t.name[:36]:<38} {fmt_tokens(t.result_tokens):>7}  {t.calls} calls")
        out.append("")

    if a.rollups:
        out.append(p.b("   SUBAGENT ROLLUPS"))
        out.append(p.dim("   what delegation actually cost, traced back to the session that started it"))
        for r in a.rollups:
            out.append(
                f"   {r.title[:42]:<44} +{fmt_money(r.sub_cost, basis)} in {r.sub_sessions} subagent(s)"
            )
        out.append("")

    if a.overhead_per_call:
        from .benchmarks import overhead_vs_reference

        out.append(p.b("   FIXED OVERHEAD — avg input tokens per API call"))
        out.append(p.dim("   the silent tax: tool definitions + system prompt re-sent with every single request"))
        ranked = sorted(a.overhead_per_call.items(), key=lambda kv: kv[1], reverse=True)
        for i, (src, v) in enumerate(ranked[:5]):
            flag = p.red(" ← heavy") if v >= 12000 else ""
            ref = p.dim(f"   {overhead_vs_reference(v)}") if i == 0 and v > 0 else ""
            out.append(f"   {src:<20} {v:>8,}{flag}{ref}")
        if a.composition:
            c = a.composition
            out.append(
                p.dim(
                    f"      input composition (sampled from {c.samples} request dumps): "
                    f"system {c.system_share:.0%} · tools {c.tools_share:.0%} · history {c.history_share:.0%}"
                )
            )
        out.append("")

    if recs:
        out.append(p.b("   💡 DO THIS"))
        for i, r in enumerate(recs, 1):
            out.append(f"   {i}. {r}")
        out.append("")

    for w in a.warnings:
        out.append(p.yellow(f"   ⚠ {w}"))
    out.append(
        p.dim(
            "   Methodology: numbers come from the agent's own local accounting "
            f"({'provider-billed' if basis == 'actual' else basis} costs); nothing leaves this machine."
        )
    )
    out.append("")
    return "\n".join(out)


def render_json(a: Analysis, recs: list) -> str:
    def enc(o):
        if is_dataclass(o):
            return asdict(o)
        return str(o)

    payload = {
        "agentburn": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(a.period_end)),
        "agent": a.agent,
        "days": a.days,
        "cost_basis": a.cost_basis,
        "total": asdict(a.total),
        "monthly_projection": a.monthly_projection,
        "by_source": {k: asdict(v) for k, v in a.by_source.items()},
        "by_model": {k: asdict(v) for k, v in a.by_model.items()},
        "night": {"window": list(a.night_window), **asdict(a.night)},
        "tools": [asdict(t) for t in a.tools],
        "subagent_rollups": [asdict(r) for r in a.rollups],
        "overhead_per_call": a.overhead_per_call,
        "composition": asdict(a.composition) if a.composition else None,
        "zero_token_sessions": a.zero_token_sessions,
        "recommendations": recs,
        "warnings": a.warnings,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=enc)
