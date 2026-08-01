"""Aggregations over the normalized snapshot. Pure functions, no I/O."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .model import SessionRec, Snapshot


@dataclass
class Bucket:
    sessions: int = 0
    api_calls: int = 0
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = False

    def add(self, s: SessionRec) -> None:
        self.sessions += 1
        self.api_calls += s.api_calls
        self.tokens += s.total_tokens
        self.input_tokens += s.input_tokens
        self.output_tokens += s.output_tokens
        self.cache_read_tokens += s.cache_read_tokens
        if s.cost_usd is not None:
            self.cost += s.cost_usd
            self.cost_known = True


@dataclass
class ParentRollup:
    id: str
    title: str
    model: Optional[str]
    own_cost: float
    sub_cost: float
    sub_tokens: int
    sub_sessions: int


@dataclass
class Analysis:
    agent: str
    source_path: str
    days: Optional[int]
    period_start: Optional[float]
    period_end: float
    total: Bucket
    by_source: dict
    by_model: dict
    tools: list
    night: Bucket
    night_by_source: dict
    night_window: tuple
    rollups: list
    overhead_per_call: dict  # source -> avg input tokens per api call
    composition: object
    cost_basis: str  # actual | estimated | mixed | unknown
    zero_token_sessions: int
    span_days: Optional[float]
    daily_cost: Optional[float]
    monthly_projection: Optional[float]
    warnings: list = field(default_factory=list)


def _is_night(ts: float, window: tuple) -> bool:
    h = time.localtime(ts).tm_hour
    start, end = window
    if start <= end:
        return start <= h < end
    return h >= start or h < end  # wraps midnight, e.g. 23-7


def analyze(snap: Snapshot, night_window: tuple = (0, 8)) -> Analysis:
    total = Bucket()
    by_source: dict = {}
    by_model: dict = {}
    night = Bucket()
    night_by_source: dict = {}
    zero_token = 0
    bases = set()

    for s in snap.sessions:
        total.add(s)
        by_source.setdefault(s.source, Bucket()).add(s)
        by_model.setdefault(s.model or "unknown", Bucket()).add(s)
        if s.started_at and _is_night(s.started_at, night_window):
            night.add(s)
            night_by_source.setdefault(s.source, Bucket()).add(s)
        if s.total_tokens == 0 and s.message_count > 0:
            zero_token += 1
        bases.add(s.cost_basis)

    bases.discard("unknown")
    cost_basis = (
        "unknown" if not bases else bases.pop() if len(bases) == 1 else "mixed"
    )

    # subagent costs rolled up to their root parents
    by_id = {s.id: s for s in snap.sessions}
    sub_cost: dict = {}
    sub_tokens: dict = {}
    sub_count: dict = {}
    for s in snap.sessions:
        if s.source != "subagent":
            continue
        root = s
        seen = set()
        while root.parent_id and root.parent_id in by_id and root.id not in seen:
            seen.add(root.id)
            root = by_id[root.parent_id]
        if root.id != s.id:
            sub_cost[root.id] = sub_cost.get(root.id, 0.0) + (s.cost_usd or 0.0)
            sub_tokens[root.id] = sub_tokens.get(root.id, 0) + (s.total_tokens or 0)
            sub_count[root.id] = sub_count.get(root.id, 0) + 1
    rollups = sorted(
        (
            ParentRollup(
                id=pid,
                title=(by_id[pid].title or pid)[:60],
                model=by_id[pid].model,
                own_cost=by_id[pid].cost_usd or 0.0,
                sub_cost=c,
                sub_tokens=sub_tokens.get(pid, 0),
                sub_sessions=sub_count[pid],
            )
            for pid, c in sub_cost.items()
            if pid in by_id
        ),
        key=lambda r: (r.sub_cost, r.sub_tokens),
        reverse=True,
    )[:5]

    # Everything the model reads on each call, cache included. Counting only
    # uncached input understates agents that rely on prompt caching (on Claude
    # Code nearly all input arrives as cache reads, which made this read ~165
    # tokens/call against a real ~154k — and turned the baseline comparison into
    # a meaningless "−89%").
    overhead = {
        src: round((b.input_tokens + b.cache_read_tokens) / b.api_calls)
        for src, b in by_source.items()
        if b.api_calls > 0
    }

    starts = [s.started_at for s in snap.sessions if s.started_at]
    period_start = min(starts) if starts else None
    span_days = (
        max(1.0, (snap.generated_at - period_start) / 86400) if period_start else None
    )
    daily = (total.cost / span_days) if (span_days and total.cost_known) else None

    agent_label = {
        "hermes": "Hermes",
        "openclaw": "OpenClaw",
        "claude-code": "Claude Code",
    }.get(snap.agent, snap.agent)

    warnings = list(snap.warnings)
    if zero_token > 0:
        gap_note = (
            " (e.g. streaming without usage, hermes-agent #12023)"
            if snap.agent == "hermes"
            else ""
        )
        warnings.append(
            f"{zero_token} session(s) have messages but zero recorded tokens — "
            f"{agent_label} accounting gaps{gap_note}. "
            "All totals are a LOWER BOUND."
        )
    if cost_basis == "estimated":
        warnings.append(
            f"Costs are {agent_label}'s own estimates, not provider-billed actuals."
        )
    if cost_basis == "mixed":
        warnings.append(f"Costs mix provider-billed actuals and {agent_label} estimates.")
    if cost_basis == "unknown" and total.tokens > 0:
        warnings.append(f"No cost data recorded by {agent_label} — token counts only.")

    return Analysis(
        agent=snap.agent,
        source_path=snap.source_path,
        days=snap.days,
        period_start=period_start,
        period_end=snap.generated_at,
        total=total,
        by_source=dict(sorted(by_source.items(), key=lambda kv: kv[1].cost, reverse=True)),
        by_model=dict(sorted(by_model.items(), key=lambda kv: kv[1].cost, reverse=True)),
        tools=snap.tools[:10],
        night=night,
        night_by_source=dict(
            sorted(night_by_source.items(), key=lambda kv: kv[1].cost, reverse=True)
        ),
        night_window=night_window,
        rollups=rollups,
        overhead_per_call=overhead,
        composition=snap.composition,
        cost_basis=cost_basis,
        zero_token_sessions=zero_token,
        span_days=span_days,
        daily_cost=daily,
        monthly_projection=daily * 30 if daily is not None else None,
        warnings=warnings,
    )
