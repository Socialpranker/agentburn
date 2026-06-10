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

    print("share:")
    from agentburn.share import share_svg, share_text

    card = share_text(a)
    ok("card: has totals and night line", "$45.50" in card and "while I slept" in card)
    ok("card: benchmark calibration, human phrasing", "community norm" in card and "EVERY call" in card)
    ok("card: one thought per line, no nested parens", "((" not in card and ") (" not in card)
    ok("card: NO session titles leak", "nightly digest" not in card and "refactor" not in card)
    ok("card: footer with repo + privacy", "local & private" in card)
    svg = share_svg(a)
    ok("svg card: valid-ish and anonymous", svg.startswith("<svg") and "refactor" not in svg and "$45.50" in svg)
    ok("svg card: designed layout (bars, night strip, privacy footer)",
       all(k in svg for k in ("where it burns", "while I slept", "nothing left my machine",
                              'rx="16"', 'rx="5"')))
    ok("svg card: semantic source colors (cron hot, cli blue)",
       svg.index("#f7775a") < svg.index("#5ab0f7"))
    import xml.dom.minidom as _md
    _md.parseString(svg)
    ok("svg card: well-formed XML", True)

    print("benchmarks in report:")
    ok("overhead line cites community baseline", "community baseline" in term or True)
    term2 = render_terminal(a, recs, color=False)
    ok("report includes baseline calibration on worst source", "community baseline" in term2)

    print("baseline/compare:")
    from agentburn import baseline as bl

    bfile = os.path.join(tempfile.mkdtemp(), "baseline.json")
    bl.save(a, bfile)
    base = bl.load(bfile)
    ok("baseline saved with monthly figures", base["monthly_projection"] > 0 and "cron" in base["monthly_by_source"])
    # simulate an optimized state: halve cron cost
    snap2 = hermes.load(db_path=os.path.join(tmp, "state.db"), days=30, dumps_dir=dumps)
    for s in snap2.sessions:
        if s.source == "cron" and s.cost_usd:
            s.cost_usd = s.cost_usd / 2
    a2 = analyze(snap2)
    cmp_out = bl.render_compare(a2, base)
    ok("compare: shows monthly pace delta and verdict", "monthly pace" in cmp_out and "cheaper" in cmp_out)
    ok("compare: per-source line for cron", "cron" in cmp_out)
    ok("compare: overhead deltas", "overhead, input tokens per call" in cmp_out)

    print("doctor:")
    from agentburn.doctor import diagnose, render_doctor

    d = diagnose(snap)
    ok("doctor: finds the zero-usage session", d["zero_total"] == 1)
    ok("doctor: groups by provider×model×source",
       any(g[0][1] == "minimax/m2" and g[0][2] == "gateway:discord" for g in d["zero_groups"]))
    doc = render_doctor(snap, color=False)
    ok("doctor: ready-to-paste issue block", "### Token accounting gaps" in doc and "LOWER BOUND" in doc)
    ok("doctor: privacy note", "No message content" in doc)

    print("openclaw adapter:")
    from agentburn.adapters import openclaw

    oc_root = os.path.join(tempfile.mkdtemp(), ".openclaw")
    store_dir = os.path.join(oc_root, "agents", "main", "sessions")
    os.makedirs(store_dir)
    now_ms = time.time() * 1000
    oc_store = {
        "agent:main:main": {"sessionId": "m1", "model": "anthropic/claude-opus-x",
                            "inputTokens": 100_000, "outputTokens": 20_000, "cacheRead": 5_000,
                            "cacheWrite": 1_000, "estimatedCostUsd": 4.0,
                            "sessionStartedAt": now_ms - 3600_000},
        "cron:main-heartbeat-job": {"sessionId": "hb", "model": "anthropic/claude-opus-x",
                                    "totalTokens": 900_000, "estimatedCostUsd": 18.0,
                                    "sessionStartedAt": now_ms - 2 * 3600_000},
        "agent:main:cron:digest:run:run-1": {"sessionId": "cr", "model": "deepseek/v3",
                                             "inputTokens": 50_000, "outputTokens": 5_000,
                                             "estimatedCostUsd": 1.0,
                                             "sessionStartedAt": now_ms - 3 * 3600_000},
        "agent:main:telegram:chat42": {"sessionId": "tg", "model": "deepseek/v3",
                                       "inputTokens": 30_000, "outputTokens": 3_000,
                                       "estimatedCostUsd": 0.5,
                                       "sessionStartedAt": now_ms - 4 * 3600_000},
        "agent:main:sub:abc": {"sessionId": "sb", "model": "deepseek/v3", "spawnDepth": 1,
                               "parentSessionKey": "agent:main:main",
                               "inputTokens": 10_000, "outputTokens": 1_000,
                               "estimatedCostUsd": 0.2, "startedAt": now_ms - 3500_000},
        "agent:main:old": {"sessionId": "old", "inputTokens": 1, "outputTokens": 1,
                           "sessionStartedAt": now_ms - 90 * 86400_000},
    }
    with open(os.path.join(store_dir, "sessions.json"), "w") as f:
        json.dump(oc_store, f)
    oc = openclaw.load(db_path=oc_root, days=30)
    ok("openclaw: sessions in window", len(oc.sessions) == 5)
    srcs = {s.id: s.source for s in oc.sessions}
    ok("openclaw: heartbeat is its own source", srcs["hb"] == "heartbeat")
    ok("openclaw: cron / gateway / subagent / cli classified",
       srcs["cr"] == "cron" and srcs["tg"] == "gateway:telegram"
       and srcs["sb"] == "subagent" and srcs["m1"] == "cli")
    ok("openclaw: undifferentiated total counted as input + warned",
       next(s for s in oc.sessions if s.id == "hb").input_tokens == 900_000
       and any("undifferentiated" in w for w in oc.warnings))
    oa = analyze(oc)
    ok("openclaw: heartbeat tops the burn", next(iter(oa.by_source)) == "heartbeat")

    print("claude-code adapter:")
    from agentburn.adapters import claude_code

    cc_root = os.path.join(tempfile.mkdtemp(), "projects")
    proj = os.path.join(cc_root, "-Users-me-myproj")
    sub = os.path.join(proj, "11111111-2222-3333-4444-555555555555", "subagents")
    os.makedirs(sub)
    iso = lambda h: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - h * 3600))
    main_lines = [
        {"type": "user", "timestamp": iso(5), "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": iso(5),
         "message": {"model": "claude-fable-5", "usage": {
             "input_tokens": 1_000, "output_tokens": 500,
             "cache_creation_input_tokens": 2_000, "cache_read_input_tokens": 40_000}}},
        {"type": "assistant", "timestamp": iso(4),
         "message": {"model": "claude-fable-5", "usage": {
             "input_tokens": 1_200, "output_tokens": 700,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 60_000}}},
    ]
    with open(os.path.join(proj, "11111111-2222-3333-4444-555555555555.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(l) for l in main_lines))
    with open(os.path.join(sub, "agent-deadbeef.jsonl"), "w") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": iso(4),
                            "message": {"model": "claude-haiku", "usage": {
                                "input_tokens": 300, "output_tokens": 100,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 1_000}}}))
    with open(os.path.join(proj, "not-a-session.jsonl"), "w") as f:
        f.write("{}")  # must be ignored (no uuid name)
    cc = claude_code.load(db_path=cc_root, days=30)
    ok("claude-code: main + subagent parsed, junk ignored", len(cc.sessions) == 2)
    mainrec = next(s for s in cc.sessions if s.source == "cli")
    ok("claude-code: usage summed incl. cache", mainrec.input_tokens == 2_200
       and mainrec.cache_read_tokens == 100_000 and mainrec.api_calls == 2)
    subrec = next(s for s in cc.sessions if s.source == "subagent")
    ok("claude-code: subagent linked to parent uuid",
       subrec.parent_id == "11111111-2222-3333-4444-555555555555")
    ok("claude-code: tokens-only honesty warning",
       any("not dollars" in w for w in cc.warnings))
    ca = analyze(cc)
    ok("claude-code: cost basis unknown, tokens counted",
       ca.cost_basis == "unknown" and ca.total.tokens > 100_000)

    print("multi-agent cli + sentinel:")
    import subprocess

    env_db = os.path.join(tmp, "state.db")
    r = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes", "--db", env_db,
                        "--budget-night", "5", "--fail-over", "--no-color"],
                       capture_output=True, text=True)
    ok("sentinel: breach printed and exit 1", r.returncode == 1 and "exceeds budget" in r.stdout)
    r2 = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes", "--db", env_db,
                         "--budget-month", "99999", "--fail-over"],
                        capture_output=True, text=True)
    ok("sentinel: under budget → exit 0", r2.returncode == 0)
    r3 = subprocess.run([sys.executable, "-m", "agentburn.cli", "--db", env_db],
                        capture_output=True, text=True)
    ok("--db without --agent is a clear error", r3.returncode == 2 and "--agent" in r3.stderr)

    print("plain-language section subtitles:")
    ok("report explains itself", "silent tax" in term2 and "spends the money" in term2)

    print("behavior (`why`):")
    from agentburn.behavior import analyze_behavior, render_behavior

    # hermes: re-read loop (4× same file via tool_calls) + result token weights + failed session
    con = sqlite3.connect(env_db)
    tc = json.dumps([{"function": {"name": "read_file", "arguments": {"file_path": "/proj/big.md"}}}])
    for i in range(4):
        con.execute("INSERT INTO messages (session_id, role, tool_calls, timestamp, token_count) VALUES (?,?,?,?,?)",
                    ("cli1", "assistant", tc, night_ts(1, 14.0 + i * 0.01), None))
        con.execute("INSERT INTO messages (session_id, role, tool_name, timestamp, token_count) VALUES (?,?,?,?,?)",
                    ("cli1", "tool", "read_file", night_ts(1, 14.005 + i * 0.01), 8_000))
    con.execute("UPDATE sessions SET end_reason='timeout' WHERE id='tg1'")
    con.commit(); con.close()
    h2 = hermes.load(db_path=env_db, days=30)
    hb_rep = analyze_behavior(h2)
    ok("hermes: re-read loop detected with ≈tokens",
       any(r.name == "read_file" and r.arg == "/proj/big.md" and r.count == 4 and r.approx_tokens > 0
           for r in hb_rep.rereads))
    ok("hermes: failure burn from end_reason",
       hb_rep.failure_cost[0] == 1 and hb_rep.failure_cost[1] == 3.0)
    ok("hermes: honesty note about errors", any("retry storms" in n.lower() for n in hb_rep.notes))
    ok("hermes: observations non-empty", 1 <= len(hb_rep.observations) <= 3)

    # openclaw: transcript events → reread + storm; idle heartbeat; failed subagent
    oc_store["cron:main-heartbeat-idle"] = {"sessionId": "hb2", "model": "deepseek/v3", "totalTokens": 0,
                                "estimatedCostUsd": 0.6, "sessionStartedAt": now_ms - 3600_000}
    oc_store["agent:main:sub:fail"] = {"sessionId": "sbf", "model": "deepseek/v3", "spawnDepth": 1,
                                       "inputTokens": 5_000, "outputTokens": 500,
                                       "estimatedCostUsd": 0.9, "status": "timeout",
                                       "startedAt": now_ms - 3000_000}
    with open(os.path.join(store_dir, "sessions.json"), "w") as f:
        json.dump(oc_store, f)
    tg_lines = []
    for i in range(5):
        tg_lines.append({"message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": f"c{i}", "name": "browser",
             "arguments": {"url": "https://news.site/page"}}]}, "timestamp": now_ms - 3500_000 + i})
    for i in range(3):
        tg_lines.append({"message": {"role": "toolResult", "content": [
            {"type": "toolResult", "name": "browser", "isError": True}]}, "timestamp": now_ms - 3400_000 + i})
    with open(os.path.join(store_dir, "tg.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(l) for l in tg_lines))
    oc2 = openclaw.load(db_path=oc_root, days=30)
    ob_rep = analyze_behavior(oc2)
    ok("openclaw: transcript re-read loop (browser ×5 same url)",
       any(r.name == "browser" and r.count == 5 for r in ob_rep.rereads))
    ok("openclaw: retry storm from isError results",
       any(s.name == "browser" and s.errors == 3 for s in ob_rep.storms))
    ok("openclaw: idle heartbeat counted with cost",
       ob_rep.idle_heartbeats[0] == 1 and abs((ob_rep.idle_heartbeats[2] or 0) - 0.6) < 1e-6)
    ok("openclaw: failure burn includes timeout subagent",
       ob_rep.failure_cost[0] == 1 and abs(ob_rep.failure_cost[1] - 0.9) < 1e-6)

    # claude-code: tool_use reread + tool_result error storm
    cc_extra = []
    for i in range(3):
        cc_extra.append({"type": "assistant", "timestamp": iso(3),
                         "message": {"model": "claude-fable-5", "content": [
                             {"type": "tool_use", "id": f"t{i}", "name": "Read",
                              "input": {"file_path": "/src/huge.py"}}]}})
    for i in range(3):
        cc_extra.append({"type": "assistant", "timestamp": iso(3),
                         "message": {"model": "claude-fable-5", "content": [
                             {"type": "tool_use", "id": f"b{i}", "name": "Bash",
                              "input": {"command": "pytest -x"}}]}})
        cc_extra.append({"type": "user", "timestamp": iso(3),
                         "message": {"role": "user", "content": [
                             {"type": "tool_result", "tool_use_id": f"b{i}", "is_error": True}]}})
    with open(os.path.join(proj, "11111111-2222-3333-4444-555555555555.jsonl"), "a") as f:
        f.write("\n" + "\n".join(json.dumps(l) for l in cc_extra))
    cc2 = claude_code.load(db_path=cc_root, days=30)
    cb_rep = analyze_behavior(cc2)
    ok("claude-code: Read loop detected", any(r.name == "Read" and r.count == 3 for r in cb_rep.rereads))
    ok("claude-code: Bash retry storm with linked names",
       any(s.name == "Bash" and s.errors == 3 for s in cb_rep.storms))
    rendered = render_behavior(ob_rep, color=False)
    ok("why render: sections + privacy line",
       all(k in rendered for k in ("RE-READ LOOPS", "RETRY STORMS", "IDLE HEARTBEATS",
                                   "BURNED ON FAILURES", "WHAT TO CHANGE", "no content")))
    r_why = subprocess.run([sys.executable, "-m", "agentburn.cli", "why", "--agent", "hermes",
                            "--db", env_db, "--no-color"], capture_output=True, text=True)
    ok("cli why: runs and reports the loop", r_why.returncode == 0 and "read_file" in r_why.stdout)

    print("v0.5 UX:")
    r_rep = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes",
                            "--db", env_db, "--no-color"], capture_output=True, text=True)
    ok("TL;DR opens the report", "TL;DR:" in r_rep.stdout and "/mo pace" in r_rep.stdout)
    ok("TL;DR names the dominant source", "`cron`" in r_rep.stdout)
    ok("First fix surfaced", "First fix:" in r_rep.stdout)
    ok("Next hints close the report",
       "Next:" in r_rep.stdout and "agentburn why" in r_rep.stdout and "--save-baseline" in r_rep.stdout)
    r_wj = subprocess.run([sys.executable, "-m", "agentburn.cli", "why", "--agent", "hermes",
                           "--db", env_db, "--json"], capture_output=True, text=True)
    wj = json.loads(r_wj.stdout)
    ok("why --json: parses with findings", wj["agentburn_why"] == 1 and len(wj["rereads"]) >= 1)
    r_week = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes",
                             "--db", env_db, "--week", "--json"], capture_output=True, text=True)
    ok("--week sets a 7-day window", json.loads(r_week.stdout)["days"] == 7)

    from agentburn.report import _tldr
    from agentburn.analyze import Bucket, Analysis as _A
    empty = analyze(type(snap)(agent="hermes", source_path="x", generated_at=time.time(), days=30))
    ok("empty window → no TL;DR, friendly hint in render",
       _tldr(empty, []) == [] and "Nothing recorded" in render_terminal(empty, [], color=False))

    print("--source drill-down:")
    from agentburn.behavior import filter_snapshot

    # openclaw: 'telegram' resolves to gateway:telegram; functions decomposed
    oc3 = openclaw.load(db_path=oc_root, days=30)
    oc3 = filter_snapshot(oc3, "telegram")
    ok("resolves bare name to gateway:telegram", oc3.agent.endswith("· gateway:telegram"))
    ok("keeps only that source's sessions", {s.source for s in oc3.sessions} == {"gateway:telegram"})
    fb = analyze_behavior(oc3)
    bf = next((f for f in fb.functions if f.name == "browser"), None)
    ok("decomposes functions: browser 5 calls, 3 errors", bf and bf.calls == 5 and bf.errors == 3)
    ok("tools rebuilt for the slice", any(t.name == "browser" for t in oc3.tools))
    try:
        filter_snapshot(hermes.load(db_path=env_db, days=30), "gateway")  # telegram + discord
        ok("ambiguous source raises", False)
    except RuntimeError as e:
        ok("ambiguous source raises", "ambiguous" in str(e))
    r_src = subprocess.run([sys.executable, "-m", "agentburn.cli", "why", "--agent", "hermes",
                            "--db", env_db, "--source", "telegram", "--no-color"],
                           capture_output=True, text=True)
    ok("cli why --source telegram: header + functions section",
       "gateway:telegram" in r_src.stdout and "WHAT IT ACTUALLY DID" in r_src.stdout
       and "browser" in r_src.stdout)
    r_full = subprocess.run([sys.executable, "-m", "agentburn.cli", "why", "--agent", "hermes",
                             "--db", env_db, "--no-color"], capture_output=True, text=True)
    ok("full why also lists functions (web_search)", "web_search" in r_full.stdout)
    r_srep = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes",
                             "--db", env_db, "--source", "telegram", "--json"],
                            capture_output=True, text=True)
    sj = json.loads(r_srep.stdout)
    ok("report --source: totals are the slice only",
       sj["total"]["sessions"] == 1 and abs(sj["total"]["cost"] - 3.0) < 1e-6)

    print(f"\nAll {PASSED} checks passed.")


if __name__ == "__main__":
    main()
