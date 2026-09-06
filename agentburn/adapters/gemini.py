"""Gemini CLI adapter: reads ~/.gemini/tmp/<project>/chats/session-*.json (read-only).

One JSON document per session (google-gemini/gemini-cli, chatRecordingService):
  {sessionId, projectHash, startTime, lastUpdated, kind, messages: [...]}
Messages of type "gemini" carry `model`, `timestamp`, `toolCalls` and
  tokens: {input, output, cached, thoughts, tool, total}
already per turn (total = input + output + thoughts + tool; `input` includes
`cached`). Files are written without any telemetry configuration, unlike the
OTEL export, so they are the primary source.

The directory under ~/.gemini/tmp is a label from ~/.gemini/projects.json
(path → label), which is how a session gets its working directory back.

No local costs, and Gemini CLI is mostly used on a free tier or a Google
subscription: tokens and windows only, no invented dollars.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import time
from typing import Optional

from .. import cache
from ..model import BUCKET_SECONDS, ActionEvent, ContextCall, SessionRec, Snapshot, UsageCell
from .hermes import salient_arg

CHARS_PER_TOKEN = 4


def default_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".gemini", "tmp")


def available() -> bool:
    root = default_root()
    return os.path.isdir(root) and bool(glob.glob(os.path.join(root, "*", "chats", "session-*.json")))


def _parse_ts(v) -> Optional[float]:
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _projects(root: str) -> dict:
    """label → path, from ~/.gemini/projects.json next to the tmp dir."""
    try:
        with open(os.path.join(os.path.dirname(root), "projects.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for path, label in (data.get("projects") or {}).items():
        out.setdefault(label, path)
    return out


def _scan_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        try:
            doc = json.load(f)
        except json.JSONDecodeError:
            return {"lines": 0}
    if not isinstance(doc, dict):
        return {"lines": 0}
    msgs = doc.get("messages") or []
    first = _parse_ts(doc.get("startTime"))
    last = _parse_ts(doc.get("lastUpdated"))
    calls = inp = out = cr = thoughts = 0
    model = None
    events: list = []
    cells: dict = {}
    contexts: list = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        ts = _parse_ts(m.get("timestamp"))
        if ts:
            first = ts if first is None else min(first, ts)
            last = ts if last is None else max(last, ts)
        if m.get("type") != "gemini":
            continue
        if m.get("model"):
            model = m["model"]
        t = m.get("tokens")
        if isinstance(t, dict):
            i_ = int(t.get("input") or 0)
            c_ = max(0, min(int(t.get("cached") or 0), i_))
            o_ = int(t.get("output") or 0)
            th = int(t.get("thoughts") or 0)
            calls += 1
            inp += i_ - c_
            cr += c_
            out += o_
            thoughts += th
            contexts.append([ts, m.get("model"), i_ // 1000, o_ + th, None])
            if ts:
                key = (int(ts // BUCKET_SECONDS) * BUCKET_SECONDS, m.get("model"))
                c = cells.get(key)
                if c is None:
                    cells[key] = [1, i_ - c_, o_ + th, c_, 0]
                else:
                    c[0] += 1
                    c[1] += i_ - c_
                    c[2] += o_ + th
                    c[3] += c_
        for tc in m.get("toolCalls") or []:
            if not isinstance(tc, dict):
                continue
            name = str(tc.get("name") or "tool")[:40]
            events.append([ts, name, salient_arg(tc.get("args")), None, None])
            res = tc.get("result")
            text = json.dumps(res) if res is not None else ""
            ok = None
            if isinstance(tc.get("status"), str):
                ok = tc["status"].lower() not in ("error", "failed", "cancelled")
            events.append([ts, name, None, ok, len(text) // CHARS_PER_TOKEN])
    return {
        "lines": len(msgs), "first": first, "last": last, "calls": calls, "inp": inp, "out": out,
        "cr": cr, "thoughts": thoughts, "model": model, "kind": doc.get("kind"),
        "session_id": doc.get("sessionId"), "events": events,
        "cells": [[b, m] + v for (b, m), v in cells.items()], "contexts": contexts,
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
            f"Gemini CLI dir not found at {root}. Pass --db ~/.gemini/tmp (or its actual location)."
        )
    now = now or time.time()
    since = now - days * 86400 if days else 0
    snap = Snapshot(agent="gemini", source_path=root, generated_at=now, days=days)
    projects = _projects(root)
    cache.maybe_sweep("gemini")
    empty = 0
    for path in glob.glob(os.path.join(root, "*", "chats", "session-*.json")):
        try:
            if days and os.path.getmtime(path) < since:
                continue
            key = cache.stamp(path)
            scan = cache.get("gemini", path, key)
            if scan is None:
                scan = _scan_file(path)
                cache.put("gemini", path, key, scan)
        except OSError:
            continue
        if scan["lines"] == 0 or (days and scan.get("last") is not None and scan["last"] < since):
            continue
        if scan["calls"] == 0 and not scan["events"]:
            empty += 1  # a chat with no model reply is not an accounting gap
            continue
        label = os.path.basename(os.path.dirname(os.path.dirname(path)))
        cwd = projects.get(label)
        sid = scan.get("session_id") or os.path.basename(path)[8:-5]
        source = "cli" if (scan.get("kind") or "main") == "main" else "subagent"
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
        snap.sessions.append(SessionRec(
            id=sid, source=source, model=scan["model"], started_at=scan["first"], ended_at=scan["last"],
            parent_id=None, title=f"{label}/{sid[:8]}", api_calls=scan["calls"],
            input_tokens=scan["inp"], output_tokens=scan["out"], cache_read_tokens=scan["cr"],
            cache_write_tokens=0, reasoning_tokens=scan.get("thoughts", 0), cost_usd=None,
            cost_basis="unknown", message_count=scan["lines"], project=cwd,
        ))
    if not snap.sessions:
        raise RuntimeError(
            f"Gemini CLI sessions found but nothing with usage in the window ({empty} empty chat(s)) — try --days 0."
        )
    snap.warnings.append(
        "Gemini CLI does not record costs locally; showing tokens and windows, not dollars."
    )
    return snap
