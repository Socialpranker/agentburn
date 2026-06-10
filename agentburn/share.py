"""Shareable burn card: anonymized by construction.

Includes ONLY: period, totals, source categories, model names, the overnight
line and overhead calibration. Never includes session titles, paths, user ids
or message content — safe to paste into a public post.
"""

from __future__ import annotations

from .analyze import Analysis
from .benchmarks import overhead_vs_reference_short
from .report import fmt_money, fmt_tokens

REPO = "github.com/Socialpranker/agentburn"


def share_text(a: Analysis) -> str:
    """One clear thought per line, no nested parentheses, no jargon."""
    basis = a.cost_basis
    days = f"last {a.days}d" if a.days else "all time"
    lines = [f"🔥 my {a.agent} agent · {days}"]

    total = fmt_money(a.total.cost if a.total.cost_known else None, basis)
    pace = (
        f" → {fmt_money(a.monthly_projection, basis)}/mo pace"
        if a.monthly_projection is not None
        else ""
    )
    lines.append(f"{total}{pace} · {fmt_tokens(a.total.tokens)} tokens")

    cost_total = a.total.cost or 0.0
    if cost_total > 0 and a.by_source:
        shares = [
            f"{src.replace('gateway:', '')} {b.cost / cost_total:.0%}"
            for src, b in list(a.by_source.items())[:4]
            if b.cost > 0
        ]
        if shares:
            lines.append("where it burns: " + " · ".join(shares))

    if a.night.sessions > 0 and cost_total > 0:
        share = a.night.cost / cost_total
        lines.append(
            f"🌙 while I slept ({a.night_window[0]:02d}–{a.night_window[1]:02d}): "
            f"{fmt_money(a.night.cost, basis)} — {share:.0%} of everything"
        )

    if a.overhead_per_call:
        src, v = max(a.overhead_per_call.items(), key=lambda kv: kv[1])
        if v > 0:
            ref = overhead_vs_reference_short(v)
            lines.append(
                f"⚙️ {src.replace('gateway:', '')} re-sends {v:,} tokens with EVERY call"
                + (f" — {ref}" if ref else "")
            )

    top_model = next(iter(a.by_model), None)
    if top_model and top_model != "unknown":
        lines.append(f"top model: {top_model}")

    lines.append(f"— agentburn · local & private · {REPO}")
    return "\n".join(lines)


def share_svg(a: Analysis, width: int = 640, height: int = 340) -> str:
    """Dark share card (X/OG friendly). Same anonymity rules as share_text."""
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    basis = a.cost_basis
    cost_total = a.total.cost or 0.0
    days = f"last {a.days}d" if a.days else "all time"
    pal = ["#f7775a", "#5ab0f7", "#7df0a8", "#f5d76e", "#c89bf7"]

    rows = []
    y = 150
    for i, (src, b) in enumerate(list(a.by_source.items())[:4]):
        if cost_total <= 0:
            break
        share = b.cost / cost_total
        bar_w = int(360 * share)
        label = src.replace("gateway:", "")
        rows.append(
            f'<text x="48" y="{y + 13}" font-size="14" fill="#e6e9ec" font-family="{F}">{esc(label)}</text>'
            f'<rect x="170" y="{y}" width="{max(2, bar_w)}" height="16" rx="3" fill="{pal[i % len(pal)]}"/>'
            f'<text x="{178 + max(2, bar_w)}" y="{y + 13}" font-size="13" fill="#8a949e" font-family="{F}">'
            f"{share:.0%} · {esc(fmt_money(b.cost, basis))}</text>"
        )
        y += 28

    night = ""
    if a.night.sessions > 0 and cost_total > 0:
        night = (
            f'<text x="48" y="{y + 22}" font-size="15" fill="#f5d76e" font-family="{F}">'
            f"🌙 while I slept ({a.night_window[0]:02d}–{a.night_window[1]:02d}): "
            f"{esc(fmt_money(a.night.cost, basis))} ({a.night.cost / cost_total:.0%})</text>"
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="{width}" height="{height}" rx="14" fill="#0b0d10"/>
<text x="48" y="56" font-size="20" font-weight="700" fill="#e6e9ec" font-family="{F}">🔥 where my {esc(a.agent)} agent burns money</text>
<text x="48" y="80" font-size="13" fill="#8a949e" font-family="{F}">{esc(days)}</text>
<text x="48" y="124" font-size="32" font-weight="700" fill="#f7775a" font-family="{F}">{esc(fmt_money(a.total.cost if a.total.cost_known else None, basis))}
<tspan font-size="16" fill="#8a949e"> {esc('→ ' + fmt_money(a.monthly_projection, basis) + '/mo pace' if a.monthly_projection is not None else '')}</tspan></text>
{''.join(rows)}
{night}
<text x="48" y="{height - 22}" font-size="12" fill="#8a949e" font-family="{F}">profiled locally · {REPO} · nothing left my machine</text>
</svg>
"""


F = "system-ui,-apple-system,Segoe UI,sans-serif"
