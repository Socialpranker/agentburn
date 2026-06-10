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


F = "system-ui,-apple-system,Segoe UI,sans-serif"

SOURCE_COLORS = (
    (("cron", "heartbeat"), "#f7775a"),
    (("cli",), "#5ab0f7"),
    (("gateway",), "#f5d76e"),
    (("subagent",), "#c89bf7"),
)


def _source_color(src: str) -> str:
    for prefixes, color in SOURCE_COLORS:
        if any(src.startswith(p) for p in prefixes):
            return color
    return "#7df0a8"


def share_svg(a: Analysis, width: int = 640) -> str:
    """Dark share card (X/OG friendly). Same anonymity rules as share_text.

    Design: brand row → big cost + pace → 'where it burns' bars (color = source
    meaning: hot for scheduled, blue for you, yellow for gateways, purple for
    subagents) → night callout strip → overhead line → privacy footer.
    """
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    basis = a.cost_basis
    cost_total = a.total.cost or 0.0
    days = f"last {a.days} days" if a.days else "all time"
    t = lambda x, y, size, fill, s, anchor="start", weight=None: (
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" font-family="{F}"'
        + (f' font-weight="{weight}"' if weight else "")
        + (f' text-anchor="{anchor}"' if anchor != "start" else "")
        + f">{s}</text>"
    )

    parts = []
    parts.append(t(28, 42, 13, "#8a949e", "agentburn", weight="500"))
    parts.append(t(612, 42, 13, "#5c6670", f"{esc(a.agent)} · {esc(days)}", anchor="end"))

    total = fmt_money(a.total.cost if a.total.cost_known else None, basis)
    if not a.total.cost_known:
        total = fmt_tokens(a.total.tokens) + " tokens"
    parts.append(t(28, 104, 42, "#e6e9ec", esc(total), weight="500"))
    if a.monthly_projection is not None:
        parts.append(
            t(612, 104, 14, "#f5d76e", f"≈ {esc(fmt_money(a.monthly_projection, basis))}/mo at this pace",
              anchor="end", weight="500")
        )
    parts.append(f'<rect x="28" y="126" width="584" height="1" fill="#242a31"/>')

    y = 156
    parts.append(t(28, y, 12, "#8a949e", "where it burns"))
    y += 14
    shown = [
        (src, b) for src, b in list(a.by_source.items())[:4]
        if (b.cost > 0 if cost_total > 0 else b.tokens > 0)
    ]
    for src, b in shown:
        share = (b.cost / cost_total) if cost_total > 0 else (b.tokens / max(a.total.tokens, 1))
        label = src.replace("gateway:", "")
        bar_w = max(3, int(340 * share))
        val = f"{share:.0%} · {fmt_money(b.cost, basis)}" if cost_total > 0 else f"{share:.0%} · {fmt_tokens(b.tokens)}"
        parts.append(t(28, y + 10, 14, "#e6e9ec", esc(label[:14])))
        parts.append(f'<rect x="140" y="{y + 1}" width="340" height="10" rx="5" fill="#1b2026"/>')
        parts.append(f'<rect x="140" y="{y + 1}" width="{bar_w}" height="10" rx="5" fill="{_source_color(src)}"/>')
        parts.append(t(612, y + 10, 13, "#8a949e", esc(val), anchor="end"))
        y += 30

    if a.night.sessions > 0 and cost_total > 0:
        y += 8
        share = a.night.cost / cost_total
        parts.append(f'<rect x="28" y="{y}" width="584" height="36" rx="10" fill="#f7775a" opacity="0.12"/>')
        parts.append(
            t(44, y + 23, 14, "#f7a08a",
              f"🌙 while I slept ({a.night_window[0]:02d}–{a.night_window[1]:02d}): "
              f"{esc(fmt_money(a.night.cost, basis))} — {share:.0%} of everything", weight="500")
        )
        y += 36

    if a.overhead_per_call:
        src, v = max(a.overhead_per_call.items(), key=lambda kv: kv[1])
        if v > 0:
            from .benchmarks import overhead_vs_reference_short

            y += 26
            ref = overhead_vs_reference_short(v)
            line = f"{src.replace('gateway:', '')} re-sends {v:,} tokens with every call" + (f" — {ref}" if ref else "")
            parts.append(t(28, y, 12.5, "#8a949e", esc(line)))

    y += 28
    parts.append(t(28, y, 12, "#5c6670", "local &amp; private — nothing left my machine"))
    parts.append(t(612, y, 12, "#5c6670", REPO, anchor="end"))
    height = y + 22

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="agent burn card">\n'
        f'<rect width="{width}" height="{height}" rx="16" fill="#0b0d10"/>\n'
        + "\n".join(parts)
        + "\n</svg>\n"
    )
