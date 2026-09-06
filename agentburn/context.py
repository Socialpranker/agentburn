"""`agentburn context` — the price of a long context, and what a `/clear` buys.

On a subscription the window is filled by what the model re-reads, and every
call re-reads the whole context: a turn at 300k costs the window as much as
three turns at 100k. Claude Code records the exact size of every call's
context (uncached input + cache reads + cache writes), so this is measured,
not modelled.

Two questions, both answered from the same per-call records:
- how much of the window went to calls whose context was already past N —
  the share of usage that is "long-session tax";
- if every session had been restarted at a threshold T, how much of the
  weighted window volume would not have been spent — the honest saving of
  a `/clear` habit, assuming the same amount of work in shorter sessions.

Skill costs are measured the same way: the growth of the context between the
reply that invoked a skill and the next one, when the skill was the only
tool call in that reply. Bundled skills never touch the disk, so reading the
file would miss them; the transcript sees all of them.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .limits import token_weights
from .model import Snapshot, agent_label
from .report import fmt_tokens

BANDS = ((0, "<50k"), (50_000, "50–100k"), (100_000, "100–200k"), (200_000, "200–400k"), (400_000, ">400k"))
THRESHOLDS = (100_000, 150_000, 200_000, 300_000)
# Median of the last K observations per skill: skills change size, a maximum
# would keep one old outlier forever, a global median lags behind a diet.
SKILL_KEEP = 5


@dataclass
class Band:
    label: str
    calls: int = 0
    context: int = 0  # raw tokens
    weight: float = 0.0  # weighted (price-ratio) tokens


@dataclass
class Saving:
    threshold: int
    share: float  # weighted volume that would not have been spent
    calls_over: int


@dataclass
class SkillCost:
    skill: str
    calls: int
    tokens: int  # median of the last SKILL_KEEP measurements
    total: int  # tokens × calls in this window


@dataclass
class ContextReport:
    agent: str
    calls: int = 0
    bands: list = field(default_factory=list)  # Band
    median_context: int = 0
    p90_context: int = 0
    max_context: int = 0
    savings: list = field(default_factory=list)  # Saving
    by_effort: list = field(default_factory=list)  # [(effort, calls, share_of_weight)]
    longest: list = field(default_factory=list)  # [(session, max_context, calls)]
    skills: list = field(default_factory=list)  # SkillCost
    total_weight: float = 0.0
    unsupported: str = ""


def _weight(model: Optional[str], context: int, output: int) -> float:
    # A context call doesn't say which part was cache read vs uncached, so the
    # whole context is weighted at the cache-read rate: the smallest ratio,
    # i.e. a lower bound. Output is weighted at its real price.
    w_in, w_out, w_cr, _ = token_weights(model)
    return context * w_cr + output * w_out


def build_context(snap: Snapshot) -> ContextReport:
    rep = ContextReport(agent=snap.agent)
    if not snap.context_calls:
        rep.unsupported = (
            f"{agent_label(snap.agent)}'s adapter does not record per-call context sizes, "
            "so the price of long sessions can't be measured here."
        )
        return rep

    bands = {label: Band(label) for _, label in BANDS}
    sizes = []
    total_w = 0.0
    over = {t: [0, 0.0] for t in THRESHOLDS}  # calls over, weight over
    effort_w: dict = defaultdict(lambda: [0, 0.0])
    per_session: dict = defaultdict(lambda: [0, 0])  # max ctx, calls
    for c in snap.context_calls:
        w = _weight(c.model, c.context, c.output)
        total_w += w
        sizes.append(c.context)
        label = BANDS[0][1]
        for lo, lab in BANDS:
            if c.context >= lo:
                label = lab
        b = bands[label]
        b.calls += 1
        b.context += c.context
        b.weight += w
        for t in THRESHOLDS:
            if c.context > t:
                over[t][0] += 1
                # What a restart at t would have saved on this call: the part of
                # the context above t, at the cache-read rate.
                over[t][1] += (c.context - t) * token_weights(c.model)[2]
        e = effort_w[c.effort or "default"]
        e[0] += 1
        e[1] += w
        ps = per_session[c.session]
        ps[0] = max(ps[0], c.context)
        ps[1] += 1

    rep.calls = len(sizes)
    rep.total_weight = total_w
    rep.bands = [bands[lab] for _, lab in BANDS if bands[lab].calls]
    sizes.sort()
    rep.median_context = int(statistics.median(sizes))
    rep.p90_context = sizes[min(len(sizes) - 1, int(len(sizes) * 0.9))]
    rep.max_context = sizes[-1]
    if total_w > 0:
        rep.savings = [
            Saving(threshold=t, share=over[t][1] / total_w, calls_over=over[t][0]) for t in THRESHOLDS
        ]
        rep.by_effort = sorted(
            ((e, n, w / total_w) for e, (n, w) in effort_w.items()), key=lambda kv: -kv[2]
        )
    titles = {s.id: s.title or s.id for s in snap.sessions}
    rep.longest = sorted(
        ((titles.get(sid, sid), mx, n) for sid, (mx, n) in per_session.items()),
        key=lambda r: -r[1],
    )[:5]

    by_skill: dict = defaultdict(list)
    for sl in snap.skill_loads:
        by_skill[sl.skill].append((sl.ts or 0, sl.tokens))
    costs = []
    for skill, obs in by_skill.items():
        obs.sort()
        recent = [t for _, t in obs[-SKILL_KEEP:]]
        med = int(statistics.median(recent))
        costs.append(SkillCost(skill=skill, calls=len(obs), tokens=med, total=med * len(obs)))
    rep.skills = sorted(costs, key=lambda c: -c.total)[:8]
    return rep


def render_context(rep: ContextReport, color: bool = True) -> str:
    from .report import P

    p = P(color)
    out = ["", p.b(f"📏 agentburn context — {rep.agent} · what a long context costs")]
    out.append(p.dim("   every call re-reads its whole context; on a subscription that IS the window"))
    out.append("")
    if rep.unsupported:
        out.append(f"   {rep.unsupported}")
        out.append("")
        return "\n".join(out)
    if not rep.calls:
        out.append("   Nothing recorded in this window — try `--days 0`.")
        out.append("")
        return "\n".join(out)

    out.append(
        f"   {'CALLS':<25} {rep.calls:>10,}   "
        + p.dim(f"median context {fmt_tokens(rep.median_context)} · p90 {fmt_tokens(rep.p90_context)} · max {fmt_tokens(rep.max_context)}")
    )
    out.append("")
    out.append(p.b("   WHERE THE WINDOW GOES, BY CONTEXT SIZE"))
    out.append(p.dim("   share of weighted volume · calls"))
    for b in rep.bands:
        share = b.weight / rep.total_weight if rep.total_weight else 0
        bar = "█" * max(0, min(18, round(share * 18))) + "·" * (18 - max(0, min(18, round(share * 18))))
        line = f"   {b.label:<12} {bar} {share:>5.0%}   {b.calls:>7,} calls"
        out.append(p.red(line) if b.label in (">400k",) and share >= 0.1 else line)
    out.append("")

    if rep.savings:
        out.append(p.b("   IF YOU HAD RESTARTED AT…"))
        out.append(p.dim("   the part of every call's context above the threshold, at the cache-read rate"))
        for sv in rep.savings:
            line = f"   /clear at {fmt_tokens(sv.threshold):<8} → {sv.share:>5.0%} of the window not spent   ({sv.calls_over:,} calls were past it)"
            out.append(p.green(line) if sv.share >= 0.25 else line)
        out.append(p.dim("   assumes the same work done in shorter sessions; a restart itself costs one bootstrap"))
        out.append("")

    if rep.longest:
        out.append(p.b("   LONGEST SESSIONS"))
        for title, mx, n in rep.longest:
            out.append(f"   {title[:40]:<40} {fmt_tokens(mx):>8} max context · {n:,} calls")
        out.append("")

    if rep.by_effort and len(rep.by_effort) > 1:
        out.append(p.b("   BY EFFORT LEVEL"))
        for e, n, share in rep.by_effort:
            out.append(f"   {e:<12} {share:>5.0%} of weighted volume · {n:,} calls")
        out.append("")

    if rep.skills:
        out.append(p.b("   WHAT A SKILL COSTS"))
        out.append(p.dim("   measured: context growth right after a lone Skill call, median of recent loads"))
        for sc in rep.skills:
            out.append(f"   {sc.skill[:36]:<36} {fmt_tokens(sc.tokens):>8} per load × {sc.calls:>4} = {fmt_tokens(sc.total):>8}")
        out.append("")

    out.append(p.dim("   context = uncached input + cache reads + cache writes of each call, as Claude Code recorded it."))
    out.append(p.dim("   Shares are weighted by published price ratios; the context part at the cache-read rate (a lower bound)."))
    out.append("")
    return "\n".join(out)


def context_json(rep: ContextReport) -> dict:
    return {
        "agent": rep.agent,
        "unsupported": rep.unsupported or None,
        "calls": rep.calls,
        "median_context": rep.median_context,
        "p90_context": rep.p90_context,
        "max_context": rep.max_context,
        "bands": [
            {"band": b.label, "calls": b.calls, "context_tokens": b.context,
             "share": round(b.weight / rep.total_weight, 4) if rep.total_weight else 0}
            for b in rep.bands
        ],
        "savings": [
            {"clear_at": s.threshold, "share_not_spent": round(s.share, 4), "calls_over": s.calls_over}
            for s in rep.savings
        ],
        "by_effort": [{"effort": e, "calls": n, "share": round(sh, 4)} for e, n, sh in rep.by_effort],
        "longest_sessions": [{"session": t, "max_context": mx, "calls": n} for t, mx, n in rep.longest],
        "skills": [
            {"skill": s.skill, "loads": s.calls, "tokens_per_load": s.tokens, "total": s.total}
            for s in rep.skills
        ],
        "generated_at": time.time(),
    }
