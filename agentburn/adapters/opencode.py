"""opencode adapter: reads ~/.local/share/opencode/opencode.db (SQLite, read-only).

Schema observed in sst/opencode (September 2026):
  session(id, parent_id, directory, title, model, cost, tokens_*, time_created ms)
  message(id, session_id, time_created ms, data JSON) — assistant rows carry
    role, modelID, providerID, cost, path.cwd, tokens{input, output, reasoning,
    cache{read, write}}, time{created, completed}
  part(id, message_id, session_id, data JSON) — {"type": "tool", "tool", "state":
    {"status", "input", "output"}}

opencode records its own cost per message (from the provider's price list it
ships), so `cost_usd` is the agent's number, basis "actual" when non-zero.
Free/self-hosted providers leave it at 0 → basis "unknown", tokens only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional

from ..model import BUCKET_SECONDS, ActionEvent, ContextCall, SessionRec, Snapshot, UsageCell

CHARS_PER_TOKEN = 4


def default_root() -> str:
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(xdg, "opencode", "opencode.db")


def available() -> bool:
    return os.path.isfile(default_root())


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load(
    db_path: Optional[str] = None,
    days: Optional[int] = 30,
    dumps_dir: Optional[str] = None,
    now: Optional[float] = None,
) -> Snapshot:
    path = db_path or default_root()
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"opencode database not found at {path}. Pass --db ~/.local/share/opencode/opencode.db."
        )
    now = now or time.time()
    since_ms = int((now - days * 86400) * 1000) if days else 0
    snap = Snapshot(agent="opencode", source_path=path, generated_at=now, days=days)
    try:
        con = _connect(path)
    except sqlite3.Error as e:
        raise RuntimeError(f"cannot open opencode database: {e}") from None
    with con:
        sessions = {
            r["id"]: r for r in con.execute(
                "SELECT id, parent_id, directory, title, model, time_created, time_updated FROM session "
                "WHERE time_updated >= ?", (since_ms,)
            )
        }
        if not sessions:
            raise RuntimeError("opencode database has no sessions in the window — try --days 0.")
        agg: dict = {}
        priced = 0
        for r in con.execute(
            "SELECT session_id, time_created, data FROM message WHERE time_created >= ? ORDER BY time_created",
            (since_ms,),
        ):
            sid = r["session_id"]
            if sid not in sessions:
                continue
            try:
                d = json.loads(r["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(d, dict) or d.get("role") != "assistant":
                continue
            t = d.get("tokens") or {}
            if not isinstance(t, dict):
                continue
            cache_ = t.get("cache") or {}
            i_ = int(t.get("input") or 0)
            o_ = int(t.get("output") or 0)
            rs = int(t.get("reasoning") or 0)
            cr = int(cache_.get("read") or 0)
            cw = int(cache_.get("write") or 0)
            ts = (r["time_created"] or 0) / 1000.0
            model = d.get("modelID")
            provider = d.get("providerID")
            cost = float(d.get("cost") or 0)
            a = agg.setdefault(sid, {"calls": 0, "inp": 0, "out": 0, "cr": 0, "cw": 0, "rs": 0, "cost": 0.0,
                                     "model": None, "provider": None, "first": None, "last": None})
            a["calls"] += 1
            a["inp"] += i_
            a["out"] += o_
            a["cr"] += cr
            a["cw"] += cw
            a["rs"] += rs
            a["cost"] += cost
            if cost > 0:
                priced += 1
            a["model"] = f"{provider}/{model}" if provider and model and "/" not in str(model) else model
            a["provider"] = provider
            a["first"] = ts if a["first"] is None else min(a["first"], ts)
            a["last"] = ts if a["last"] is None else max(a["last"], ts)
            source = "subagent" if sessions[sid]["parent_id"] else "cli"
            snap.usage_cells.append(UsageCell(
                start=int(ts // BUCKET_SECONDS) * BUCKET_SECONDS, source=source, model=a["model"],
                calls=1, input_tokens=i_, output_tokens=o_ + rs, cache_read_tokens=cr,
                cache_write_tokens=cw, session=sid,
            ))
            snap.context_calls.append(ContextCall(ts=ts, session=sid, model=a["model"],
                                                  context=i_ + cr + cw, output=o_ + rs))
        for r in con.execute(
            "SELECT session_id, time_created, data FROM part WHERE time_created >= ?", (since_ms,)
        ):
            if r["session_id"] not in sessions:
                continue
            try:
                d = json.loads(r["data"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(d, dict) or d.get("type") != "tool":
                continue
            state = d.get("state") or {}
            name = str(d.get("tool") or "tool")[:40]
            ts = (r["time_created"] or 0) / 1000.0
            from .hermes import salient_arg

            snap.events.append(ActionEvent(session_id=r["session_id"], ts=ts, name=name,
                                           arg_key=salient_arg(state.get("input"))))
            status = str(state.get("status") or "")
            out_text = state.get("output")
            snap.events.append(ActionEvent(
                session_id=r["session_id"], ts=ts, name=name,
                ok=None if not status else status not in ("error", "failed"),
                tokens=len(out_text) // CHARS_PER_TOKEN if isinstance(out_text, str) else None,
            ))
    for sid, s in sessions.items():
        a = agg.get(sid)
        if a is None:
            continue
        cost_known = a["cost"] > 0
        snap.sessions.append(SessionRec(
            id=sid, source="subagent" if s["parent_id"] else "cli", model=a["model"],
            started_at=a["first"] or (s["time_created"] or 0) / 1000.0, ended_at=a["last"],
            parent_id=s["parent_id"], title=(s["title"] or sid)[:80], api_calls=a["calls"],
            input_tokens=a["inp"], output_tokens=a["out"], cache_read_tokens=a["cr"],
            cache_write_tokens=a["cw"], reasoning_tokens=a["rs"],
            cost_usd=a["cost"] if cost_known else None, cost_basis="actual" if cost_known else "unknown",
            message_count=a["calls"], provider=a["provider"], project=s["directory"],
        ))
    if not snap.sessions:
        raise RuntimeError("opencode database has sessions but no assistant messages in the window.")
    unpriced = sum(1 for s in snap.sessions if s.cost_basis == "unknown")
    if unpriced:
        snap.warnings.append(
            f"{unpriced} of {len(snap.sessions)} sessions carry no cost from opencode (free or "
            "self-hosted provider) — shown as tokens only."
        )
    return snap
