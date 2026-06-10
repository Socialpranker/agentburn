#!/usr/bin/env python3
"""Offline self-test: builds a synthetic Hermes state.db and runs the full
pipeline (adapter → analyze → recommend → render). No network, no real data.

Run: python tests/selftest.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agentburn.adapters import hermes  # noqa: E402
from agentburn.analyze import analyze  # noqa: E402
from agentburn.recommend import recommend  # noqa: E402
from agentburn.report import fmt_tokens, render_json, render_terminal  # noqa: E402

PASSED = 0


def ok(name: str, cond: bool, extra: str = ""):
    global PASSED
    if not cond:
        print(f"  ✗ {name} {extra}")
        raise SystemExit(1)
    PASSED += 1
    print(f"  ✓ {name}")


def night_ts(days_ago: float, hour: float) -> float:
    """A timestamp `days_ago` days back at local `hour` (fractional ok)."""
    t = time.localtime(time.time() - days_ago * 86400)
    h = int(hour)
    m = int(round((hour - h) * 60)) or 30
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))


def build_db(path: str):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, model TEXT,
            model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
            started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
            message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0, cwd TEXT, billing_provider TEXT,
            billing_base_url TEXT, billing_mode TEXT, estimated_cost_usd REAL,
            actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,
            pricing_version TEXT, title TEXT, api_call_count INTEGER DEFAULT 0,
            handoff_state TEXT, handoff_platform TEXT, handoff_error TEXT,
            rewind_count INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT,
            tool_name TEXT, timestamp REAL NOT NULL, token_count INTEGER,
            finish_reason TEXT, reasoning TEXT, reasoning_content TEXT,
            reasoning_details TEXT, codex_reasoning_items TEXT,
            codex_message_items TEXT, platform_message_id TEXT,
            observed INTEGER DEFAULT 0, active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    S = (
        "INSERT INTO sessions (id, source, model, parent_session_id, started_at, title, "
        "message_count, api_call_count, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    rows = [
        # nightly cron on an expensive model — the villain (3:30, multiple days back)
        ("cron1", "cron", "anthropic/claude-opus-x", None, night_ts(2, 3), "nightly digest",
         12, 40, 600_000, 20_000, 0, 0, 0, 18.0, None),
        ("cron2", "cron", "anthropic/claude-opus-x", None, night_ts(1, 3), "nightly digest",
         12, 40, 600_000, 20_000, 0, 0, 0, 18.0, None),
        # daytime CLI session, actual cost, light overhead
        ("cli1", "cli", "deepseek/deepseek-v3", None, night_ts(1, 14), "refactor",
         30, 20, 100_000, 30_000, 50_000, 5_000, 0, None, 4.0),
        # telegram gateway: heavy per-call input (bootstrap resend pattern)
        ("tg1", "telegram", "deepseek/deepseek-v3", None, night_ts(1, 12), "tg chat",
         40, 10, 200_000, 10_000, 0, 0, 0, 3.0, None),
        # subagents spawned by cli1 (one nested two levels deep)
        ("sub1", "subagent", "deepseek/deepseek-v3", "cli1", night_ts(1, 14.2), "research",
         8, 6, 60_000, 8_000, 0, 0, 0, 1.5, None),
        ("sub2", "subagent", "deepseek/deepseek-v3", "sub1", night_ts(1, 14.3), "sub-research",
         5, 4, 40_000, 5_000, 0, 0, 0, 1.0, None),
        # broken accounting: messages exist, zero tokens (hermes #12023)
        ("brk1", "discord", "minimax/m2", None, night_ts(3, 10), "broken",
         9, 3, 0, 0, 0, 0, 0, None, None),
        # ancient session outside the 30d window — must be filtered out
        ("old1", "cli", "deepseek/deepseek-v3", None, time.time() - 90 * 86400, "old",
         5, 5, 1_000_000, 100_000, 0, 0, 0, 99.0, None),
    ]
    con.executemany(S, rows)
    M = ("INSERT INTO messages (session_id, role, tool_name, timestamp, token_count) "
         "VALUES (?,?,?,?,?)")
    msgs = [
        ("cli1", "tool", "web_search", night_ts(1, 14.1), 9_000),
        ("cli1", "tool", "web_search", night_ts(1, 14.15), 7_000),
        ("cli1", "tool", "read_file", night_ts(1, 14.2), 2_000),
        ("tg1", "tool", "browser", night_ts(1, 12.1), 5_000),
        ("old1", "tool", "web_search", time.time() - 90 * 86400, 50_000),
    ]
    con.executemany(M, msgs)
    con.commit()
    con.close()


def build_dumps(d: str):
    os.makedirs(d, exist_ok=True)
    body = {
        "model": "deepseek/deepseek-v3",
        "system": "S" * 5000,
        "tools": [{"name": f"tool{i}", "description": "D" * 280} for i in range(31)],
        "messages": [{"role": "user", "content": "U" * 2000}],
    }
    for i in range(3):
        with open(os.path.join(d, f"request_dump_test_{i}.json"), "w") as f:
            json.dump({"body": body}, f)


def main():
    tmp = tempfile.mkdtemp(prefix="agentburn-")
    db = os.path.join(tmp, "state.db")
    dumps = os.path.join(tmp, "sessions")
    build_db(db)
    build_dumps(dumps)

    print("adapter:")
    snap = hermes.load(db_path=db, days=30, dumps_dir=dumps)
    ok("loads sessions within the window", len(snap.sessions) == 7, f"got {len(snap.sessions)}")
    ok("old session filtered out", all(s.id != "old1" for s in snap.sessions))
    ok("source normalization (gateway:telegram)", any(s.source == "gateway:telegram" for s in snap.sessions))
    ok("tools aggregated within window only",
       any(t.name == "web_search" and t.calls == 2 and t.result_tokens == 16_000 for t in snap.tools))
    ok("dump composition sampled", snap.composition is not None and snap.composition.samples == 3)
    ok("tools dominate sampled composition", snap.composition.tools_share > 0.5)

    print("analyze:")
    a = analyze(snap, night_window=(0, 8))
    ok("total cost sums actual+estimated", abs(a.total.cost - (18 + 18 + 4 + 3 + 1.5 + 1)) < 1e-6)
    ok("cost basis is mixed", a.cost_basis == "mixed")
    ok("night bucket catches both cron runs", a.night.sessions == 2 and abs(a.night.cost - 36.0) < 1e-6)
    ok("night share ≥ 25% of spend", a.night.cost / a.total.cost >= 0.25)
    ok("cron tops by_source", next(iter(a.by_source)) == "cron")
    ok("overhead per call: cron heavy", a.overhead_per_call["cron"] == 15_000)
    ok("overhead per call: telegram 4x cli", a.overhead_per_call["gateway:telegram"] == 20_000
       and a.overhead_per_call["cli"] == 5_000)
    ok("subagent rollup chains to root cli1",
       len(a.rollups) == 1 and a.rollups[0].id == "cli1"
       and abs(a.rollups[0].sub_cost - 2.5) < 1e-6 and a.rollups[0].sub_sessions == 2)
    ok("zero-token session detected", a.zero_token_sessions == 1)
    ok("warnings mention lower bound", any("LOWER BOUND" in w for w in a.warnings))
    ok("monthly projection exists", a.monthly_projection is not None and a.monthly_projection > 0)

    print("recommend:")
    recs = recommend(a)
    ok("1-4 recommendations", 1 <= len(recs) <= 4, f"got {len(recs)}")
    ok("data-quality rec first (5%+ broken sessions)", "zero tokens" in recs[0])
    ok("night rec present", any("at night" in r for r in recs))
    ok("cron rec present", any("cron" in r.lower() for r in recs))

    print("render:")
    term = render_terminal(a, recs, color=False)
    ok("terminal: sections present", all(k in term for k in
       ("WHERE IT BURNS", "WHILE YOU SLEPT", "MODELS", "TOP TOOLS", "SUBAGENT ROLLUPS",
        "FIXED OVERHEAD", "DO THIS", "Methodology")))
    ok("terminal: night line shows $36", "$36.00" in term)
    ok("terminal: estimates marked with ~", "~$" in term)
    js = json.loads(render_json(a, recs))
    ok("json: parses & has keys", js["agentburn"] == 1 and "by_source" in js and "recommendations" in js)
    ok("json: cron bucket correct", js["by_source"]["cron"]["sessions"] == 2)

    print("utils:")
    ok("fmt_tokens", fmt_tokens(1_310_000) == "1.31M" and fmt_tokens(600_000) == "600K")

    print(f"\nAll {PASSED} checks passed.")


if __name__ == "__main__":
    main()
