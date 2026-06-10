"""Hermes Agent adapter: reads ~/.hermes/state.db (SQLite) read-only.

Schema observed in NousResearch/hermes-agent `hermes_state.py` (June 2026):
  sessions(id, source, model, parent_session_id, started_at, ended_at,
           message_count, tool_call_count, api_call_count,
           input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
           reasoning_tokens, estimated_cost_usd, actual_cost_usd, cost_status,
           title, archived, ...)
  messages(session_id, role, tool_name, tool_calls, timestamp, token_count, ...)

Known upstream accounting gaps (hermes-agent #12023, #6775, #8337): some
providers/streams record zero tokens. We DETECT and REPORT those gaps instead
of silently presenting totals as truth.

Optional precision layer: request_dump_*.json files (written when request
dumping is enabled) contain the full API body; we sample them to estimate the
input composition (system prompt vs tool definitions vs history).
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
import time
from typing import Optional

from ..model import DumpComposition, SessionRec, Snapshot, ToolStat

GATEWAY_SOURCES = {
    "telegram",
    "whatsapp",
    "discord",
    "slack",
    "signal",
    "imessage",
    "email",
    "api",
    "api_server",
    "web",
}


def default_db_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".hermes", "state.db")


def available() -> bool:
    return os.path.exists(default_db_path())


def normalize_source(raw: Optional[str]) -> str:
    s = (raw or "unknown").strip().lower()
    if s in ("cli", "cron", "subagent"):
        return s
    if s in GATEWAY_SOURCES:
        return f"gateway:{s}"
    if s.startswith(("gateway:", "other:")):
        return s
    return f"other:{s}"


def _columns(con: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _col(cols: set, name: str, default_sql: str = "NULL") -> str:
    return name if name in cols else f"{default_sql} AS {name}"


def load(
    db_path: Optional[str] = None,
    days: Optional[int] = 30,
    dumps_dir: Optional[str] = None,
    now: Optional[float] = None,
) -> Snapshot:
    path = db_path or default_db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Hermes state not found at {path}. Pass --db /path/to/state.db "
            "or run on the machine where Hermes Agent lives."
        )
    now = now or time.time()
    since = now - days * 86400 if days else 0

    snap = Snapshot(
        agent="hermes",
        source_path=path,
        generated_at=now,
        days=days,
    )

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        scols = _columns(con, "sessions")
        if "id" not in scols:
            raise RuntimeError(
                "sessions table not found — is this really a Hermes state.db? "
                "(schema may have changed; please open an issue with `PRAGMA table_info(sessions)` output)"
            )

        fields = ", ".join(
            [
                "id",
                _col(scols, "source", "'unknown'"),
                _col(scols, "model"),
                _col(scols, "parent_session_id"),
                _col(scols, "started_at"),
                _col(scols, "ended_at"),
                _col(scols, "title"),
                _col(scols, "message_count", "0"),
                _col(scols, "api_call_count", "0"),
                _col(scols, "input_tokens", "0"),
                _col(scols, "output_tokens", "0"),
                _col(scols, "cache_read_tokens", "0"),
                _col(scols, "cache_write_tokens", "0"),
                _col(scols, "reasoning_tokens", "0"),
                _col(scols, "estimated_cost_usd"),
                _col(scols, "actual_cost_usd"),
                _col(scols, "billing_provider"),
            ]
        )
        where = "WHERE COALESCE(started_at, 0) >= ?" if days else ""
        rows = con.execute(
            f"SELECT {fields} FROM sessions {where}", (since,) if days else ()
        ).fetchall()

        for r in rows:
            actual = r["actual_cost_usd"]
            est = r["estimated_cost_usd"]
            cost, basis = (
                (actual, "actual")
                if actual is not None
                else (est, "estimated")
                if est is not None
                else (None, "unknown")
            )
            snap.sessions.append(
                SessionRec(
                    id=str(r["id"]),
                    source=normalize_source(r["source"]),
                    model=r["model"],
                    started_at=r["started_at"],
                    ended_at=r["ended_at"],
                    parent_id=r["parent_session_id"],
                    title=r["title"],
                    api_calls=int(r["api_call_count"] or 0),
                    input_tokens=int(r["input_tokens"] or 0),
                    output_tokens=int(r["output_tokens"] or 0),
                    cache_read_tokens=int(r["cache_read_tokens"] or 0),
                    cache_write_tokens=int(r["cache_write_tokens"] or 0),
                    reasoning_tokens=int(r["reasoning_tokens"] or 0),
                    cost_usd=float(cost) if cost is not None else None,
                    cost_basis=basis,
                    message_count=int(r["message_count"] or 0),
                    provider=r["billing_provider"],
                )
            )

        mcols = _columns(con, "messages")
        if {"tool_name", "timestamp"} <= mcols:
            tok = "COALESCE(token_count, 0)" if "token_count" in mcols else "0"
            mwhere = "AND timestamp >= ?" if days else ""
            for r in con.execute(
                f"""SELECT tool_name, COUNT(*) AS calls, SUM({tok}) AS toks
                    FROM messages
                    WHERE tool_name IS NOT NULL AND tool_name != '' {mwhere}
                    GROUP BY tool_name ORDER BY toks DESC""",
                (since,) if days else (),
            ):
                snap.tools.append(
                    ToolStat(name=r["tool_name"], calls=int(r["calls"]), result_tokens=int(r["toks"] or 0))
                )
    finally:
        con.close()

    comp = _sample_dumps(dumps_dir or os.path.join(os.path.dirname(path), "sessions"))
    if comp:
        snap.composition = comp
    return snap


def _sample_dumps(dumps_dir: str, limit: int = 20) -> Optional[DumpComposition]:
    """Estimate input composition from request dumps (newest `limit` files).

    Char-proportional split of the request body: system prompt vs tool
    definitions vs message history. Proportions, not exact tokens — labeled
    as sampled estimate in the report.
    """
    try:
        files = sorted(glob.glob(os.path.join(dumps_dir, "request_dump_*.json")))[-limit:]
    except OSError:
        return None
    if not files:
        return None
    sys_c = tools_c = hist_c = 0
    samples = 0
    for f in files:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        body = payload.get("body") or payload.get("request") or payload
        if not isinstance(body, dict):
            continue
        msgs = body.get("messages") or []
        tools = body.get("tools") or []
        s = t = h = 0
        if isinstance(body.get("system"), str):
            s += len(body["system"])
        for m in msgs if isinstance(msgs, list) else []:
            chunk = len(json.dumps(m, ensure_ascii=False, default=str))
            if isinstance(m, dict) and m.get("role") == "system":
                s += chunk
            else:
                h += chunk
        t = len(json.dumps(tools, ensure_ascii=False, default=str)) if tools else 0
        total = s + t + h
        if total <= 0:
            continue
        sys_c += s
        tools_c += t
        hist_c += h
        samples += 1
    total = sys_c + tools_c + hist_c
    if samples == 0 or total == 0:
        return None
    return DumpComposition(
        samples=samples,
        system_share=sys_c / total,
        tools_share=tools_c / total,
        history_share=hist_c / total,
    )
