"""Codex CLI adapter: reads ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (read-only).

Layout observed in openai/codex 0.144 (September 2026), one JSONL per thread,
every line `{"timestamp", "type", "payload"}`:
  session_meta   once: cwd, originator ("codex_exec" CLI, "Codex Desktop"), cli_version
  turn_context   per turn: model, effort, cwd
  event_msg      payload.type == "token_count":
                   info.total_token_usage  — cumulative for the thread
                   info.last_token_usage   — the last request
                   rate_limits.primary/secondary — {used_percent, window_minutes, resets_at}
  response_item  function_call / function_call_output, custom_tool_call(_output)

Usage is taken as the DELTA of the cumulative counter, so a repeated
token_count (rate-limit refreshes re-send the same totals) counts once.
`input_tokens` includes `cached_input_tokens` (total = input + output).

Codex does not record costs locally and on a ChatGPT plan there is no honest
per-token price, so this adapter reports tokens and windows, never dollars —
same stance as the Claude Code adapter. What Codex *does* record that no other
agent does is the provider's own view of the window: `rate_limits.used_percent`
at the time of every request. Those samples are kept as RateLimitSample and
turn into a measured ceiling in `agentburn limits`.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import time
from typing import Optional

from .. import cache
from ..model import (
    BUCKET_SECONDS,
    ActionEvent,
    ContextCall,
    RateLimitSample,
    SessionRec,
    Snapshot,
    UsageCell,
)
from .hermes import salient_arg

CHARS_PER_TOKEN = 4
MAX_EVENTS_PER_FILE = 80_000


def default_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def available() -> bool:
    root = default_root()
    return os.path.isdir(root) and bool(glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")))


def _parse_ts(v) -> Optional[float]:
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _scan_file(path: str) -> dict:
    first = last = None
    calls = 0
    inp = out = cr = reasoning = 0
    model = None
    cwd = None
    originator = None
    lines = 0
    compactions = 0
    events: list = []
    cells: dict = {}
    contexts: list = []
    limits: list = []  # [ts, window_minutes, used_percent, resets_at]
    prev_total = None
    prev = None  # previous cumulative usage dict
    effort = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            lines += 1
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = _parse_ts(obj.get("timestamp"))
            if ts:
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
            kind = obj.get("type")
            p = obj.get("payload")
            if not isinstance(p, dict):
                continue
            if kind == "session_meta":
                cwd = cwd or p.get("cwd")
                originator = p.get("originator")
            elif kind == "turn_context":
                if p.get("model"):
                    model = p["model"]
                effort = p.get("effort") if isinstance(p.get("effort"), str) else effort
                cwd = p.get("cwd") or cwd
            elif kind == "compacted" or (kind == "event_msg" and p.get("type") == "context_compacted"):
                compactions += 1
            elif kind == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") or {}
                tot = info.get("total_token_usage") or {}
                total = int(tot.get("total_tokens") or 0)
                rl = p.get("rate_limits") or {}
                for key in ("primary", "secondary"):
                    w = rl.get(key)
                    if isinstance(w, dict) and w.get("used_percent") is not None and w.get("window_minutes") and ts:
                        limits.append([ts, int(w["window_minutes"]), float(w["used_percent"]),
                                       w.get("resets_at")])
                if total <= 0 or total == prev_total:
                    continue
                if prev_total is not None and total > prev_total and prev:
                    d_in = int(tot.get("input_tokens") or 0) - int(prev.get("input_tokens") or 0)
                    d_cr = int(tot.get("cached_input_tokens") or 0) - int(prev.get("cached_input_tokens") or 0)
                    d_out = int(tot.get("output_tokens") or 0) - int(prev.get("output_tokens") or 0)
                    d_rs = int(tot.get("reasoning_output_tokens") or 0) - int(prev.get("reasoning_output_tokens") or 0)
                else:
                    # first sample, or the counter reset (a new thread after compaction)
                    src = info.get("last_token_usage") or tot
                    d_in = int(src.get("input_tokens") or 0)
                    d_cr = int(src.get("cached_input_tokens") or 0)
                    d_out = int(src.get("output_tokens") or 0)
                    d_rs = int(src.get("reasoning_output_tokens") or 0)
                prev_total, prev = total, tot
                if d_in < 0 or d_out < 0:
                    continue
                d_cr = max(0, min(d_cr, d_in))
                calls += 1
                inp += d_in - d_cr
                cr += d_cr
                out += d_out
                reasoning += d_rs
                contexts.append([ts, model, d_in // 1000, d_out, effort])
                if ts:
                    key = (int(ts // BUCKET_SECONDS) * BUCKET_SECONDS, model)
                    c = cells.get(key)
                    if c is None:
                        cells[key] = [1, d_in - d_cr, d_out, d_cr, 0]
                    else:
                        c[0] += 1
                        c[1] += d_in - d_cr
                        c[2] += d_out
                        c[3] += d_cr
            elif kind == "response_item" and len(events) < MAX_EVENTS_PER_FILE:
                t = p.get("type")
                if t in ("function_call", "custom_tool_call"):
                    name = p.get("name") or "tool"
                    args = p.get("arguments") if t == "function_call" else p.get("input")
                    events.append([ts, str(name)[:40], salient_arg(args), None, None])
                elif t in ("function_call_output", "custom_tool_call_output"):
                    output = p.get("output")
                    text = output if isinstance(output, str) else json.dumps(output) if output is not None else ""
                    ok = None
                    if isinstance(output, dict) and "success" in output:
                        ok = bool(output.get("success"))
                    events.append([ts, "tool", None, ok, len(text) // CHARS_PER_TOKEN])
    return {
        "first": first, "last": last, "calls": calls, "inp": inp, "out": out, "cr": cr,
        "reasoning": reasoning, "model": model, "cwd": cwd, "originator": originator,
        "lines": lines, "compactions": compactions, "events": events,
        "cells": [[b, m] + v for (b, m), v in cells.items()],
        "contexts": contexts, "limits": limits,
    }


def load(
    db_path: Optional[str] = None,
    days: Optional[int] = 30,
    dumps_dir: Optional[str] = None,
    now: Optional[float] = None,
) -> Snapshot:
    root = db_path or default_root()
    if not os.path.isdir(root):
        raise FileNotFoundError(
            f"Codex sessions dir not found at {root}. Pass --db ~/.codex/sessions (or its actual location)."
        )
    now = now or time.time()
    since = now - days * 86400 if days else 0
    snap = Snapshot(agent="codex", source_path=root, generated_at=now, days=days)
    files = glob.glob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl"))
    cache.maybe_sweep("codex")
    empty = 0
    for path in files:
        try:
            if days and os.path.getmtime(path) < since:
                continue
            key = cache.stamp(path)
            scan = cache.get("codex", path, key)
            if scan is None:
                scan = _scan_file(path)
                cache.put("codex", path, key, scan)
        except OSError:
            continue
        if scan["lines"] == 0 or (days and scan["last"] is not None and scan["last"] < since):
            continue
        if scan["calls"] == 0 and not scan["events"]:
            empty += 1  # a thread that never got a model reply is not an accounting gap
            continue
        sid = os.path.basename(path)[len("rollout-"):-6]
        source = "desktop" if (scan.get("originator") or "").lower().startswith("codex desktop") else "cli"
        for ts, name, arg_key, ok, tokens in scan["events"]:
            snap.events.append(ActionEvent(session_id=sid, ts=ts, name=name, arg_key=arg_key, ok=ok, tokens=tokens))
        for bucket, m_, calls_, i_, o_, r_, w_ in scan["cells"]:
            if days and bucket < since:
                continue
            snap.usage_cells.append(UsageCell(start=bucket, source=source, model=m_, calls=calls_,
                                              input_tokens=i_, output_tokens=o_, cache_read_tokens=r_,
                                              cache_write_tokens=w_, session=sid))
        for ts, m_, ctx_k, o_, effort in scan.get("contexts", []):
            if days and ts is not None and ts < since:
                continue
            snap.context_calls.append(ContextCall(ts=ts, session=sid, model=m_, context=ctx_k * 1000,
                                                  output=o_, effort=effort))
        for ts, wm, used, resets in scan.get("limits", []):
            if days and ts < since:
                continue
            snap.rate_limits.append(RateLimitSample(ts=ts, window_minutes=wm, used_percent=used,
                                                    resets_at=float(resets) if resets else None))
        if scan["compactions"]:
            snap.compactions[sid] = scan["compactions"]
        cwd = scan.get("cwd")
        title = f"{os.path.basename((cwd or '').rstrip('/')) or 'thread'}/{sid[-8:]}"
        snap.sessions.append(SessionRec(
            id=sid, source=source, model=scan["model"], started_at=scan["first"], ended_at=scan["last"],
            parent_id=None, title=title[:80], api_calls=scan["calls"], input_tokens=scan["inp"],
            output_tokens=scan["out"], cache_read_tokens=scan["cr"], cache_write_tokens=0,
            reasoning_tokens=scan.get("reasoning", 0), cost_usd=None, cost_basis="unknown",
            message_count=scan["lines"], project=cwd,
        ))
    if not snap.sessions:
        raise RuntimeError(
            f"Codex rollouts found but nothing with usage in the window ({empty} empty thread(s)) — try --days 0."
        )
    snap.warnings.append(
        "Codex does not record costs locally; a ChatGPT plan has no honest per-token price — "
        "showing tokens and windows, not dollars."
    )
    if snap.rate_limits:
        snap.warnings.append(
            f"{len(snap.rate_limits):,} rate-limit samples recorded by Codex itself — "
            "`agentburn limits` measures your ceiling from them."
        )
    return snap
