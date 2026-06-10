"""Optimize → prove it: save a baseline, change your config, compare.

Comparisons are pace-normalized (per-month figures), so a 7-day baseline can
be compared against a 30-day current window honestly.
"""

from __future__ import annotations

import json
import os
import time

from .analyze import Analysis
from .report import fmt_money

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".agentburn", "baseline.json")


def _monthly_by_source(a: Analysis) -> dict:
    total = a.total.cost or 0.0
    proj = a.monthly_projection or 0.0
    if total <= 0 or proj <= 0:
        return {}
    return {src: proj * (b.cost / total) for src, b in a.by_source.items() if b.cost > 0}


def snapshot_for_baseline(a: Analysis) -> dict:
    return {
        "saved_at": time.time(),
        "agent": a.agent,
        "days": a.days,
        "cost_basis": a.cost_basis,
        "monthly_projection": a.monthly_projection,
        "monthly_by_source": _monthly_by_source(a),
        "overhead_per_call": a.overhead_per_call,
        "night_monthly": (a.monthly_projection or 0.0)
        * ((a.night.cost / a.total.cost) if (a.total.cost or 0) > 0 else 0.0),
    }


def save(a: Analysis, path: str = DEFAULT_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot_for_baseline(a), f, indent=1)
    return path


def load(path: str = DEFAULT_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _delta(old: float, new: float, basis: str) -> str:
    d = new - old
    pct = f" ({d / old:+.0%})" if old else ""
    return f"{fmt_money(old, basis)} → {fmt_money(new, basis)}  ({'+' if d >= 0 else '−'}{fmt_money(abs(d), '').lstrip('~')}{pct})"


def render_compare(a: Analysis, base: dict) -> str:
    basis = a.cost_basis
    cur = snapshot_for_baseline(a)
    age_days = max(0, (time.time() - base.get("saved_at", time.time())) / 86400)
    out = ["", f"📐 Δ vs baseline saved {age_days:.0f} day(s) ago", ""]

    bo, co = base.get("monthly_projection"), cur.get("monthly_projection")
    if bo is not None and co is not None:
        eps = max(0.01, abs(bo) * 0.002)  # ignore float drift / sub-cent noise
        verdict = "✅ cheaper" if co < bo - eps else "⚠ more expensive" if co > bo + eps else "≈ flat"
        out.append(f"   monthly pace : {_delta(bo, co, basis)}  {verdict}")

    bsrc, csrc = base.get("monthly_by_source", {}), cur.get("monthly_by_source", {})
    for src in sorted(set(bsrc) | set(csrc), key=lambda s: bsrc.get(s, 0), reverse=True)[:6]:
        out.append(f"   {src:<13}: {_delta(bsrc.get(src, 0.0), csrc.get(src, 0.0), basis)}")

    bov, cov = base.get("overhead_per_call", {}), cur.get("overhead_per_call", {})
    common = [s for s in cov if s in bov and bov[s] > 0]
    if common:
        out.append("")
        out.append("   overhead, input tokens per call:")
        for s in sorted(common, key=lambda s: bov[s], reverse=True)[:4]:
            d = cov[s] - bov[s]
            out.append(f"   {s:<13}: {bov[s]:,} → {cov[s]:,} ({d:+,})")

    bn, cn = base.get("night_monthly"), cur.get("night_monthly")
    if bn is not None and cn is not None and (bn or cn):
        out.append("")
        out.append(f"   🌙 night/mo   : {_delta(bn, cn, basis)}")

    out.append("")
    out.append("   (pace-normalized: monthly figures, so different windows compare honestly)")
    out.append("")
    return "\n".join(out)
