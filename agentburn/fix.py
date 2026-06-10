"""`agentburn fix` — turn findings into ready-to-paste config patches.

DRY-RUN BY DESIGN: nothing is ever written. Every patch names the exact file,
shows current → proposed, and states the expected effect. You paste it
yourself, then prove it with `agentburn --save-baseline` → `--compare`.

Patch generators exist ONLY for config keys verified against the agents'
source code (June 2026):
- Hermes: per-job `model` / `enabled_toolsets` in ~/.hermes/cron/jobs.json
  (cron/jobs.py), per-platform toolsets in config.yaml (gateway/run.py).
- OpenClaw: `agents.defaults.heartbeat.{every, activeHours, model, lightContext}`
  in ~/.openclaw/openclaw.json (config/types.agent-defaults.ts).
Findings with no verified lever stay recommendations, not patches.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .analyze import Analysis
from .report import fmt_money

CHEAP_MODEL_HINT = "deepseek/deepseek-chat"  # example; any cheap model works
EXPENSIVE_HINTS = ("opus", "gpt-5", "o3", "sonnet", "pro")


@dataclass
class Patch:
    title: str
    target: str
    target_exists: bool
    why: str
    impact: str
    current: str = ""
    proposed: str = ""
    notes: list = field(default_factory=list)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _hermes_jobs(raw):
    if isinstance(raw, dict):
        raw = raw.get("jobs", raw)
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [j for j in raw if isinstance(j, dict)] if isinstance(raw, list) else []


def build_fixes(agent: str, source_path: str, a: Analysis, brep=None) -> list:
    patches = []
    cost_total = a.total.cost or 0.0
    monthly = a.monthly_projection or 0.0

    if agent.startswith("hermes"):
        root = os.path.dirname(source_path)  # …/.hermes
        cron = a.by_source.get("cron")
        if cron and cost_total > 0 and cron.cost / cost_total >= 0.15:
            jobs_path = os.path.join(root, "cron", "jobs.json")
            jobs = _hermes_jobs(_read_json(jobs_path) or [])
            cron_monthly = monthly * (cron.cost / cost_total)
            cur_lines, prop_lines = [], []
            for j in jobs[:5]:
                name = str(j.get("name") or j.get("id") or "job")[:40]
                model = j.get("model") or "(agent default)"
                cur_lines.append(f'"{name}": "model": {model}')
                prop_lines.append(f'"{name}": "model": "{CHEAP_MODEL_HINT}"')
            p = Patch(
                title="Point Hermes cron jobs at a cheap model",
                target=jobs_path,
                target_exists=os.path.exists(jobs_path),
                why=f"cron is {cron.cost / cost_total:.0%} of spend; scheduled maintenance rarely needs a frontier model.",
                impact=f"bulk of ≈{fmt_money(cron_monthly, a.cost_basis)}/mo moves to cheap-model pricing",
                current="\n".join(cur_lines) or "(no jobs parsed — open the file and check the `model` field per job)",
                proposed="\n".join(prop_lines)
                or f'set "model": "{CHEAP_MODEL_HINT}" per job (any cheap model)',
                notes=[
                    "field verified in hermes-agent cron/jobs.py: per-job `model` override",
                    "same file supports `enabled_toolsets`: restrict each job to the toolsets it needs — shrinks the tool-definitions overhead resent on every call",
                ],
            )
            patches.append(p)

        heavy_gw = {
            s: v for s, v in a.overhead_per_call.items() if s.startswith("gateway:") and v >= 12_000
        }
        if heavy_gw:
            gw = max(heavy_gw, key=heavy_gw.get)
            patches.append(
                Patch(
                    title=f"Trim the {gw.replace('gateway:', '')} toolset (fixed-overhead tax)",
                    target=os.path.join(root, "config.yaml"),
                    target_exists=os.path.exists(os.path.join(root, "config.yaml")),
                    why=f"{gw} carries {heavy_gw[gw]:,} input tokens per call — tool definitions + system prompt resent every message.",
                    impact="each removed toolset cuts every future call on that platform",
                    proposed=(
                        "in config.yaml, restrict per-platform toolsets for this gateway to what it actually uses\n"
                        "(platform-specific toolsets are a first-class Hermes feature; see your config.yaml `tools` section)"
                    ),
                    notes=["mechanism verified in hermes-agent gateway/run.py (_get_platform_tools)",
                           "the maintainer's own guidance in issue #4379"],
                )
            )

    if agent.startswith("openclaw"):
        hb = a.by_source.get("heartbeat")
        if hb:
            cfg_path = os.path.join(source_path, "openclaw.json")
            cfg = _read_json(cfg_path) or {}
            cur_hb = ((cfg.get("agents") or {}).get("defaults") or {}).get("heartbeat") or {}
            hb_monthly = monthly * (hb.cost / cost_total) if cost_total > 0 else None
            idle_note = ""
            if brep and brep.idle_heartbeats[0] > 0:
                idle_note = f"{brep.idle_heartbeats[0]}/{brep.idle_heartbeats[1]} heartbeat runs did nothing. "
            proposed = {
                "agents": {
                    "defaults": {
                        "heartbeat": {
                            "every": "60m",
                            "activeHours": {"start": "09:00", "end": "24:00"},
                            "model": CHEAP_MODEL_HINT,
                            "lightContext": True,
                        }
                    }
                }
            }
            patches.append(
                Patch(
                    title="Tame the OpenClaw heartbeat (interval, night window, cheap model, light context)",
                    target=cfg_path,
                    target_exists=os.path.exists(cfg_path),
                    why=idle_note
                    + (
                        f"heartbeat costs ≈{fmt_money(hb_monthly, a.cost_basis)}/mo at the current pace."
                        if hb_monthly
                        else "heartbeat runs around the clock with full bootstrap context."
                    ),
                    impact="idle burn → ~0: no night beats, half the frequency, cheap model, minimal context",
                    current=json.dumps({"heartbeat": cur_hb}, indent=2) if cur_hb else "(heartbeat: defaults — every 30m, full context, agent model)",
                    proposed=json.dumps(proposed, indent=2),
                    notes=[
                        "keys verified in openclaw config/types.agent-defaults.ts: heartbeat.every / activeHours / model / lightContext",
                        "per-agent overrides also exist if you only want to tame one agent",
                    ],
                )
            )

    return patches


def render_fixes(agent: str, patches: list, color: bool = True) -> str:
    b = (lambda s: f"\033[1m{s}\033[0m") if color else (lambda s: s)
    dim = (lambda s: f"\033[2m{s}\033[0m") if color else (lambda s: s)
    g = (lambda s: f"\033[32m{s}\033[0m") if color else (lambda s: s)
    out = ["", b(f"🔧 agentburn fix — {agent} · DRY-RUN (nothing was changed)")]
    out.append(dim("   ready-to-paste config changes for findings with a verified config lever"))
    out.append("")
    if not patches:
        out.append("   No applicable patches: current findings don't map to a verified config lever.")
        out.append(dim("   (Claude Code has no cost levers in local config; more generators are coming.)"))
        out.append("")
        return "\n".join(out)
    for i, p in enumerate(patches, 1):
        out.append(b(f"   {i}. {p.title}"))
        out.append(f"      file   : {p.target}" + ("" if p.target_exists else dim("  (not found on this machine — create/locate it)")))
        out.append(f"      why    : {p.why}")
        out.append(g(f"      effect : {p.impact}"))
        if p.current:
            out.append(dim("      current:"))
            out.extend(dim(f"        {l}") for l in p.current.splitlines())
        out.append("      proposed:")
        out.extend(f"        {l}" for l in p.proposed.splitlines())
        for n in p.notes:
            out.append(dim(f"      ⓘ {n}"))
        out.append("")
    out.append(dim("   Apply by hand, then prove it: agentburn --save-baseline → (paste changes) → agentburn --compare"))
    out.append(dim("   There is no --apply on purpose: it's your agent's config."))
    out.append("")
    return "\n".join(out)
