"""`agentburn limits` — a subscription bills windows, not dollars.

On a subscription plan the invoice is fixed: optimizing does not change what
you pay, it changes how far you get before the wall. The wall is a *usage
window* — Claude Code enforces a rolling 5-hour allowance plus a weekly one.

Anthropic does not publish the formula behind those allowances, so this module
does not invent a threshold. It does two honest things instead:

1. Weighs your own tokens by *published API price ratios* — the only public
   statement about how much more one token costs than another — and sums them
   over rolling windows. That makes YOUR windows comparable with each other:
   the peak, the typical one, the one running right now.
2. If you tell it when you were actually cut off (`--hit "2026-08-20 14:30"`),
   the window that ended at that moment becomes a *measured* ceiling, and
   everything else is reported as a percentage of it. That number is yours,
   from your own wall, not a guess about the provider's arithmetic.

Unit: "weighted tokens" = tokens × price ratio, normalized so that one
uncached input token of the reference model = 1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from . import prices
from .model import BUCKET_SECONDS, Snapshot, agent_label

# Reference: one uncached input token of this model weighs 1.
REFERENCE_MODEL = "anthropic/claude-sonnet-5"
REF_IN_PRICE = prices.PRICES[REFERENCE_MODEL][0]

# Published Anthropic price ratios, relative to that model's input token:
# cache reads are billed at 0.1×, cache writes (5-minute TTL) at 1.25×.
# Output is not a constant — it comes from the model's own completion price.
CACHE_READ_W = 0.1
CACHE_WRITE_W = 1.25

DEFAULT_WINDOW_HOURS = 5.0
WEEK_SECONDS = 7 * 86400


@dataclass
class Window:
    start: float
    end: float
    weight: float


@dataclass
class LimitsReport:
    agent: str
    window_hours: float
    generated_at: float
    peak: Optional[Window] = None
    peak_by_model: list = field(default_factory=list)  # [(label, share)]
    peak_by_source: list = field(default_factory=list)
    typical: float = 0.0
    active_slots: int = 0
    current: float = 0.0
    week_total: float = 0.0
    busiest_day: Optional[tuple] = None  # (day_start_ts, weight)
    mix: list = field(default_factory=list)  # [(label, share)] over the period
    ceiling: Optional[float] = None
    ceiling_at: Optional[float] = None
    slots_over_ceiling: int = 0
    unsupported: str = ""  # non-empty when the adapter can't answer this
    notes: list = field(default_factory=list)


def token_weights(model: Optional[str]) -> tuple:
    """(input, output, cache_read, cache_write) weight of one token.

    Unknown models fall back to the reference model's own ratios rather than
    dropping out of the total — a window you can't fully price is still a
    window you filled.
    """
    p = prices.lookup(model or "") or prices.PRICES[REFERENCE_MODEL]
    f_in = p[0] / REF_IN_PRICE
    f_out = p[1] / REF_IN_PRICE
    return (f_in, f_out, f_in * CACHE_READ_W, f_in * CACHE_WRITE_W)


def cell_weight(cell) -> float:
    w_in, w_out, w_cr, w_cw = token_weights(cell.model)
    return (
        cell.input_tokens * w_in
        + cell.output_tokens * w_out
        + cell.cache_read_tokens * w_cr
        + cell.cache_write_tokens * w_cw
    )


def _shares(counter: dict) -> list:
    total = sum(counter.values())
    if total <= 0:
        return []
    return sorted(((k, v / total) for k, v in counter.items()), key=lambda kv: -kv[1])


def _rolling(series: dict, span: int) -> list:
    """[(window_end_ts, weight)] for every bucket, window = `span` seconds.

    `series` is bucket_start → weight (sparse). The scan walks a dense grid so
    that a quiet stretch inside a window still counts as quiet, not as absent.
    """
    if not series:
        return []
    lo, hi = min(series), max(series)
    n = int(span // BUCKET_SECONDS) or 1
    grid = [series.get(t, 0.0) for t in range(lo, hi + BUCKET_SECONDS, BUCKET_SECONDS)]
    out = []
    run = 0.0
    for i, v in enumerate(grid):
        run += v
        if i >= n:
            run -= grid[i - n]
        out.append((lo + (i + 1) * BUCKET_SECONDS, run))
    return out


def _median(xs: list) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def build_limits(
    snap: Snapshot,
    hit: Optional[float] = None,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    now: Optional[float] = None,
) -> LimitsReport:
    now = now or snap.generated_at or time.time()
    span = int(window_hours * 3600)
    rep = LimitsReport(agent=snap.agent, window_hours=window_hours, generated_at=now)

    if not snap.usage_cells:
        rep.unsupported = (
            f"{agent_label(snap.agent)}'s adapter does not record per-call timestamps yet, "
            "so windows can't be reconstructed. Windowed limits need intra-session "
            "resolution — sessions alone can't say which window a call landed in."
        )
        return rep

    series: dict = {}
    by_kind: dict = {
        "cache reads": 0.0,
        "uncached input": 0.0,
        "output": 0.0,
        "cache writes": 0.0,
    }
    for c in snap.usage_cells:
        w = cell_weight(c)
        if w <= 0:
            continue
        series[c.start] = series.get(c.start, 0.0) + w
        w_in, w_out, w_cr, w_cw = token_weights(c.model)
        by_kind["uncached input"] += c.input_tokens * w_in
        by_kind["output"] += c.output_tokens * w_out
        by_kind["cache reads"] += c.cache_read_tokens * w_cr
        by_kind["cache writes"] += c.cache_write_tokens * w_cw
    rep.mix = [kv for kv in _shares(by_kind) if kv[1] >= 0.005]

    rolling = _rolling(series, span)
    if rolling:
        end, weight = max(rolling, key=lambda kv: kv[1])
        rep.peak = Window(start=end - span, end=end, weight=weight)
        in_peak_model: dict = {}
        in_peak_source: dict = {}
        for c in snap.usage_cells:
            if rep.peak.start <= c.start < rep.peak.end:
                w = cell_weight(c)
                in_peak_model[c.model or "unknown"] = (
                    in_peak_model.get(c.model or "unknown", 0.0) + w
                )
                in_peak_source[c.source] = in_peak_source.get(c.source, 0.0) + w
        rep.peak_by_model = [kv for kv in _shares(in_peak_model)[:4] if kv[1] >= 0.005]
        rep.peak_by_source = [kv for kv in _shares(in_peak_source)[:4] if kv[1] >= 0.005]

    # Non-overlapping slots for "typical": a rolling maximum is by definition
    # unusual, and 60 overlapping views of the same busy hour would drag the
    # median toward the peak.
    slots: dict = {}
    for start, w in series.items():
        slots[start // span] = slots.get(start // span, 0.0) + w
    active = [w for w in slots.values() if w > 0]
    rep.typical = _median(active)
    rep.active_slots = len(active)

    rep.current = sum(w for t, w in series.items() if t >= now - span)
    rep.week_total = sum(w for t, w in series.items() if t >= now - WEEK_SECONDS)

    days: dict = {}
    for t, w in series.items():
        day = int(time.mktime(time.localtime(t)[:3] + (0, 0, 0, 0, 0, -1)))
        days[day] = days.get(day, 0.0) + w
    if days:
        rep.busiest_day = max(days.items(), key=lambda kv: kv[1])

    if hit:
        rep.ceiling_at = hit
        rep.ceiling = sum(w for t, w in series.items() if hit - span <= t < hit)
        if rep.ceiling > 0:
            rep.slots_over_ceiling = sum(1 for w in active if w >= rep.ceiling)
        else:
            rep.notes.append(
                "no usage recorded in the 5 hours before the timestamp you passed — "
                "check the date, or whether that window is inside --days."
            )
    return rep


def _fmt(w: float) -> str:
    from .report import fmt_tokens

    return fmt_tokens(w)


def _model_label(model: str) -> str:
    """Short, human-facing model name: no author prefix, no date suffix."""
    return prices._norm(model or "unknown").split("/")[-1][:24]


def _stamp(ts: float, with_date: bool = True) -> str:
    fmt = "%b %d %H:%M" if with_date else "%H:%M"
    return time.strftime(fmt, time.localtime(ts))


def render_limits(rep: LimitsReport, color: bool = True) -> str:
    from .report import P

    p = P(color)
    out = [
        "",
        p.b(
            f"⏳ agentburn limits — {rep.agent} · rolling {rep.window_hours:g}-hour windows"
        ),
    ]
    out.append(
        p.dim(
            "   a subscription bills windows, not dollars: this is how fast you fill one"
        )
    )
    out.append("")

    if rep.unsupported:
        out.append(f"   {rep.unsupported}")
        out.append("")
        return "\n".join(out)
    if not rep.peak:
        out.append("   Nothing recorded in this window — try `--days 0`.")
        out.append("")
        return "\n".join(out)

    peak = rep.peak
    # Pad first, colour second: ANSI escapes count toward f-string widths.
    out.append(
        "   "
        + p.b(f"{'PEAK WINDOW':<25}")
        + f" {_stamp(peak.start)}–{_stamp(peak.end, False)} · "
        + p.b(_fmt(peak.weight))
        + " weighted"
    )
    mix_bits = " · ".join(
        f"{_model_label(m)} {s:.0%}" for m, s in rep.peak_by_model[:3]
    )
    src_bits = " · ".join(f"{s} {v:.0%}" for s, v in rep.peak_by_source[:3])
    for bits in (mix_bits, src_bits):
        if bits:
            out.append("   " + " " * 26 + p.dim(bits))
    out.append(
        f"   {'TYPICAL WINDOW':<25} {_fmt(rep.typical):>10}   "
        + p.dim(f"median of {rep.active_slots} active {rep.window_hours:g}h slots")
    )
    if rep.typical > 0:
        ratio = peak.weight / rep.typical
        line = f"   {'PEAK / TYPICAL':<25} {ratio:>9.1f}×   "
        out.append(
            line + p.dim("a wall is hit by the peak, not by the median")
        )
    out.append(f"   {'LAST ' + f'{rep.window_hours:g}H':<25} {_fmt(rep.current):>10}")
    if rep.busiest_day:
        out.append(
            f"   {'BUSIEST DAY':<25} {_fmt(rep.busiest_day[1]):>10}   "
            + p.dim(time.strftime("%a %b %d", time.localtime(rep.busiest_day[0])))
        )
    out.append(
        f"   {'LAST 7 DAYS':<25} {_fmt(rep.week_total):>10}   "
        + p.dim("weekly caps count this")
    )
    out.append("")

    if rep.ceiling:
        out.append(p.b("   YOUR MEASURED CEILING"))
        out.append(
            p.dim(
                f"   the window that ended {_stamp(rep.ceiling_at)}, when you say you were cut off"
            )
        )
        out.append(f"   {'ceiling':<25} {_fmt(rep.ceiling):>10}   weighted tokens")
        for label, val in (
            ("peak window", peak.weight),
            (f"last {rep.window_hours:g}h", rep.current),
        ):
            share = val / rep.ceiling
            txt = f"   {label:<25} {share:>9.0%}   of your ceiling"
            out.append(
                p.red(txt) if share >= 0.9 else (p.yellow(txt) if share >= 0.6 else txt)
            )
        if rep.slots_over_ceiling:
            out.append(
                p.dim(
                    f"   {rep.slots_over_ceiling} other slot(s) in this window reached it too"
                )
            )
        out.append("")

    if rep.mix:
        out.append(p.b("   WHAT FILLS THE WINDOW"))
        out.append(
            p.dim("   weighted, so a cheap token counts for less than an expensive one")
        )
        for label, share in rep.mix:
            out.append(f"   {label:<25} {share:>9.0%}")
        out.append("")

    tips = _tips(rep)
    if tips:
        out.append(p.b("   💡 WHAT MOVES THE NEEDLE"))
        for i, t in enumerate(tips, 1):
            out.append(f"   {i}. {t}")
        out.append("")

    for n in rep.notes:
        out.append(p.yellow(f"   ⚠ {n}"))
    if not rep.ceiling:
        out.append(
            p.dim(
                "   Been cut off before? `agentburn limits --hit \"YYYY-MM-DD HH:MM\"` turns that\n"
                "   moment into a ceiling measured from your own wall."
            )
        )
    out.append(
        p.dim(
            "   No published formula exists for these allowances, so no absolute threshold is\n"
            f"   claimed here. weighted tokens = tokens × published price ratio (cache read "
            f"{CACHE_READ_W}× ·\n"
            f"   cache write {CACHE_WRITE_W}× · output per model), normalized to one input token\n"
            f"   of {REFERENCE_MODEL.split('/')[-1]} (price snapshot {prices.AS_OF}). Local data only."
        )
    )
    out.append("")
    return "\n".join(out)


def _tips(rep: LimitsReport) -> list:
    """Ranked, and only when the numbers actually support the claim."""
    tips = []
    share = dict(rep.mix)
    sub = dict(rep.peak_by_source).get("subagent", 0.0)
    top_model = rep.peak_by_model[0] if rep.peak_by_model else None

    if sub >= 0.25:
        tips.append(
            f"subagents were {sub:.0%} of your peak window — each one re-sends its own "
            "bootstrap context; fewer, larger delegations fill the window slower than many small ones."
        )
    if top_model and prices.lookup(top_model[0]):
        pin = prices.lookup(top_model[0])[0]
        if pin > REF_IN_PRICE and top_model[1] >= 0.5:
            cheaper = pin / REF_IN_PRICE
            tips.append(
                f"{top_model[0].split('/')[-1]} was {top_model[1]:.0%} of the peak window and each of "
                f"its tokens weighs {cheaper:.1f}× the reference — the same work on a smaller model "
                "buys back most of the window."
            )
    if share.get("cache reads", 0) >= 0.5:
        tips.append(
            f"cache reads are {share['cache reads']:.0%} of the weighted total even at 0.1× — "
            "that is context volume, not model choice: shorter sessions and fewer re-reads "
            "shrink it (see `agentburn why`)."
        )
    if share.get("output", 0) >= 0.35:
        tips.append(
            f"output is {share['output']:.0%} of the weighted total — output tokens cost ~5× input; "
            "asking for shorter answers is the cheapest lever you have."
        )
    return tips[:3]


def limits_json(rep: LimitsReport) -> dict:
    return {
        "agent": rep.agent,
        "window_hours": rep.window_hours,
        "unit": f"weighted tokens (1 = one input token of {REFERENCE_MODEL})",
        "unsupported": rep.unsupported or None,
        "peak": (
            {
                "start": rep.peak.start,
                "end": rep.peak.end,
                "weight": round(rep.peak.weight),
            }
            if rep.peak
            else None
        ),
        "peak_by_model": [
            {"model": m, "share": round(s, 4)} for m, s in rep.peak_by_model
        ],
        "peak_by_source": [
            {"source": m, "share": round(s, 4)} for m, s in rep.peak_by_source
        ],
        "typical_window": round(rep.typical),
        "active_slots": rep.active_slots,
        "current_window": round(rep.current),
        "last_7d": round(rep.week_total),
        "busiest_day": (
            {
                "day": time.strftime("%Y-%m-%d", time.localtime(rep.busiest_day[0])),
                "weight": round(rep.busiest_day[1]),
            }
            if rep.busiest_day
            else None
        ),
        "mix": [{"kind": k, "share": round(s, 4)} for k, s in rep.mix],
        "ceiling": round(rep.ceiling) if rep.ceiling else None,
        "ceiling_at": rep.ceiling_at,
        "slots_over_ceiling": rep.slots_over_ceiling,
        "tips": _tips(rep),
        "notes": rep.notes,
    }
