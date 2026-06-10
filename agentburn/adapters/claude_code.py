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

from ..model import SessionRec, Snapshot

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


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


def _scan_file(path: str):
    """→ (first_ts, last_ts, api_calls, usage sums, model, lines) — tolerant."""
    first = last = None
    calls = 0
    inp = out = cr = cw = 0
    model = None
    lines = 0
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
            msg = obj.get("message")
            if isinstance(msg, dict):
                u = msg.get("usage")
                if isinstance(u, dict):
                    calls += 1
                    inp += int(u.get("input_tokens") or 0)
                    out += int(u.get("output_tokens") or 0)
                    cw += int(u.get("cache_creation_input_tokens") or 0)
                    cr += int(u.get("cache_read_input_tokens") or 0)
                if msg.get("model"):
                    model = msg["model"]
    return first, last, calls, inp, out, cr, cw, model, lines


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

    def consider(path: str, source: str, parent: Optional[str], title: str):
        try:
            if days and os.path.getmtime(path) < since:
                return
            first, last, calls, inp, out, cr, cw, model, lines = _scan_file(path)
        except OSError:
            return
        if lines == 0:
            return
        if days and last is not None and last < since:
            return
        sid = os.path.basename(path)[:-6]
        snap.sessions.append(
            SessionRec(
                id=sid,
                source=source,
                model=model,
                started_at=first,
                ended_at=last,
                parent_id=parent,
                title=title[:80],
                api_calls=calls,
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                reasoning_tokens=0,
                cost_usd=None,
                cost_basis="unknown",
                message_count=lines,
            )
        )

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
    return snap
