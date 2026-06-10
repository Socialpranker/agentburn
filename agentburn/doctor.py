"""agentburn doctor — diagnose the agent's own accounting gaps and generate a
ready-to-paste upstream bug report.

The trust problem is universal: trackers disagree because the underlying
accounting is broken (zero-usage streams, unpriced models). doctor names the
broken combination (provider × model × source) so the user can fix config or
file a precise upstream issue instead of distrusting every number.
"""

from __future__ import annotations

import time
from collections import Counter

from .model import Snapshot

KNOWN_ISSUES = "https://github.com/NousResearch/hermes-agent/issues/12023"


def diagnose(snap: Snapshot) -> dict:
    zero_groups = Counter()
    unpriced_groups = Counter()
    zero_total = unpriced_total = 0
    for s in snap.sessions:
        key = (s.provider or "unknown-provider", s.model or "unknown-model", s.source)
        if s.message_count > 0 and s.total_tokens == 0:
            zero_groups[key] += 1
            zero_total += 1
        if s.total_tokens > 0 and s.cost_usd is None:
            unpriced_groups[key] += 1
            unpriced_total += 1
    return {
        "sessions": len(snap.sessions),
        "zero_total": zero_total,
        "zero_groups": zero_groups.most_common(8),
        "unpriced_total": unpriced_total,
        "unpriced_groups": unpriced_groups.most_common(8),
    }


def render_doctor(snap: Snapshot, color: bool = True) -> str:
    d = diagnose(snap)
    b = (lambda s: f"\033[1m{s}\033[0m") if color else (lambda s: s)
    y = (lambda s: f"\033[33m{s}\033[0m") if color else (lambda s: s)
    out = ["", b(f"🩺 agentburn doctor — {snap.agent} accounting health"), ""]
    out.append(f"   sessions inspected : {d['sessions']}")
    out.append(f"   zero-usage sessions: {d['zero_total']} (messages exist, tokens recorded = 0)")
    out.append(f"   unpriced sessions  : {d['unpriced_total']} (tokens exist, no cost recorded)")
    out.append("")

    if d["zero_total"] == 0 and d["unpriced_total"] == 0:
        out.append("   ✅ accounting looks healthy — every number in `agentburn` is trustworthy.")
        out.append("")
        return "\n".join(out)

    if d["zero_groups"]:
        out.append(b("   ZERO-USAGE BY (provider × model × source):"))
        for (prov, model, src), n in d["zero_groups"]:
            out.append(f"   {n:>3} × {prov} · {model} · {src}")
        out.append("")
    if d["unpriced_groups"]:
        out.append(b("   UNPRICED BY (provider × model × source):"))
        for (prov, model, src), n in d["unpriced_groups"]:
            out.append(f"   {n:>3} × {prov} · {model} · {src}")
        out.append("")

    out.append(y("   Until fixed upstream, every agentburn total is a LOWER BOUND."))
    out.append("")
    out.append("   ── copy below into a GitHub issue " + "─" * 30)
    out.append(issue_markdown(snap, d))
    out.append("   " + "─" * 64)
    out.append("")
    return "\n".join(out)


def issue_markdown(snap: Snapshot, d: dict) -> str:
    date = time.strftime("%Y-%m-%d")
    lines = [
        f"### Token accounting gaps: {d['zero_total']} zero-usage / {d['unpriced_total']} unpriced sessions",
        "",
        f"Scanned {d['sessions']} sessions in local `state.db` on {date} "
        f"(read-only, via [agentburn](https://github.com/Socialpranker/agentburn) doctor).",
        "",
    ]
    if d["zero_groups"]:
        lines += ["**Sessions with messages but zero recorded usage** (likely streaming without usage payload — see " + KNOWN_ISSUES + "):", ""]
        lines += [f"- {n} × `{prov}` / `{model}` / source `{src}`" for (prov, model, src), n in d["zero_groups"]]
        lines.append("")
    if d["unpriced_groups"]:
        lines += ["**Sessions with tokens but no recorded cost** (pricing table gap?):", ""]
        lines += [f"- {n} × `{prov}` / `{model}` / source `{src}`" for (prov, model, src), n in d["unpriced_groups"]]
        lines.append("")
    lines += [
        "Happy to provide schema-level details (`PRAGMA table_info`) or re-run with a debug flag.",
        "_No message content was read or shared — counters only._",
    ]
    return "\n".join("   " + l for l in lines)
