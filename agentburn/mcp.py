"""`agentburn mcp` — a minimal MCP (Model Context Protocol) stdio server.

Lets any MCP-capable agent ask about its own burn from inside the conversation:
"where do I burn money?" → the host calls burn_report / burn_why and interprets
the JSON itself (the host IS an LLM — no inner model needed).

Protocol: newline-delimited JSON-RPC 2.0 over stdio; implements initialize,
tools/list and tools/call. Pure stdlib, same privacy properties as the CLI:
local files, read-only, counters and tool names only.

Register (Claude Code):  claude mcp add agentburn -- agentburn mcp
Register (Hermes/OpenClaw): add an stdio MCP server with command `agentburn mcp`.
"""

from __future__ import annotations

import json
import sys

from . import __version__
from .adapters import ADAPTERS, detect
from .analyze import analyze
from .recommend import recommend
from .report import render_json

WINDOW = {
    "type": "object",
    "properties": {
        "agent": {
            "type": "string",
            "description": "hermes | openclaw | claude-code (default: first detected)",
        },
        "days": {"type": "integer", "description": "window in days (default 30, 0 = all time)"},
        "source": {
            "type": "string",
            "description": "drill into one source, e.g. telegram / cron / heartbeat / subagent",
        },
    },
}

TOOLS = [
    {
        "name": "burn_report",
        "description": (
            "Where this machine's AI agent burns money: totals, monthly pace, breakdown by "
            "source (cron/heartbeat/gateways/subagents/cli), models, overnight window, fixed "
            "overhead per call, recommendations. Returns JSON."
        ),
        "inputSchema": WINDOW,
    },
    {
        "name": "burn_why",
        "description": (
            "Behavioral forensics from the agent's own records: functions called (with error "
            "counts), re-read loops, retry storms, idle heartbeats, money burned in failed "
            "runs, plus what-to-change observations. Returns JSON."
        ),
        "inputSchema": WINDOW,
    },
    {
        "name": "burn_card",
        "description": "Anonymized shareable burn summary (plain text, safe to post).",
        "inputSchema": WINDOW,
    },
]


def _snapshot(args: dict):
    name = args.get("agent")
    if not name:
        found = detect()
        if not found:
            raise RuntimeError("no supported agent data found on this machine")
        name = found[0]
    if name not in ADAPTERS:
        raise RuntimeError(f"unknown agent '{name}' (have: {', '.join(sorted(ADAPTERS))})")
    days = args.get("days", 30) or None
    snap = ADAPTERS[name].load(days=days)
    if args.get("source"):
        from .behavior import filter_snapshot

        snap = filter_snapshot(snap, str(args["source"]))
    return snap


def _call(name: str, args: dict) -> str:
    snap = _snapshot(args or {})
    if name == "burn_report":
        a = analyze(snap)
        return render_json(a, recommend(a))
    if name == "burn_why":
        from .behavior import analyze_behavior, behavior_json

        return json.dumps(behavior_json(analyze_behavior(snap)), indent=2, ensure_ascii=False)
    if name == "burn_card":
        from .share import share_text

        return share_text(analyze(snap))
    raise RuntimeError(f"unknown tool {name}")


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def send(obj):
        stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        stdout.flush()

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method", "")
        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agentburn", "version": __version__},
                },
            })
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            try:
                text = _call(params.get("name", ""), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            except (RuntimeError, FileNotFoundError) as e:
                result = {"content": [{"type": "text", "text": f"agentburn: {e}"}], "isError": True}
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:  # unknown request → JSON-RPC error; notifications ignored
            send({
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })
