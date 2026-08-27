"""Claude Code adapter: reads ~/.claude/projects/**.jsonl transcripts (read-only).

Layout (public, parsed by the wider ecosystem as well):
  ~/.claude/projects/<encoded-project-path>/<session-uuid>.jsonl     ← main session
  ~/.claude/projects/<proj>/<session-uuid>/subagents/agent-*.jsonl   ← subagents

Each line is a JSON object; assistant turns carry
  { "timestamp": ISO8601, "message": { "model": …, "usage": {
      input_tokens, output_tokens, cache_creation_input_tokens,
      cache_read_input_tokens } } }

Costs are NOT recorded locally by Claude Code, and subscription usage has no
honest per-token price — so this adapter reports tokens only (cost_basis
"unknown") rather than invent a number. That is deliberate.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import time
from typing import Optional

from .. import cache
from ..model import BUCKET_SECONDS, ActionEvent, SessionRec, Snapshot, UsageCell
from .hermes import salient_arg

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Claude Code records no per-result token count, so result weight is estimated
# from the text length at the usual ~4 chars/token. Char-proportional and
# labeled an estimate — same footing as the sampled input composition, and only
# ever used to rank findings against each other, never reported as a total.
CHARS_PER_TOKEN = 4

# Per-file guard on behavioural events: one runaway transcript should not be
# able to fill memory. Files hitting it are counted and reported, not hidden.
MAX_EVENTS_PER_FILE = 80_000

# Claude Code stamps turns it produced itself (interrupts, tool errors) with
# this in place of a model name. Not a model — do not report it as one.
SYNTHETIC_MODEL = "<synthetic>"


def _result_weight(content) -> int:
    """Approximate token weight of a tool result. 0 when nothing is measurable."""
    if isinstance(content, str):
        return len(content) // CHARS_PER_TOKEN
    if isinstance(content, list):
        chars = 0
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chars += len(block["text"])
            elif isinstance(block, str):
                chars += len(block)
        return chars // CHARS_PER_TOKEN
    return 0


def default_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def available() -> bool:
    root = default_root()
    if not os.path.isdir(root):
        return False
    for proj in os.scandir(root):
        if proj.is_dir():
            for f in os.scandir(proj.path):
                if f.name.endswith(".jsonl") and UUID_RE.match(f.name[:-6]):
                    return True
    return False


def _parse_ts(v) -> Optional[float]:
    if isinstance(v, (int, float)) and v > 0:
        return v / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _scan_file(path: str) -> dict:
    """Parse one transcript into a cacheable dict of plain values.

    Returns first/last timestamps, api_calls, usage sums, model, line count,
    compactions, plus compact rows for events and per-bucket usage. Plain lists
    rather than dataclasses, because this is exactly what gets cached.

    Also appends ActionEvents (tool_use with salient arg; tool_result error
    flags linked by tool_use_id), fills `cells` — (bucket, model) → usage, the
    intra-session resolution `agentburn limits` needs — and counts compactions — lines whose
    type/subtype mentions a compact boundary (anthropics/claude-code writes
    `subtype: "compact_boundary"`). Each compaction re-sends a near-full
    context window, so the count is a direct cost signal.
    """
    first = last = None
    calls = 0
    inp = out = cr = cw = 0
    model = None
    lines = 0
    compactions = 0
    no_ts = 0
    id_to_name = {}
    events: list = []
    cells: dict = {}
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
            ts = _parse_ts(obj.get("timestamp"))
            if ts:
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
            marker = f"{obj.get('type', '')}/{obj.get('subtype', '')}".lower()
            if "compact" in marker:
                compactions += 1
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if isinstance(u, dict):
                calls += 1
                i_ = int(u.get("input_tokens") or 0)
                o_ = int(u.get("output_tokens") or 0)
                w_ = int(u.get("cache_creation_input_tokens") or 0)
                r_ = int(u.get("cache_read_input_tokens") or 0)
                inp += i_
                out += o_
                cw += w_
                cr += r_
                if ts:
                    m_ = msg.get("model")
                    key = (
                        int(ts // BUCKET_SECONDS) * BUCKET_SECONDS,
                        None if m_ == SYNTHETIC_MODEL else m_,
                    )
                    c = cells.get(key)
                    if c is None:
                        cells[key] = [1, i_, o_, r_, w_]
                    else:
                        c[0] += 1
                        c[1] += i_
                        c[2] += o_
                        c[3] += r_
                        c[4] += w_
                else:
                    # No timestamp = no window. Counted so the report can say so
                    # instead of quietly under-reporting every window.
                    no_ts += 1
            if msg.get("model") and msg["model"] != SYNTHETIC_MODEL:
                model = msg["model"]
            content = msg.get("content")
            if isinstance(content, list) and len(events) < MAX_EVENTS_PER_FILE:
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_use" and item.get("name"):
                        if item.get("id"):
                            id_to_name[item["id"]] = item["name"]
                        events.append(
                            [ts, str(item["name"])[:40], salient_arg(item.get("input")), None, None]
                        )
                    elif item.get("type") == "tool_result":
                        name = id_to_name.get(item.get("tool_use_id"), "tool")
                        events.append(
                            [ts, str(name)[:40], None,
                             not bool(item.get("is_error")), _result_weight(item.get("content"))]
                        )
    return {
        "first": first,
        "last": last,
        "calls": calls,
        "inp": inp,
        "out": out,
        "cr": cr,
        "cw": cw,
        "model": model,
        "lines": lines,
        "compactions": compactions,
        "no_ts": no_ts,
        "events": events,
        "cells": [[b, m] + v for (b, m), v in cells.items()],
    }


def load(
    db_path: Optional[str] = None,
    days: Optional[int] = 30,
    dumps_dir: Optional[str] = None,  # unused; adapter interface parity
    now: Optional[float] = None,
) -> Snapshot:
    root = db_path or default_root()
    if not os.path.isdir(root):
        raise FileNotFoundError(
            f"Claude Code projects dir not found at {root}. Pass --db ~/.claude/projects "
            "(or its actual location)."
        )
    now = now or time.time()
    since = now - days * 86400 if days else 0

    snap = Snapshot(agent="claude-code", source_path=root, generated_at=now, days=days)

    mains = [
        p
        for p in glob.glob(os.path.join(root, "*", "*.jsonl"))
        if UUID_RE.match(os.path.basename(p)[:-6] or "")
    ]
    subs = glob.glob(os.path.join(root, "*", "*", "subagents", "*.jsonl"))

    stats = {"parsed": 0, "cached": 0, "no_ts": 0, "truncated": 0}

    def consider(path: str, source: str, parent: Optional[str], title: str):
        sid = os.path.basename(path)[:-6]
        try:
            if days and os.path.getmtime(path) < since:
                return
            key = cache.stamp(path)
            scan = cache.get("claude-code", path, key)
            if scan is None:
                scan = _scan_file(path)
                cache.put("claude-code", path, key, scan)
                stats["parsed"] += 1
            else:
                stats["cached"] += 1
        except OSError:
            return
        if scan["lines"] == 0:
            return
        last = scan["last"]
        if days and last is not None and last < since:
            return

        stats["no_ts"] += scan.get("no_ts", 0)
        if len(scan["events"]) >= MAX_EVENTS_PER_FILE:
            stats["truncated"] += 1
        for ts, name, arg_key, ok, tokens in scan["events"]:
            snap.events.append(
                ActionEvent(session_id=sid, ts=ts, name=name, arg_key=arg_key, ok=ok, tokens=tokens)
            )
        for bucket, cell_model, calls_, i_, o_, r_, w_ in scan["cells"]:
            if days and bucket < since:
                continue  # a long-lived file can carry buckets from before the window
            snap.usage_cells.append(
                UsageCell(
                    start=bucket,
                    source=source,
                    model=cell_model,
                    calls=calls_,
                    input_tokens=i_,
                    output_tokens=o_,
                    cache_read_tokens=r_,
                    cache_write_tokens=w_,
                )
            )
        if scan["compactions"]:
            snap.compactions[sid] = scan["compactions"]
        snap.sessions.append(
            SessionRec(
                id=sid,
                source=source,
                model=scan["model"],
                started_at=scan["first"],
                ended_at=last,
                parent_id=parent,
                title=title[:80],
                api_calls=scan["calls"],
                input_tokens=scan["inp"],
                output_tokens=scan["out"],
                cache_read_tokens=scan["cr"],
                cache_write_tokens=scan["cw"],
                reasoning_tokens=0,
                cost_usd=None,
                cost_basis="unknown",
                message_count=scan["lines"],
            )
        )

    cache.maybe_sweep("claude-code")

    for p in mains:
        project = os.path.basename(os.path.dirname(p)).strip("-").split("-")[-1] or "project"
        consider(p, "cli", None, f"{project}/{os.path.basename(p)[:8]}")
    for p in subs:
        session_uuid = os.path.basename(os.path.dirname(os.path.dirname(p)))
        consider(p, "subagent", session_uuid, f"subagent {os.path.basename(p)[:18]}")

    if not snap.sessions:
        raise RuntimeError(
            "Claude Code transcripts found but nothing parsed in the window — "
            "try --days 0, or open an issue if the JSONL schema changed."
        )
    usable = sum(1 for s in snap.sessions if s.total_tokens > 0)
    if usable == 0:
        snap.warnings.append(
            "no usage fields found in any transcript — Claude Code schema may have changed; "
            "counts below are line counts only."
        )
    snap.warnings.append(
        "Claude Code does not record costs locally; subscription usage has no honest per-token "
        "price — showing tokens, not dollars."
    )
    total_calls = sum(s_.api_calls for s_ in snap.sessions)
    if stats["no_ts"] and total_calls and stats["no_ts"] / total_calls >= 0.005:
        # Calls without a timestamp are counted in totals but cannot be placed
        # in a window — say so rather than let `limits` under-report silently.
        snap.warnings.append(
            f"{stats['no_ts']:,} of {total_calls:,} API calls carry no timestamp: they are in the "
            "totals but not in any window, so `agentburn limits` is a lower bound."
        )
    if stats["truncated"]:
        snap.warnings.append(
            f"{stats['truncated']} transcript(s) hit the {MAX_EVENTS_PER_FILE:,}-event per-file cap; "
            "`agentburn why` sees the beginning of those sessions only."
        )
    return snap
