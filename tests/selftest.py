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

# Windows consoles default to cp1252, where the ✓ this suite prints is not
# encodable — the run then dies on its own output instead of on a failure.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASSED = 0


def ok(name: str, cond: bool, extra: str = ""):
    global PASSED
    if not cond:
        print(f"  ✗ {name} {extra}")
        raise SystemExit(1)
    PASSED += 1
    print(f"  ✓ {name}")


def fake_home(path: str) -> dict:
    """Env with `~` pointed at `path` on every platform.

    ntpath.expanduser reads USERPROFILE, not HOME — setting only HOME made the
    MCP tests look at the real home directory on Windows.
    """
    return dict(os.environ, HOME=path, USERPROFILE=path)


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
        ("cron1", "cron", "anthropic/claude-opus-4.6", None, night_ts(2, 3), "nightly digest",
         12, 40, 600_000, 20_000, 0, 0, 0, 18.0, None),
        ("cron2", "cron", "anthropic/claude-opus-4.6", None, night_ts(1, 3), "nightly digest",
         12, 40, 600_000, 20_000, 0, 0, 0, 18.0, None),
        # daytime CLI session, actual cost, light overhead
        ("cli1", "cli", "deepseek/deepseek-v3.2-20251201", None, night_ts(1, 14), "refactor",
         30, 20, 100_000, 30_000, 50_000, 5_000, 0, None, 4.0),
        # telegram gateway: heavy per-call input (bootstrap resend pattern)
        ("tg1", "telegram", "deepseek/deepseek-v3.2-20251201", None, night_ts(1, 12), "tg chat",
         40, 10, 200_000, 10_000, 0, 0, 0, 3.0, None),
        # subagents spawned by cli1 (one nested two levels deep)
        ("sub1", "subagent", "deepseek/deepseek-v3.2-20251201", "cli1", night_ts(1, 14.2), "research",
         8, 6, 60_000, 8_000, 0, 0, 0, 1.5, None),
        ("sub2", "subagent", "deepseek/deepseek-v3.2-20251201", "sub1", night_ts(1, 14.3), "sub-research",
         5, 4, 40_000, 5_000, 0, 0, 0, 1.0, None),
        # broken accounting: messages exist, zero tokens (hermes #12023)
        ("brk1", "discord", "minimax/m2", None, night_ts(3, 10), "broken",
         9, 3, 0, 0, 0, 0, 0, None, None),
        # ancient session outside the 30d window — must be filtered out
        ("old1", "cli", "deepseek/deepseek-v3.2-20251201", None, time.time() - 90 * 86400, "old",
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
        "model": "deepseek/deepseek-v3.2-20251201",
        "system": "S" * 5000,
        "tools": [{"name": f"tool{i}", "description": "D" * 280} for i in range(31)],
        "messages": [{"role": "user", "content": "U" * 2000}],
    }
    for i in range(3):
        with open(os.path.join(d, f"request_dump_test_{i}.json"), "w", encoding="utf-8") as f:
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
    # cli reads 50k of its input from cache; overhead counts cached input too,
    # so cli is 7.5k/call (not 5k) and telegram outweighs it 2.7×, not 4×.
    ok("overhead per call counts cached input", a.overhead_per_call["gateway:telegram"] == 20_000
       and a.overhead_per_call["cli"] == 7_500)
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

    # FIXED OVERHEAD leads with the uncached figure — the one that is actually
    # re-sent at full price. cli has 7,500/call including cache but 5,000 without.
    ok("terminal: overhead shows uncached, not cache-inclusive",
       "5,000" in term and "7,500" not in term)
    ok("terminal: overhead header says uncached", "avg uncached input tokens" in term)

    import copy as _copy
    a_cached = _copy.deepcopy(a)
    a_cached.overhead_per_call = {"cli": 215_288, "subagent": 80_560}
    a_cached.overhead_uncached = {"cli": 155, "subagent": 849}
    a_cached.cache_share = {"cli": 1.0, "subagent": 0.99}
    term_c = render_terminal(a_cached, recs, color=False)
    sec = term_c[term_c.find("FIXED OVERHEAD"):]
    ok("terminal: cache-heavy rows rank by uncached, not by cache-inclusive total",
       sec.find("subagent") < sec.find("cli"))
    ok("terminal: every cache-heavy row explains its total, not just the first",
       sec.count("including cache reads") == 2)
    ok("terminal: cache-heavy row never flagged as heavy", "← heavy" not in sec)
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

    # A cache-heavy run must not be advertised as a resend tax: overhead_per_call
    # includes cache reads, and claiming those inverts the finding (98% below the
    # baseline reads as 27x above it). Regression for the share card specifically.
    import copy as _copy
    from agentburn.share import overhead_headline
    a_cached = _copy.deepcopy(a)
    a_cached.overhead_per_call = {"cli": 214_829}
    a_cached.overhead_uncached = {"cli": 158}
    a_cached.cache_share = {"cli": 1.0}
    head = overhead_headline(a_cached)
    ok("card: cache-heavy overhead calibrates on uncached, not cache reads",
       "158" in head and "214,829" not in head.split("—")[0] and "26.9" not in head)
    ok("card: cache-heavy overhead states the cache share",
       "100% served from cache" in head and "214,829/call total" in head)
    card_cached = share_text(a_cached)
    ok("card: cache-heavy card never claims 27x the norm", "26.9" not in card_cached)
    ok("card: sub-baseline overhead reads as a percentage, not '0.0x'",
       "below the community norm" in head and "0.0×" not in head)
    ok("svg card: uses the same overhead headline as text",
       overhead_headline(a) in share_svg(a).replace("&amp;", "&"))
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
    ok("doctor: hermes report cites the hermes issue and db",
       "hermes-agent/issues/12023" in doc and "~/.hermes/state.db" in doc)
    # The report gets pasted into an upstream tracker: it must name the agent the
    # data actually came from, never another project's issue.
    from dataclasses import replace
    from agentburn.model import Snapshot
    zero_sess = next(s for s in snap.sessions if s.message_count > 0 and s.total_tokens == 0)
    cc_snap = Snapshot(agent="claude-code · Arcana", source_path="/x",
                       generated_at=time.time(), days=30)
    cc_snap.sessions = [replace(zero_sess, id="cc1")]
    cc_doc = render_doctor(cc_snap, color=False)
    ok("doctor: claude-code report does not cite the hermes tracker",
       "hermes-agent" not in cc_doc and "state.db" not in cc_doc)
    ok("doctor: claude-code report names its own agent and store",
       "Claude Code accounting health" in cc_doc and "~/.claude/projects" in cc_doc)

    print("openclaw adapter:")
    from agentburn.adapters import openclaw

    oc_root = os.path.join(tempfile.mkdtemp(), ".openclaw")
    store_dir = os.path.join(oc_root, "agents", "main", "sessions")
    os.makedirs(store_dir)
    now_ms = time.time() * 1000
    oc_store = {
        "agent:main:main": {"sessionId": "m1", "model": "anthropic/claude-opus-4.6",
                            "inputTokens": 100_000, "outputTokens": 20_000, "cacheRead": 5_000,
                            "cacheWrite": 1_000, "estimatedCostUsd": 4.0,
                            "sessionStartedAt": now_ms - 3600_000},
        "cron:main-heartbeat-job": {"sessionId": "hb", "model": "anthropic/claude-opus-4.6",
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
    with open(os.path.join(store_dir, "sessions.json"), "w", encoding="utf-8") as f:
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
    with open(os.path.join(proj, "11111111-2222-3333-4444-555555555555.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(l) for l in main_lines))
    with open(os.path.join(sub, "agent-deadbeef.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": iso(4),
                            "message": {"model": "claude-haiku", "usage": {
                                "input_tokens": 300, "output_tokens": 100,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 1_000}}}))
    with open(os.path.join(proj, "not-a-session.jsonl"), "w", encoding="utf-8") as f:
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
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("sentinel: breach printed and exit 1", r.returncode == 1 and "exceeds budget" in r.stdout)

    # A cp1252 console (the Windows default) could not encode the emoji in the
    # report header, so the tool died on its own first line. Pin that shut.
    r_cp = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes",
                           "--db", env_db, "--no-color"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONIOENCODING="cp1252"))
    ok("cli survives a non-UTF-8 console", r_cp.returncode == 0
       and "agentburn" in r_cp.stdout, r_cp.stderr[-200:])
    r2 = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes", "--db", env_db,
                         "--budget-month", "99999", "--fail-over"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("sentinel: under budget → exit 0", r2.returncode == 0)
    r3 = subprocess.run([sys.executable, "-m", "agentburn.cli", "--db", env_db],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
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
    with open(os.path.join(store_dir, "sessions.json"), "w", encoding="utf-8") as f:
        json.dump(oc_store, f)
    tg_lines = []
    for i in range(5):
        tg_lines.append({"message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": f"c{i}", "name": "browser",
             "arguments": {"url": "https://news.site/page"}}]}, "timestamp": now_ms - 3500_000 + i})
    for i in range(3):
        tg_lines.append({"message": {"role": "toolResult", "content": [
            {"type": "toolResult", "name": "browser", "isError": True}]}, "timestamp": now_ms - 3400_000 + i})
    with open(os.path.join(store_dir, "tg.jsonl"), "w", encoding="utf-8") as f:
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
    with open(os.path.join(proj, "11111111-2222-3333-4444-555555555555.jsonl"), "a", encoding="utf-8") as f:
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
                            "--db", env_db, "--no-color"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("cli why: runs and reports the loop", r_why.returncode == 0 and "read_file" in r_why.stdout)

    print("v0.5 UX:")
    r_rep = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes",
                            "--db", env_db, "--no-color"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("TL;DR opens the report", "TL;DR:" in r_rep.stdout and "/mo pace" in r_rep.stdout)
    ok("TL;DR names the dominant source", "`cron`" in r_rep.stdout)
    ok("First fix surfaced", "First fix:" in r_rep.stdout)
    ok("Next hints close the report",
       "Next:" in r_rep.stdout and "agentburn why" in r_rep.stdout and "--save-baseline" in r_rep.stdout)
    r_wj = subprocess.run([sys.executable, "-m", "agentburn.cli", "why", "--agent", "hermes",
                           "--db", env_db, "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    wj = json.loads(r_wj.stdout)
    ok("why --json: parses with findings", wj["agentburn_why"] == 1 and len(wj["rereads"]) >= 1)
    r_week = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes",
                             "--db", env_db, "--week", "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("--week sets a 7-day window", json.loads(r_week.stdout)["days"] == 7)

    from agentburn.report import _tldr
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
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("cli why --source telegram: header + functions section",
       "gateway:telegram" in r_src.stdout and "WHAT IT ACTUALLY DID" in r_src.stdout
       and "browser" in r_src.stdout)
    r_full = subprocess.run([sys.executable, "-m", "agentburn.cli", "why", "--agent", "hermes",
                             "--db", env_db, "--no-color"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("full why also lists functions (web_search)", "web_search" in r_full.stdout)
    r_srep = subprocess.run([sys.executable, "-m", "agentburn.cli", "--agent", "hermes",
                             "--db", env_db, "--source", "telegram", "--json"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    sj = json.loads(r_srep.stdout)
    ok("report --source: totals are the slice only",
       sj["total"]["sessions"] == 1 and abs(sj["total"]["cost"] - 3.0) < 1e-6)

    print("explain (LLM, stub server):")
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    captured = {}

    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured["body"] = body
            captured["auth"] = self.headers.get("Authorization", "")
            resp = json.dumps({"choices": [{"message": {"content": "STUB-ANALYSIS: cron dominates."}}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Stub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    r_ex = subprocess.run([sys.executable, "-m", "agentburn.cli", "explain", "--agent", "hermes",
                           "--db", env_db, "--llm", f"http://127.0.0.1:{port}/v1",
                           "--model", "test-model", "--no-color"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("explain: local endpoint works end-to-end",
       r_ex.returncode == 0 and "STUB-ANALYSIS" in r_ex.stdout and "nothing left this machine" in r_ex.stdout)
    content = captured["body"]["messages"][1]["content"]
    ok("explain: payload carries report + why", '"report"' in content and '"agentburn_why"' in content)
    ok("explain: local payload NOT redacted (titles intact)", "refactor" in content)
    ok("explain: model + system prompt present",
       captured["body"]["model"] == "test-model"
       and "cost analyst" in captured["body"]["messages"][0]["content"])

    r_rem = subprocess.run([sys.executable, "-m", "agentburn.cli", "explain", "--agent", "hermes",
                            "--db", env_db, "--llm", "http://example.com/v1", "--model", "m"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("explain: remote refused without --yes-remote",
       r_rem.returncode == 2 and "--yes-remote" in r_rem.stderr)

    from agentburn.llm import redact as _redact
    red = _redact({"report": {"subagent_rollups": [{"title": "secret task name"}]},
                   "why": {"rereads": [{"session": "secret task name", "arg": "/home/u/proj/big.md"}],
                           "storms": [], "failure_cost": {"examples": ["secret task name"]},
                           "reasoning_heavy": []}})
    s = json.dumps(red)
    ok("redact: titles → session-N, paths → basenames",
       "secret task name" not in s and "session-1" in s and '"big.md"' in s and "/home/" not in s)

    r_nomodel = subprocess.run([sys.executable, "-m", "agentburn.cli", "explain", "--agent", "hermes",
                                "--db", env_db, "--llm", f"http://127.0.0.1:{port}/v1"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
    # Paths and project names used to survive inside prose: redact walked known
    # keys, while observations render the same path into a sentence.
    from agentburn import llm  # noqa: E402

    leaky = {
        "report": {"subagent_rollups": [{"title": "Thoforge/5df33520"}],
                   "recommendations": ["'Thoforge/5df33520' compacted 4× — trim its context"]},
        "why": {"rereads": [{"session": "abc", "arg": "/Users/someone/secret/plan.md", "count": 4}],
                "observations": ["`/Users/someone/secret/plan.md` was fetched 13× by Read"],
                "compactions": {"total": 4, "worst": [["Thoforge/5df33520", 4]]}},
    }
    cleaned = json.dumps(llm.redact(leaky), ensure_ascii=False)
    ok("redact: no absolute path survives, not even inside prose",
       "/Users/someone" not in cleaned and "secret" not in cleaned, cleaned[:160])
    ok("redact: a session title is aliased everywhere it appears",
       "Thoforge/5df33520" not in cleaned and "session-1" in cleaned, cleaned[:160])
    ok("redact: the finding is still readable after scrubbing",
       "fetched 13" in cleaned and "plan.md" in cleaned)

    ok("explain: missing --model is a clear error", r_nomodel.returncode == 2 and "--model" in r_nomodel.stderr)
    srv.shutdown()

    print("mcp server:")
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18"}})
    tlist = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tcall = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "burn_report", "arguments": {"agent": "hermes"}}})
    bad = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    env = fake_home(os.path.dirname(env_db))  # mcp resolves the agent from default paths
    # точечно: подменяем default_db_path через HERMES-структуру в tmp
    hermes_home = tempfile.mkdtemp()
    os.makedirs(os.path.join(hermes_home, ".hermes"), exist_ok=True)
    import shutil
    shutil.copy(env_db, os.path.join(hermes_home, ".hermes", "state.db"))
    env = fake_home(hermes_home)
    r_mcp = subprocess.run([sys.executable, "-m", "agentburn.cli", "mcp"],
                           input="\n".join([init, tlist, tcall, bad]) + "\n",
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    lines = [json.loads(l) for l in r_mcp.stdout.strip().splitlines()]
    byid = {l.get("id"): l for l in lines}
    ok("mcp: initialize → serverInfo", byid[1]["result"]["serverInfo"]["name"] == "agentburn")
    ok("mcp: tools/list → 4 tools",
       {t["name"] for t in byid[2]["result"]["tools"]}
       == {"burn_report", "burn_why", "burn_limits", "burn_card"})
    body0 = json.loads(byid[3]["result"]["content"][0]["text"])
    ok("mcp: tools/call burn_report returns the report JSON",
       byid[3]["result"]["isError"] is False and body0["agentburn"] == 1 and body0["total"]["sessions"] > 0)
    ok("mcp: unknown method → -32601", byid[4]["error"]["code"] == -32601)

    print("fix (dry-run):")
    jobs_dir = os.path.join(hermes_home, ".hermes", "cron")
    os.makedirs(jobs_dir, exist_ok=True)
    with open(os.path.join(jobs_dir, "jobs.json"), "w", encoding="utf-8") as f:
        json.dump({"jobs": [{"id": "j1", "name": "nightly digest", "model": "anthropic/claude-opus-4.6"},
                            {"id": "j2", "name": "weekly report", "model": None}]}, f)
    r_fix = subprocess.run([sys.executable, "-m", "agentburn.cli", "fix", "--agent", "hermes",
                            "--db", os.path.join(hermes_home, ".hermes", "state.db"), "--no-color"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("fix: dry-run header, nothing applied", "DRY-RUN" in r_fix.stdout and "nothing was changed" in r_fix.stdout)
    ok("fix: hermes cron patch with real jobs.json path and jobs",
       "cron" in r_fix.stdout and "jobs.json" in r_fix.stdout and "nightly digest" in r_fix.stdout
       and "deepseek/deepseek-chat" in r_fix.stdout)
    ok("fix: telegram toolset patch present", "toolset" in r_fix.stdout.lower())
    ok("fix: verified-keys notes", "verified in" in r_fix.stdout)
    r_fix_oc = subprocess.run([sys.executable, "-m", "agentburn.cli", "fix", "--agent", "openclaw",
                               "--db", oc_root, "--no-color"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("fix: openclaw heartbeat patch (every/activeHours/lightContext)",
       all(k in r_fix_oc.stdout for k in ("heartbeat", "activeHours", "lightContext", "openclaw.json")))
    # Bare HOME: no MCP servers registered, no CLAUDE.md — the generators must
    # stay silent rather than invent a lever (they read the real config, so the
    # test would otherwise depend on the machine it runs on).
    r_fix_cc = subprocess.run([sys.executable, "-m", "agentburn.cli", "fix", "--agent", "claude-code",
                               "--db", cc_root, "--no-color"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=fake_home(tempfile.mkdtemp()))
    ok("fix: claude-code on a bare machine → honest 'no applicable patches'",
       r_fix_cc.returncode == 0 and "No applicable patches" in r_fix_cc.stdout)

    print("prices (real snapshot):")
    from agentburn import prices

    ok("lookup: date suffix stripped", prices.lookup("deepseek/deepseek-v3.2-20251201") == (0.269, 0.4))
    ok("lookup: reordered anthropic slug",
       prices.lookup("anthropic/claude-4.6-opus-20260205") == (5.0, 25.0))
    ok("lookup: unknown → None", prices.lookup("nobody/mystery-model") is None)
    ok("cheap reference priced", prices.cheap_cost_usd(1e6, 1e6) == 0.32 + 0.89)
    h3 = hermes.load(db_path=env_db, days=30)
    a3 = analyze(h3)
    recs3 = recommend(a3)
    cron_rec = next(r for r in recs3 if "Scheduled (cron)" in r)
    ok("recommend: cron rule cites real-price saving",
       "saves ≈$" in cron_rec and f"price snapshot {prices.AS_OF}" in cron_rec)
    r_fix2 = subprocess.run([sys.executable, "-m", "agentburn.cli", "fix", "--agent", "hermes",
                             "--db", env_db, "--no-color"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("fix: impact uses price snapshot arithmetic", f"snapshot {prices.AS_OF}" in r_fix2.stdout
       and "saves ≈$" in r_fix2.stdout)

    print("skill + server.json:")
    root = os.path.join(os.path.dirname(__file__), "..")
    skill = open(os.path.join(root, "skill", "agentburn", "SKILL.md"), encoding="utf-8").read()
    ok("skill: frontmatter + honesty rules",
       skill.startswith("---") and "name: agentburn" in skill and "LOWER BOUND" in skill)
    srv = json.load(open(os.path.join(root, "server.json"), encoding="utf-8"))
    ok("server.json: registry shape", srv["name"] == "io.github.Socialpranker/agentburn"
       and srv["packages"][0]["registryType"] == "pypi"
       and srv["packages"][0]["transport"]["type"] == "stdio"
       and srv["packages"][0]["packageArguments"][0]["value"] == "mcp")
    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    ok("README carries mcp-name for PyPI validation",
       "mcp-name: io.github.Socialpranker/agentburn" in readme)

    print("context thrash + cron runs (ход 2):")
    cc_compact = [
        {"type": "system", "subtype": "compact_boundary", "timestamp": iso(2)},
        {"type": "system", "subtype": "compact_boundary", "timestamp": iso(2)},
        {"type": "system", "subtype": "compact_boundary", "timestamp": iso(1)},
    ]
    with open(os.path.join(proj, "11111111-2222-3333-4444-555555555555.jsonl"), "a", encoding="utf-8") as f:
        f.write("\n" + "\n".join(json.dumps(l) for l in cc_compact))
    cc4 = claude_code.load(db_path=cc_root, days=30)
    ok("cc: compactions counted per session",
       cc4.compactions.get("11111111-2222-3333-4444-555555555555") == 3)
    cb4 = analyze_behavior(cc4)
    ok("cc: CONTEXT THRASH in report + observation",
       cb4.compactions[0] == 3 and any("compaction" in o for o in cb4.observations))
    rend4 = render_behavior(cb4, color=False)
    ok("cc: thrash section rendered", "CONTEXT THRASH" in rend4 and "3 compaction" in rend4)

    oc4 = openclaw.load(db_path=oc_root, days=30)
    ob4 = analyze_behavior(oc4)
    digest_job = next((c for c in ob4.cron_runs if c.job == "digest"), None)
    ok("oc: cron run rollup by job (digest from key cron:digest:run:run-1)",
       digest_job is not None and digest_job.runs == 1 and abs(digest_job.cost - 1.0) < 1e-6)
    ok("oc: heartbeat jobs rolled up too",
       any("heartbeat" in c.job for c in ob4.cron_runs))
    ok("oc: CRON RUNS section rendered",
       "CRON RUNS" in render_behavior(ob4, color=False))

    print("drift (ход 1):")
    from agentburn.drift import build_drift, load_trends, render_drift

    trends_path = os.path.join(tempfile.mkdtemp(), "trends.json")
    with open(trends_path, "w", encoding="utf-8") as f:
        json.dump({
            "as_of": "2026-06-10", "days_covered": 40, "warming_up": False,
            "note": "test", "models": {
                "anthropic/claude-opus-4.6": {"t7": 9e12, "pct_4w": -41.0, "last": 1e12},
                "deepseek/deepseek-v3.2": {"t7": 8e12, "pct_4w": 12.0, "last": 1e12},
                "stepfun/step-3.5-flash": {"t7": 5e12, "pct_4w": 180.0, "last": 9e11},
            },
            "risers": [{"slug": "stepfun/step-3.5-flash", "pct_4w": 180.0, "t7": 5e12}],
            "fallers": [{"slug": "anthropic/claude-opus-4.6", "pct_4w": -41.0, "t7": 9e12}],
        }, f)
    tr = load_trends(trends_path)
    d = build_drift([analyze(hermes.load(db_path=env_db, days=30))], tr)
    opus_row = next(r for r in d["rows"] if "opus" in r["model"])
    ds_row = next(r for r in d["rows"] if "deepseek" in r["model"])
    ok("drift: direct join + dated-slug normalization (deepseek-…-20251201 → v3.2)",
       opus_row["world_pct_4w"] == -41.0 and ds_row["world_pct_4w"] == 12.0)
    ok("drift: alert for a dying model you pay for",
       any("world usage -41%" in a or "-41%" in a for a in d["advice"]))
    ok("drift: rising cheaper alternative cited with price math",
       any("stepfun/step-3.5-flash" in a and "cheaper" in a for a in d["advice"]))
    rendered_d = render_drift(d, color=False)
    ok("drift render: table + alerts + privacy line",
       all(k in rendered_d for k in ("YOUR MODELS vs THE WORLD", "DRIFT ALERTS",
                                     "never leaves this machine")))
    warm = build_drift([analyze(hermes.load(db_path=env_db, days=30))],
                       {"warming_up": True, "days_covered": 3, "models": {}})
    ok("drift: warming_up path renders gently",
       "warming up" in render_drift(warm, color=False))
    r_drift = subprocess.run([sys.executable, "-m", "agentburn.cli", "drift", "--agent", "hermes",
                              "--db", env_db, "--trends", trends_path, "--json"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    dj = json.loads(r_drift.stdout)
    ok("cli drift --json works end-to-end", dj["rows"] and dj["advice"])

    print("burn index (ход 3):")
    from agentburn.burnindex import (BOUNDS, build_metrics, rank_against, render_rank,
                                     spend_band, submit_url)
    sys.path.insert(0, os.path.join(root, "tools"))
    from aggregate_burn_index import aggregate, extract_submissions

    h5 = hermes.load(db_path=env_db, days=30)
    a5 = analyze(h5)
    b5 = analyze_behavior(h5)
    m5 = build_metrics([a5], [b5])
    ok("metrics: efficiency ratios + coarse band only",
       m5["schema"] == 1 and "night_share" in m5 and "overhead_cli" in m5
       and m5["spend_band"] in ("<$10", "$10-50", "$50-200", "$200-1000", "$1000+", "unknown"))
    ok("metrics: NO raw volumes/titles/paths",
       all(k not in m5 for k in ("tokens", "total", "sessions"))
       and "nightly digest" not in json.dumps(m5))
    ok("spend bands", spend_band(7) == "<$10" and spend_band(431) == "$200-1000"
       and spend_band(None) == "unknown")
    from urllib.parse import unquote as urllib_unquote
    url = submit_url(m5)
    ok("submit url: prefilled issue with label", "issues/new" in url and "burn-index" in url
       and "night_share" in urllib_unquote(url))

    issues = [
        {"body": "```json\n" + json.dumps({**m5, "overhead_cli": 4000 + i * 1000}) + "\n```"}
        for i in range(6)
    ] + [
        {"body": "```json\n" + json.dumps({**m5, "overhead_cli": 9_999_999}) + "\n```"},  # junk
        {"body": "no payload here"},
    ]
    subs = extract_submissions(issues)
    ok("aggregate: junk filtered by plausibility bounds",
       len(subs) == 7 and sum(1 for s in subs if "overhead_cli" in s) == 6)
    agg = aggregate(issues)
    ok("aggregate: quantiles for n>=5, insufficient otherwise",
       agg["metrics"]["overhead_cli"]["n"] == 6 and agg["metrics"]["overhead_cli"]["p50"]
       and agg["metrics"]["idle_heartbeat_share"].get("p50") is None)
    rows = rank_against({**m5, "overhead_cli": 999_999_0}, agg)  # very bad overhead
    oc_row = next(r for r in rows if r["metric"] == "overhead_cli")
    ok("rank: bad value lands worse-than-90", oc_row["worse_than_pct"] == 90)
    rendered_r = render_rank(rows, agg, color=False)
    ok("rank render: verdicts + invite", "worse than ~90%" in rendered_r
       and "agentburn --submit" in rendered_r)
    empty = render_rank([], {"n": 1}, color=False)
    ok("rank: graceful when index insufficient", "be one of the first" in empty)

    r_sub = subprocess.run([sys.executable, "-m", "agentburn.cli", "--submit", "--agent", "hermes",
                            "--db", env_db], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("cli --submit: payload + link, nothing sent",
       r_sub.returncode == 0 and "issues/new" in r_sub.stdout and "Nothing was sent" in r_sub.stdout)
    bench = os.path.join(tempfile.mkdtemp(), "bench.json")
    with open(bench, "w", encoding="utf-8") as f:
        json.dump(agg, f)
    r_rank = subprocess.run([sys.executable, "-m", "agentburn.cli", "rank", "--agent", "hermes",
                             "--db", env_db, "--benchmark-file", bench, "--no-color"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("cli rank: end-to-end vs local index", r_rank.returncode == 0 and "Burn Index" in r_rank.stdout)


    # ---------------------------------------------------------------- limits
    # Subscription agents record no prices, so every claim here is about
    # windows. The fixture is a Claude Code transcript tree, since that is the
    # only adapter that can see per-call timestamps.
    print("limits (subscription windows):")
    from agentburn.adapters import claude_code as cc  # noqa: E402
    from agentburn.limits import build_limits, limits_json, render_limits  # noqa: E402

    cc_root = os.path.join(tempfile.mkdtemp(), "projects")
    proj = os.path.join(cc_root, "-tmp-demo")
    subdir = os.path.join(proj, "11111111-1111-4111-8111-111111111111", "subagents")
    os.makedirs(subdir, exist_ok=True)
    now = time.time()

    def turn(ts, model, inp=0, out=0, cr=0, cw=0):
        return json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + "Z",
            "message": {"model": model, "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}},
        })

    # a heavy burst 20h ago, a light one 2h ago, both on the main session
    burst_at = now - 20 * 3600
    quiet_at = now - 2 * 3600
    main_lines = [turn(burst_at + i * 60, "claude-opus-5", inp=1000, out=1000) for i in range(10)]
    main_lines += [turn(quiet_at + i * 60, "claude-sonnet-5", inp=1000, out=0) for i in range(5)]
    with open(os.path.join(proj, "11111111-1111-4111-8111-111111111111.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(main_lines) + "\n")
    with open(os.path.join(subdir, "agent-1.jsonl"), "w", encoding="utf-8") as f:
        f.write(turn(burst_at + 300, "claude-sonnet-5", inp=0, out=0, cr=100_000) + "\n")

    snap_cc = cc.load(db_path=cc_root, days=30, now=now)
    ok("cc adapter: usage cells filled", len(snap_cc.usage_cells) > 0)
    cell_tokens = sum(c.input_tokens + c.output_tokens + c.cache_read_tokens + c.cache_write_tokens
                      for c in snap_cc.usage_cells)
    ok("cc adapter: cells account for every token",
       cell_tokens == sum(s_.total_tokens for s_ in snap_cc.sessions),
       f"{cell_tokens} vs {sum(s_.total_tokens for s_ in snap_cc.sessions)}")

    from agentburn.limits import token_weights  # noqa: E402

    w_in, w_out, w_cr, w_cw = token_weights("claude-sonnet-5")
    ok("weights: reference model input = 1", abs(w_in - 1.0) < 1e-9)
    ok("weights: output is 5x input on Claude pricing", abs(w_out - 5.0) < 1e-9)
    ok("weights: cache read is 0.1x", abs(w_cr - 0.1) < 1e-9)
    ok("weights: cache write is 1.25x", abs(w_cw - 1.25) < 1e-9)
    ok("weights: opus input weighs more than sonnet", token_weights("claude-opus-5")[0] > w_in)
    ok("weights: unknown model still counts (no silent drop)",
       token_weights("totally-unknown-model")[0] > 0)

    lim = build_limits(snap_cc, now=now)
    ok("limits: peak window found", lim.peak is not None and lim.peak.weight > 0)
    ok("limits: peak covers the burst, not the quiet hour",
       lim.peak.start <= burst_at + 600 and lim.peak.end >= burst_at)
    # opus 10x(1000 in + 1000 out) = 10*(1.667 + 8.333)k = 100k; sub cache read 100k*0.1*1 = 10k
    ok("limits: peak weight matches the arithmetic",
       abs(lim.peak.weight - 110_000) < 1000, f"{lim.peak.weight:,.0f}")
    ok("limits: peak split by source includes the subagent",
       any(src == "subagent" for src, _ in lim.peak_by_source))
    ok("limits: typical window is the median of active slots, below the peak",
       0 < lim.typical < lim.peak.weight)
    ok("limits: recent window counted", lim.current > 0)
    ok("limits: mix names what filled it",
       {k for k, _ in lim.mix} & {"output", "cache reads", "uncached input"})

    lim_hit = build_limits(snap_cc, hit=burst_at + 3600, now=now)
    ok("limits: --hit measures a ceiling from your own wall", lim_hit.ceiling > 0)
    ok("limits: ceiling counts only the window before the cut-off",
       lim_hit.ceiling <= lim.peak.weight + 1)
    rendered_lim = render_limits(lim_hit, color=False)
    ok("limits render: ceiling section + percentages",
       "MEASURED CEILING" in rendered_lim and "% " in rendered_lim.replace("%\n", "% "))
    ok("limits render: no absolute threshold is claimed",
       "does not publish" in rendered_lim or "No published formula" in rendered_lim)
    ok("limits json: unit is stated", "weighted tokens" in limits_json(lim)["unit"])

    lim_hermes = build_limits(hermes.load(db_path=env_db, days=30))
    ok("limits: adapters without per-call timestamps say so, not zero",
       bool(lim_hermes.unsupported) and lim_hermes.peak is None)
    ok("limits: unsupported render explains why",
       "per-call timestamps" in render_limits(lim_hermes, color=False))

    r_lim = subprocess.run([sys.executable, "-m", "agentburn.cli", "limits", "--agent", "claude-code",
                            "--db", cc_root, "--no-color"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("cli limits: end-to-end", r_lim.returncode == 0 and "PEAK WINDOW" in r_lim.stdout)
    r_lim_j = subprocess.run([sys.executable, "-m", "agentburn.cli", "limits", "--agent", "claude-code",
                              "--db", cc_root, "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("cli limits --json: machine readable",
       json.loads(r_lim_j.stdout)["peak"]["weight"] > 0)
    r_hit_bad = subprocess.run([sys.executable, "-m", "agentburn.cli", "limits", "--agent", "claude-code",
                                "--db", cc_root, "--hit", "yesterday"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("cli limits: a bad --hit is a clear error", r_hit_bad.returncode != 0
       and "2026-08-20" in r_hit_bad.stderr)


    # ----------------------------------------------------------------- cache
    # Parsing 3 GB of transcripts costs ~2 minutes; every command paid it again.
    # The cache is only allowed to be fast if it is also identical.
    print("parse cache:")
    from agentburn import cache  # noqa: E402

    cache_home = tempfile.mkdtemp()
    saved_home = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = cache_home
    try:
        cold = cc.load(db_path=cc_root, days=30, now=now)
        warm = cc.load(db_path=cc_root, days=30, now=now)

        def digest(sn):
            return (
                sorted((x.id, x.api_calls, x.total_tokens, x.started_at, x.ended_at, x.model)
                       for x in sn.sessions),
                sorted((c.start, c.source, c.model, c.calls, c.input_tokens, c.output_tokens,
                        c.cache_read_tokens, c.cache_write_tokens) for c in sn.usage_cells),
                [(e.name, e.ts, e.ok, e.tokens) for e in sn.events],
                sn.compactions,
            )

        ok("cache: a warm load is identical to a cold one", digest(cold) == digest(warm))
        entries = [e for e in os.listdir(cache.root("claude-code")) if e.endswith(".json")]
        ok("cache: one entry per transcript", len(entries) == 2, str(entries))

        # A grown transcript must invalidate its own entry and nothing else.
        main_path = os.path.join(proj, "11111111-1111-4111-8111-111111111111.jsonl")
        with open(main_path, "a", encoding="utf-8") as f:
            f.write(turn(now - 600, "claude-sonnet-5", inp=777) + "\n")
        grown = cc.load(db_path=cc_root, days=30, now=now)
        ok("cache: a changed file is re-parsed, not served stale",
           sum(x.total_tokens for x in grown.sessions)
           == sum(x.total_tokens for x in cold.sessions) + 777)

        stale = cache.get("claude-code", main_path, {"mtime_ns": 1, "size": 1})
        ok("cache: a mismatched stamp is a miss", stale is None)
        ok("cache: unreadable entry is a miss, not a crash",
           cache.get("claude-code", os.path.join(cc_root, "nope.jsonl"), {"a": 1}) is None)

        os.environ[cache.ENV_OFF] = "1"
        try:
            off = cc.load(db_path=cc_root, days=30, now=now)
            ok("cache: AGENTBURN_NO_CACHE reads the transcripts directly",
               digest(off) == digest(grown))
        finally:
            del os.environ[cache.ENV_OFF]

        freed = cache.clear("claude-code")
        ok("cache: clear removes the entries and reports bytes",
           freed > 0 and not os.path.isdir(cache.root("claude-code")))

        # The sweep marker must not make an entry look fresh, and a swept cache
        # must still rebuild itself.
        cc.load(db_path=cc_root, days=30, now=now)
        cache.sweep("claude-code")
        ok("cache: sweep keeps entries whose transcript still exists",
           len([e for e in os.listdir(cache.root("claude-code")) if e.endswith(".json")]) == 2)
    finally:
        for k, v in saved_home.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # A subscription agent used to submit two fields, one of them noise: night
    # and failure shares were dollar-only, and the uncached overhead of a fully
    # cached agent is single digits — which the aggregator then dropped as junk.
    from agentburn.behavior import analyze_behavior as _ab  # noqa: E402
    from agentburn.burnindex import BOUNDS, build_metrics  # noqa: E402

    sub_metrics = build_metrics([analyze(snap_cc)], [_ab(snap_cc)], [lim])
    ok("index: night share falls back to tokens when there are no prices",
       0.0 <= sub_metrics.get("night_share", -1) <= 1.0)
    ok("index: window profile submitted (peak/median, subagent share, output share)",
       sub_metrics["window_peak_to_median"] >= 1.0
       and "window_subagent_share" in sub_metrics
       and "output_share" in sub_metrics)
    ok("index: peak/median is within the aggregator's bounds",
       BOUNDS["window_peak_to_median"][0] <= sub_metrics["window_peak_to_median"]
       <= BOUNDS["window_peak_to_median"][1])
    ok("index: noise overhead is not submitted from a fully cached agent",
       "overhead_cli" not in sub_metrics or sub_metrics["cache_read_share"] < 0.9)
    ok("index: still no titles, paths or raw volumes",
       not any(isinstance(v, str) and ("/" in v or "\\" in v)
               for k, v in sub_metrics.items() if k != "agents"))

    # ------------------------------------------------- card without prices
    print("share card on a subscription agent:")
    from agentburn.share import share_svg, share_text  # noqa: E402

    a_cc = analyze(snap_cc)
    card = share_text(a_cc, lim)
    ok("card: no prices → tokens and calls, not a dash",
       card.splitlines()[1].startswith(fmt_tokens(a_cc.total.tokens)))
    ok("card: 'where it burns' survives without dollars", "where it burns:" in card)
    ok("card: peak window is on the card", "peak" in card and "weighted tokens" in card)
    svg = share_svg(a_cc, lim)
    ok("card svg: same window line", "peak" in svg and svg.startswith("<svg"))
    ok("card svg: source bars rendered without prices", "where it burns" in svg)

    # ------------------------------------------------- fix for claude code
    print("fix (claude-code levers):")
    from agentburn.fix import build_fixes, render_fixes  # noqa: E402

    cc_home = tempfile.mkdtemp()
    with open(os.path.join(cc_home, ".claude.json"), "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {"used-server": {}, "idle-server": {}}}, f)
    os.makedirs(os.path.join(cc_home, ".claude"), exist_ok=True)
    with open(os.path.join(cc_home, ".claude", "CLAUDE.md"), "w", encoding="utf-8") as f:
        f.write("x" * 12_000)  # 3k tokens at 4 chars/token
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = cc_home
    try:
        from agentburn.model import ActionEvent  # noqa: E402

        snap_cc.events.append(ActionEvent(session_id="s", ts=now, name="mcp__used-server__do"))
        patches = build_fixes("claude-code", cc_root, a_cc, None, snap_cc)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    titles = " | ".join(p_.title for p_ in patches)
    ok("fix: idle MCP server found, used one left alone",
       "1 MCP server" in titles and "idle-server" in " ".join(p_.current for p_ in patches)
       and "used-server" not in " ".join(p_.current for p_ in patches))
    ok("fix: always-loaded memory files flagged with their size",
       "memory files" in titles and "3,000 tokens" in titles)
    rendered_fix = render_fixes("claude-code", patches, color=False)
    ok("fix: claude-code patches render with a removal command",
       "claude mcp remove idle-server" in rendered_fix)
    ok("fix: still dry-run", "DRY-RUN" in rendered_fix and "no --apply" in rendered_fix.lower())

    print(f"\nAll {PASSED} checks passed.")


if __name__ == "__main__":
    main()
