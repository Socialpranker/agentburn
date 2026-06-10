"""`agentburn drift` — your spend × the world's direction.

You pay for models. The world's usage of those models moves every week —
and nobody tells you when you're paying for a model everyone else is
leaving. drift joins two things only this toolchain has:

- YOUR model spend, computed locally from the agents' own logs (no key);
- the WORLD's per-model usage trend, published as open JSON by token-history
  (archived daily from OpenRouter's public rankings; deep history when the
  archive has an OPENROUTER_API_KEY).

Network note (the one deliberate exception to "no network"): drift performs
a single read-only GET of that public trends JSON. Nothing about you is sent
anywhere; --trends accepts a local file; everything else works offline.
"""

from __future__ import annotations

import json
import os
import urllib.request

from . import prices
from .analyze import Analysis
from .report import fmt_money, fmt_tokens

TRENDS_URL = "https://socialpranker.github.io/token-history/data/models/trends.json"


def load_trends(source: str = TRENDS_URL, timeout: int = 15) -> dict:
    if os.path.exists(source):
        with open(source, "r", encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(source, headers={"user-agent": "agentburn-drift"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001 — any network failure → graceful message
        raise RuntimeError(
            f"world trends unavailable ({e}). The public archive may still be warming up — "
            "see https://github.com/Socialpranker/token-history. Pass --trends FILE to use a local copy."
        ) from None


def _norm(slug: str) -> str:
    from .prices import _norm as n

    return n(slug)


def build_drift(analyses: list, trends: dict, min_monthly: float = 2.0) -> dict:
    """→ {rows: [...], advice: [...], warming_up, days_covered}"""
    world = {_norm(k): v for k, v in (trends.get("models") or {}).items()}
    risers = [
        {**r, "norm": _norm(r["slug"])}
        for r in (trends.get("risers") or [])
        if r.get("pct_4w") is not None
    ]

    # aggregate your models across agents (monthly pace per model)
    mine = {}
    for a in analyses:
        if not a.span_days:
            continue
        f = 30.0 / a.span_days
        for model, b in a.by_model.items():
            if not model or model == "unknown":
                continue
            m = mine.setdefault(
                _norm(model),
                {"model": model, "monthly_cost": 0.0, "cost_known": False,
                 "monthly_tokens": 0.0, "agents": set()},
            )
            if b.cost_known:
                m["monthly_cost"] += b.cost * f
                m["cost_known"] = True
            m["monthly_tokens"] += b.tokens * f
            m["agents"].add(a.agent.split(" ·")[0])

    rows = []
    advice = []
    for norm, m in sorted(mine.items(), key=lambda kv: -kv[1]["monthly_cost"]):
        w = world.get(norm)
        pct = w.get("pct_4w") if w else None
        rows.append({
            "model": m["model"],
            "monthly_cost": m["monthly_cost"] if m["cost_known"] else None,
            "monthly_tokens": m["monthly_tokens"],
            "world_pct_4w": pct,
            "world_known": w is not None,
            "agents": sorted(m["agents"]),
        })
        # advice: you spend real money on a model the world is leaving
        spend = m["monthly_cost"] if m["cost_known"] else 0.0
        if pct is not None and pct <= -20 and spend >= min_monthly:
            my_price = prices.lookup(m["model"])
            alt = None
            for r in risers:
                rp = prices.lookup(r["norm"])
                if rp and my_price and rp[0] < my_price[0]:
                    cheaper_pct = round((1 - rp[0] / my_price[0]) * 100)
                    alt = (r["norm"], r["pct_4w"], cheaper_pct)
                    break
            line = (
                f"{m['model']}: you spend {fmt_money(spend, 'estimated')}/mo; world usage "
                f"{pct:+.0f}% in 4 weeks — the world is leaving this model."
            )
            if alt:
                line += (
                    f" Rising alternative {alt[0]} ({alt[1]:+.0f}%) is ~{alt[2]}% cheaper on input "
                    f"(price snapshot {prices.AS_OF})."
                )
            advice.append(line)

    return {
        "rows": rows,
        "advice": advice,
        "warming_up": bool(trends.get("warming_up")),
        "days_covered": trends.get("days_covered"),
        "as_of": trends.get("as_of"),
        "note": trends.get("note", ""),
    }


def render_drift(d: dict, color: bool = True) -> str:
    b = (lambda s: f"\033[1m{s}\033[0m") if color else (lambda s: s)
    dim = (lambda s: f"\033[2m{s}\033[0m") if color else (lambda s: s)
    red = (lambda s: f"\033[31m{s}\033[0m") if color else (lambda s: s)
    green = (lambda s: f"\033[32m{s}\033[0m") if color else (lambda s: s)

    out = ["", b("🧭 agentburn drift — your spend × the world's direction")]
    out.append(dim("   are you paying for a model the world is leaving?"))
    out.append("")
    if d["warming_up"]:
        days = d.get("days_covered") or 0
        out.append(
            f"   ⏳ world archive is warming up: {days}/35 days collected — trends appear "
            f"as token-history accumulates (or instantly once its OPENROUTER_API_KEY secret is set)."
        )
        out.append("")
    if not d["rows"]:
        out.append("   no local model spend found in this window.")
        out.append("")
        return "\n".join(out)

    out.append(b("   YOUR MODELS vs THE WORLD (4-week world trend)"))
    for r in d["rows"][:8]:
        cost = fmt_money(r["monthly_cost"], "estimated") + "/mo" if r["monthly_cost"] is not None \
            else fmt_tokens(r["monthly_tokens"]) + " tok/mo"
        pct = r["world_pct_4w"]
        if pct is None:
            trend = dim("world: no data" if not r["world_known"] else "world: warming up")
        elif pct <= -20:
            trend = red(f"world {pct:+.0f}% ⬊")
        elif pct >= 20:
            trend = green(f"world {pct:+.0f}% ⬈")
        else:
            trend = f"world {pct:+.0f}% →"
        out.append(f"   {r['model'][:38]:<40} {cost:>14}   {trend}")
    out.append("")

    if d["advice"]:
        out.append(b("   💡 DRIFT ALERTS"))
        for i, a in enumerate(d["advice"], 1):
            out.append(red(f"   {i}. {a}"))
        out.append("")

    out.append(dim(f"   world data: token-history archive · {d.get('as_of') or ''}"))
    out.append(dim("   " + (d.get("note") or "")))
    out.append(dim("   your data never leaves this machine; drift only GETs public trend JSON."))
    out.append("")
    return "\n".join(out)
