"""Codex CLI adapter: reads ~/.codex/sessions/**/*.jsonl transcripts read-only.

Observed Codex records are append-only JSONL files:
  session_meta / turn_context lines carry session metadata and the model
  response_item:function_call and function_call_output lines carry tool use
  event_msg:token_count lines carry per-response usage counters

Codex records tokens but no local cost. The adapter reports token totals and
subscription-style windows, and deliberately never invents dollars.
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

CHARS_PER_TOKEN = 4
MAX_EVENTS_PER_FILE = 80_000

GATEWAY_SOURCES = {
    "telegram",
    "whatsapp",
    "discord",
    "slack",
    "signal",
    "imessage",
    "email",
    "web",
    "botmux",
}


def default_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".codex", "sessions")


def _paths(root: str) -> list[str]:
    if root.endswith(".jsonl"):
        return [root] if os.path.exists(root) else []
    return sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))


def available() -> bool:
    root = default_root()
    if not os.path.isdir(root):
        return False
    for _, _, names in os.walk(root):
        if any(name.endswith(".jsonl") for name in names):
            return True
    return False


def _parse_ts(value) -> Optional[float]:
    if isinstance(value, (int, float)) and value > 0:
        return value / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _source(*values) -> str:
    text = " ".join(str(v).lower() for v in values if v)
    if "subagent" in text:
        return "subagent"
    if any(k in text for k in ("cron", "schedule", "scheduled")):
        return "cron"
    for name in GATEWAY_SOURCES:
        if name in text:
            return f"gateway:{name}"
    return "cli"


def _result_weight(value) -> int:
    if isinstance(value, str):
        return len(value) // CHARS_PER_TOKEN
    if isinstance(value, dict):
        return len(json.dumps(value, separators=(",", ":"))) // CHARS_PER_TOKEN
    return 0


def _looks_error(payload: dict, output) -> bool:
    if isinstance(payload.get("is_error"), bool):
        return bool(payload["is_error"])
    status = str(payload.get("status") or "").lower()
    if status in ("error", "failed", "failure"):
        return True
    if isinstance(output, str):
        return bool(re.search(r"(?:exit|return) code[:= ]+[1-9]", output, re.I))
    return False


def _title(meta: dict, path: str, sid: str) -> str:
    cwd = meta.get("cwd")
    if isinstance(cwd, str) and cwd:
        return os.path.basename(cwd.rstrip(os.sep)) or cwd[:80]
    return sid or os.path.basename(path)[:80]


def _scan_file(path: str) -> dict:
    first = last = None
    calls = 0
    inp = out = cr = cw = reasoning = 0
    model = None
    lines = 0
    messages = 0
    compactions = 0
    no_ts = 0
    meta: dict = {}
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
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            kind = str(obj.get("type") or "")
            ptype = str(payload.get("type") or "")
            ts = _parse_ts(obj.get("timestamp")) or _parse_ts(payload.get("timestamp"))
            if ts:
                first = ts if first is None else min(first, ts)
                last = ts if last is None else max(last, ts)
            marker = f"{kind}/{ptype}/{payload.get('subtype', '')}".lower()
            if "compact" in marker:
                compactions += 1

            if kind == "session_meta":
                meta = payload
                continue
            if kind == "turn_context" and isinstance(payload.get("model"), str):
                model = payload["model"]
                continue
            if ptype in ("message", "user_message", "agent_message"):
                messages += 1

            if ptype == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict):
                    usage = {}
                i_ = _int(usage.get("input_tokens"))
                raw_o = _int(usage.get("output_tokens"))
                r_ = _int(usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens"))
                w_ = _int(
                    usage.get("cache_write_input_tokens")
                    or usage.get("cache_creation_input_tokens")
                )
                reason_ = _int(usage.get("reasoning_output_tokens") or usage.get("reasoning_tokens"))
                o_ = max(raw_o - reason_, 0)
                if i_ + raw_o + r_ + w_ + reason_ > 0:
                    calls += 1
                    inp += i_
                    out += o_
                    cr += r_
                    cw += w_
                    reasoning += reason_
                    if ts:
                        key = (int(ts // BUCKET_SECONDS) * BUCKET_SECONDS, model)
                        c = cells.get(key)
                        if c is None:
                            cells[key] = [1, i_, raw_o, r_, w_]
                        else:
                            c[0] += 1
                            c[1] += i_
                            c[2] += raw_o
                            c[3] += r_
                            c[4] += w_
                    else:
                        no_ts += 1
                continue

            if len(events) >= MAX_EVENTS_PER_FILE:
                continue
            if ptype in ("function_call", "local_shell_call"):
                name = payload.get("name") or ("shell" if ptype == "local_shell_call" else "tool")
                call_id = payload.get("call_id") or payload.get("id")
                if call_id:
                    id_to_name[str(call_id)] = str(name)
                events.append(
                    [ts, str(name)[:40], salient_arg(payload.get("arguments")), None, None]
                )
            elif ptype in ("function_call_output", "local_shell_call_output"):
                call_id = payload.get("call_id") or payload.get("id")
                name = id_to_name.get(str(call_id), "tool")
                output = payload.get("output")
                events.append(
                    [ts, str(name)[:40], None, not _looks_error(payload, output), _result_weight(output)]
                )

    sid = str(meta.get("session_id") or meta.get("id") or os.path.splitext(os.path.basename(path))[0])
    return {
        "sid": sid,
        "source": _source(meta.get("source"), meta.get("thread_source"), meta.get("originator")),
        "title": _title(meta, path, sid),
        "first": first,
        "last": last,
        "calls": calls,
        "inp": inp,
        "out": out,
        "cr": cr,
        "cw": cw,
        "reasoning": reasoning,
        "model": model,
        "lines": lines,
        "messages": messages,
        "compactions": compactions,
        "no_ts": no_ts,
        "events": events,
        "cells": [[b, m] + v for (b, m), v in cells.items()],
        "truncated": len(events) >= MAX_EVENTS_PER_FILE,
    }


def load(
    db_path: Optional[str] = None,
    days: Optional[int] = 30,
    dumps_dir: Optional[str] = None,  # unused; adapter interface parity
    now: Optional[float] = None,
) -> Snapshot:
    root = db_path or default_root()
    paths = _paths(root)
    if not paths:
        raise FileNotFoundError(
            f"Codex sessions not found under {root}. Pass --db ~/.codex/sessions "
            "(or a specific JSONL session path)."
        )
    now = now or time.time()
    since = now - days * 86400 if days else 0

    snap = Snapshot(agent="codex", source_path=root, generated_at=now, days=days)
    stats = {"parsed": 0, "cached": 0, "no_ts": 0, "truncated": 0}

    for path in paths:
        try:
            if days and os.path.getmtime(path) < since:
                continue
            key = cache.stamp(path)
            scan = cache.get("codex", path, key)
            if scan is None:
                scan = _scan_file(path)
                cache.put("codex", path, key, scan)
                stats["parsed"] += 1
            else:
                stats["cached"] += 1
        except OSError:
            continue
        if scan["lines"] == 0:
            continue
        if days and scan["last"] is not None and scan["last"] < since:
            continue

        stats["no_ts"] += scan.get("no_ts", 0)
        stats["truncated"] += 1 if scan.get("truncated") else 0
        for ts, name, arg_key, ok, tokens in scan["events"]:
            snap.events.append(
                ActionEvent(session_id=scan["sid"], ts=ts, name=name, arg_key=arg_key, ok=ok, tokens=tokens)
            )
        for bucket, cell_model, calls_, i_, raw_o, r_, w_ in scan["cells"]:
            if days and bucket < since:
                continue
            snap.usage_cells.append(
                UsageCell(
                    start=bucket,
                    source=scan["source"],
                    model=cell_model,
                    calls=calls_,
                    input_tokens=i_,
                    output_tokens=raw_o,
                    cache_read_tokens=r_,
                    cache_write_tokens=w_,
                )
            )
        if scan["compactions"]:
            snap.compactions[scan["sid"]] = scan["compactions"]
        snap.sessions.append(
            SessionRec(
                id=scan["sid"],
                source=scan["source"],
                model=scan["model"],
                started_at=scan["first"],
                ended_at=scan["last"],
                parent_id=None,
                title=scan["title"][:80],
                api_calls=scan["calls"],
                input_tokens=scan["inp"],
                output_tokens=scan["out"],
                cache_read_tokens=scan["cr"],
                cache_write_tokens=scan["cw"],
                reasoning_tokens=scan["reasoning"],
                cost_usd=None,
                cost_basis="unknown",
                message_count=scan["messages"] or scan["lines"],
            )
        )

    cache.maybe_sweep("codex")

    if not snap.sessions:
        raise RuntimeError(
            "Codex sessions found but nothing parsed in the window — try --days 0, "
            "or open an issue if the JSONL schema changed."
        )
    usable = sum(1 for s in snap.sessions if s.total_tokens > 0)
    if usable == 0:
        snap.warnings.append(
            "no usage fields found in any Codex transcript — schema may have changed; "
            "counts below are line counts only."
        )
    snap.warnings.append("Codex CLI does not record costs locally — showing tokens, not dollars.")
    total_calls = sum(s.api_calls for s in snap.sessions)
    if stats["no_ts"] and total_calls and stats["no_ts"] / total_calls >= 0.005:
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
