"""`agentburn why` — behavioral forensics over the agent's own recorded actions.

Where `report` answers "where does it burn", this answers "why": loops of
re-reading the same file, retry storms on failing tools, heartbeats that wake
up and do nothing, and money burned in runs that ended in failure.

Honesty rules: everything is computed from what the agent itself recorded;
these are observations with numbers, not causal verdicts; no message content
leaves the machine (only tool names, truncated argument keys and counters).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .model import Snapshot
from .report import fmt_money, fmt_tokens

FAIL_MARKERS = ("fail", "timeout", "kill", "error", "abort", "crash")


@dataclass
class Reread:
    session: str
    name: str
    arg: str
    count: int
    approx_tokens: int  # 0 = unknown


@dataclass
class Storm:
    session: str
    name: str
    errors: int
    calls: int


@dataclass
class BehaviorReport:
    agent: str
    rereads: list = field(default_factory=list)
    storms: list = field(default_factory=list)
    idle_heartbeats: tuple = (0, 0, None)  # count, total hb sessions, cost
    failure_cost: tuple = (0, None, 0, [])  # sessions, cost, tokens, examples
    reasoning_heavy: list = field(default_factory=list)  # (title, share, tokens)
    observations: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def _is_failure(outcome: str) -> bool:
    o = (outcome or "").lower()
    return any(m in o for m in FAIL_MARKERS)


def analyze_behavior(snap: Snapshot, top: int = 6) -> BehaviorReport:
    rep = BehaviorReport(agent=snap.agent)
    title = {s.id: (s.title or s.id)[:46] for s in snap.sessions}
    sess_by_id = {s.id: s for s in snap.sessions}

    # --- re-read loops: same tool + same salient argument, 3+ times in one session
    arg_counts = Counter()
    result_tokens = defaultdict(list)  # (sid, name) -> recorded result token weights
    for e in snap.events:
        if e.arg_key:
            arg_counts[(e.session_id, e.name, e.arg_key)] += 1
        if e.tokens:
            result_tokens[(e.session_id, e.name)].append(e.tokens)
    for (sid, name, arg), n in arg_counts.most_common():
        if n < 3:
            break
        recorded = result_tokens.get((sid, name))
        approx = int(sum(recorded) / len(recorded) * n) if recorded else 0
        rep.rereads.append(Reread(title.get(sid, sid), name, arg[:60], n, approx))
    rep.rereads = rep.rereads[:top]

    # --- retry storms: 3+ recorded errors of one tool in one session
    err = Counter()
    tot = Counter()
    for e in snap.events:
        tot[(e.session_id, e.name)] += 1
        if e.ok is False:
            err[(e.session_id, e.name)] += 1
    for (sid, name), n in err.most_common():
        if n < 3:
            break
        rep.storms.append(Storm(title.get(sid, sid), name, n, tot[(sid, name)]))
    rep.storms = rep.storms[:top]

    # --- idle heartbeats (sessions classified as heartbeat that did nothing)
    hb = [s for s in snap.sessions if s.source == "heartbeat"]
    if hb:
        has_events = {e.session_id for e in snap.events}
        idle = [
            s
            for s in hb
            if s.total_tokens == 0
            or (s.id not in has_events and snap.events and s.total_tokens < 2_000)
        ]
        cost = sum(s.cost_usd or 0.0 for s in idle) if any(s.cost_usd for s in idle) else None
        rep.idle_heartbeats = (len(idle), len(hb), cost)

    # --- money burned in failed/timeout runs
    failed_ids = [sid for sid, o in snap.outcomes.items() if _is_failure(str(o))]
    if failed_ids:
        cost = sum(sess_by_id[s].cost_usd or 0.0 for s in failed_ids if s in sess_by_id)
        toks = sum(sess_by_id[s].total_tokens for s in failed_ids if s in sess_by_id)
        cost_known = any(s in sess_by_id and sess_by_id[s].cost_usd is not None for s in failed_ids)
        examples = [
            f"{title.get(s, s)} ({snap.outcomes[s]})" for s in failed_ids[:3] if s in sess_by_id
        ]
        rep.failure_cost = (len(failed_ids), cost if cost_known else None, toks, examples)

    # --- thinks more than it works
    for s in snap.sessions:
        if s.reasoning_tokens > 0 and s.total_tokens >= 20_000:
            share = s.reasoning_tokens / s.total_tokens
            if share >= 0.5:
                rep.reasoning_heavy.append(((s.title or s.id)[:46], share, s.reasoning_tokens))
    rep.reasoning_heavy = sorted(rep.reasoning_heavy, key=lambda x: -x[1])[:top]

    # --- observations: up to 3, each names the burn and the change
    if rep.rereads:
        r = rep.rereads[0]
        tok = f" ≈{fmt_tokens(r.approx_tokens)} tokens re-paid" if r.approx_tokens else ""
        rep.observations.append(
            f"`{r.arg}` was fetched {r.count}× by {r.name} in one session{tok} — "
            "cache it or add a 'do not re-read unchanged files' rule to the agent's instructions."
        )
    if rep.storms:
        s = rep.storms[0]
        rep.observations.append(
            f"{s.name} failed {s.errors}× out of {s.calls} calls in one session and was re-paid "
            "each time — fix the tool (auth/path/flags) before optimizing anything else."
        )
    if rep.idle_heartbeats[0] > 0:
        n, total, cost = rep.idle_heartbeats
        c = f" ({fmt_money(cost, 'estimated')})" if cost else ""
        rep.observations.append(
            f"{n}/{total} heartbeat runs did nothing{c} — lengthen the heartbeat interval or "
            "move it to a cheap model; an idle agent should cost ~nothing."
        )
    if rep.failure_cost[0] > 0 and len(rep.observations) < 3:
        n, cost, toks, _ = rep.failure_cost
        c = fmt_money(cost, "estimated") if cost is not None else fmt_tokens(toks) + " tokens"
        rep.observations.append(
            f"{n} run(s) ended in failure/timeout and still cost {c} — add timeouts/budget caps "
            "so broken runs die cheap."
        )
    if rep.reasoning_heavy and len(rep.observations) < 3:
        t, share, toks = rep.reasoning_heavy[0]
        rep.observations.append(
            f"'{t}' spent {share:.0%} of its tokens thinking — consider a lower reasoning level "
            "for routine tasks."
        )
    rep.observations = rep.observations[:3]

    # --- per-agent honesty notes
    if snap.agent == "hermes":
        rep.notes.append("Hermes does not flag tool errors in its log — retry storms are not detectable here yet.")
    if snap.agent == "claude-code":
        rep.notes.append("Claude Code records no costs — behavioral findings are shown in tokens.")
    if not snap.events:
        rep.notes.append("No action-level events available for this agent — loop/storm detection skipped.")
    return rep


def behavior_json(rep: BehaviorReport) -> dict:
    from dataclasses import asdict

    return {
        "agentburn_why": 1,
        "agent": rep.agent,
        "rereads": [asdict(r) for r in rep.rereads],
        "storms": [asdict(s) for s in rep.storms],
        "idle_heartbeats": {"idle": rep.idle_heartbeats[0], "total": rep.idle_heartbeats[1],
                            "cost": rep.idle_heartbeats[2]},
        "failure_cost": {"sessions": rep.failure_cost[0], "cost": rep.failure_cost[1],
                         "tokens": rep.failure_cost[2], "examples": rep.failure_cost[3]},
        "reasoning_heavy": [{"session": t, "share": round(s, 3), "tokens": k}
                            for t, s, k in rep.reasoning_heavy],
        "observations": rep.observations,
        "notes": rep.notes,
    }


def render_behavior(rep: BehaviorReport, color: bool = True) -> str:
    b = (lambda s: f"\033[1m{s}\033[0m") if color else (lambda s: s)
    dim = (lambda s: f"\033[2m{s}\033[0m") if color else (lambda s: s)
    red = (lambda s: f"\033[31m{s}\033[0m") if color else (lambda s: s)
    out = ["", b(f"🔬 agentburn why — {rep.agent}")]
    out.append(dim("   what the agent actually did, from its own records — observations, not verdicts"))
    out.append("")

    if rep.rereads:
        out.append(b("   RE-READ LOOPS"))
        out.append(dim("   the same thing fetched again and again — every repeat is re-paid in full"))
        for r in rep.rereads:
            tok = f"  ≈{fmt_tokens(r.approx_tokens)}" if r.approx_tokens else ""
            out.append(f"   {r.count}× {r.name}({r.arg})  in {r.session}{tok}")
        out.append("")

    if rep.storms:
        out.append(b("   RETRY STORMS"))
        out.append(dim("   a failing tool, called again and again — paying full price for every error"))
        for s in rep.storms:
            out.append(red(f"   {s.name}: {s.errors} errors / {s.calls} calls  in {s.session}"))
        out.append("")

    n, total, cost = rep.idle_heartbeats
    if total > 0:
        line = f"   {n} of {total} heartbeat runs did NOTHING"
        if cost:
            line += f" — {fmt_money(cost, 'estimated')} of pure idle burn"
        out.append(b("   IDLE HEARTBEATS"))
        out.append(dim("   the agent woke up, thought about it, went back to sleep — you paid for it"))
        out.append(red(line) if n else f"   every heartbeat did real work ({total} runs)")
        out.append("")

    fn, fcost, ftoks, examples = rep.failure_cost
    if fn > 0:
        out.append(b("   BURNED ON FAILURES"))
        out.append(dim("   runs that ended in failure/timeout still consumed real money"))
        c = fmt_money(fcost, "estimated") if fcost is not None else f"{fmt_tokens(ftoks)} tokens"
        out.append(red(f"   {fn} failed run(s) → {c}"))
        for e in examples:
            out.append(dim(f"      {e}"))
        out.append("")

    if rep.reasoning_heavy:
        out.append(b("   THINKS MORE THAN IT WORKS"))
        out.append(dim("   reasoning share of total tokens, per session"))
        for t, share, toks in rep.reasoning_heavy:
            out.append(f"   {share:>4.0%} thinking · {fmt_tokens(toks):>7}  {t}")
        out.append("")

    if rep.observations:
        out.append(b("   💡 WHAT TO CHANGE"))
        for i, o in enumerate(rep.observations, 1):
            out.append(f"   {i}. {o}")
        out.append("")

    if not any([rep.rereads, rep.storms, rep.idle_heartbeats[1], rep.failure_cost[0], rep.reasoning_heavy]):
        out.append("   ✅ no behavioral anti-patterns detected in this window.")
        out.append("")

    for nline in rep.notes:
        out.append(dim(f"   ⓘ {nline}"))
    out.append(dim("   Local analysis of the agent's own records; tool names and truncated args only — no content."))
    out.append("")
    return "\n".join(out)
