"""Rule-based recommendations: each one names the burn and the action.

Rules are deliberately conservative: a recommendation fires only when the
category is a meaningful share of spend, and every estimate is phrased as
an upper bound of the saving, never a promise.
"""

from __future__ import annotations

from .analyze import Analysis

EXPENSIVE_HINTS = ("opus", "gpt-5", "o3", "sonnet", "pro")


def _money(x: float) -> str:
    return f"${x:,.2f}"


def recommend(a: Analysis) -> list:
    recs = []
    cost = a.total.cost if a.total.cost_known else 0.0

    # 1. night burn
    if cost > 0 and a.night.cost / cost >= 0.25:
        share = a.night.cost / cost
        top = next(iter(a.night_by_source), None)
        monthly = (a.monthly_projection or 0) * share
        recs.append(
            f"{share:.0%} of spend happens at night ({a.night_window[0]:02d}:00–"
            f"{a.night_window[1]:02d}:00 local{', mostly ' + top if top else ''}). "
            f"That's ≈{_money(monthly)}/mo while you sleep — review scheduled jobs and "
            "gateway autonomy; route night work to a cheaper model."
        )

    # 2. cron on an expensive model — with real-price arithmetic when possible
    cron = a.by_source.get("cron")
    if cron and cost > 0 and cron.cost / cost >= 0.15:
        from . import prices

        cron_models = [
            m for m, b in a.by_model.items() if any(h in (m or "").lower() for h in EXPENSIVE_HINTS)
        ]
        monthly = (a.monthly_projection or 0) * (cron.cost / cost)
        hint = (
            f" Frontier-class models in use ({', '.join(cron_models[:2])}) — scheduled "
            "maintenance rarely needs them."
            if cron_models
            else ""
        )
        saving = ""
        if a.span_days and cron.input_tokens + cron.output_tokens > 0:
            f = 30.0 / a.span_days
            cheap = prices.cheap_cost_usd(cron.input_tokens * f, cron.output_tokens * f)
            if monthly > cheap > 0:
                saving = (
                    f" Switching them to {prices.CHEAP_REFERENCE} ≈ {_money(cheap)}/mo "
                    f"→ saves ≈{_money(monthly - cheap)}/mo (price snapshot {prices.AS_OF})."
                )
        recs.append(
            f"Scheduled (cron) sessions are {cron.cost / cost:.0%} of spend "
            f"(≈{_money(monthly)}/mo).{hint} Point cron jobs at a cheap model in config.{saving}"
        )

    # 3. fixed overhead per call
    heavy = {s: v for s, v in a.overhead_per_call.items() if v >= 12000}
    if heavy:
        worst = max(heavy, key=heavy.get)
        comp = a.composition
        comp_note = (
            f" Sampled request dumps put input at ~{comp.tools_share:.0%} tool definitions / "
            f"~{comp.system_share:.0%} system prompt / ~{comp.history_share:.0%} history."
            if comp
            else ""
        )
        recs.append(
            f"Average input is {heavy[worst]:,} tokens per API call on `{worst}` — classic "
            f"fixed-overhead pattern (tool definitions + system prompt resent every call)."
            f"{comp_note} Trim per-platform toolsets in config and prune unused skills."
        )

    # 4. subagent recursion
    sub = a.by_source.get("subagent")
    if sub and cost > 0 and sub.cost / cost >= 0.30:
        recs.append(
            f"Subagents consume {sub.cost / cost:.0%} of spend across {sub.sessions} sessions. "
            "Chained delegation compounds non-linearly — set a depth/budget cap for delegate_task."
        )

    # 5. gateways heavier than CLI
    gw_over = {
        s: v
        for s, v in a.overhead_per_call.items()
        if s.startswith("gateway:") and a.overhead_per_call.get("cli") and v >= 1.8 * a.overhead_per_call["cli"]
    }
    if gw_over:
        s = max(gw_over, key=gw_over.get)
        recs.append(
            f"`{s}` carries {gw_over[s]:,} input tokens/call vs {a.overhead_per_call['cli']:,} on CLI "
            "(messenger gateways resend bootstrap context). Use a smaller per-platform toolset for it."
        )

    # 6. data-quality first when accounting is broken
    if a.zero_token_sessions > 0 and a.total.sessions > 0 and a.zero_token_sessions / a.total.sessions >= 0.05:
        recs.insert(
            0,
            f"{a.zero_token_sessions}/{a.total.sessions} sessions recorded zero tokens despite "
            "having messages — fix accounting first (check provider usage reporting; "
            "hermes-agent #12023), otherwise every number here is an undercount.",
        )

    return recs[:4]
