"""OpenClaw adapter: reads ~/.openclaw/agents/*/sessions/sessions.json (read-only).

Schema observed in openclaw/openclaw `src/config/sessions/*` (June 2026):
session store is a JSON map of sessionKey → entry with rollup fields:
  inputTokens, outputTokens, totalTokens, cacheRead, cacheWrite,
  estimatedCostUsd, model / modelOverride, sessionStartedAt (ms),
  startedAt / endedAt (ms, subagent runs), spawnDepth, parentSessionKey.

The session KEY encodes the burn source, e.g.
  agent:main:main                      → cli (main chat)
  agent:main:telegram:...              → gateway:telegram
  agent:main:cron:<job>:run:run-123    → cron
  cron:main-heartbeat-job              → heartbeat  ← the famous one
  spawnDepth > 0                       → subagent

So agentburn can answer "what did the heartbeat cost me" — the single most
complained-about line item in the OpenClaw ecosystem.
"""

from __future__ import annotations

import glob
import json
import os
import time
from typing import Optional

from ..model import ActionEvent, SessionRec, Snapshot
from .hermes import salient_arg

CHANNELS = {
    "telegram",
    "whatsapp",
    "discord",
    "slack",
    "signal",
    "imessage",
    "email",
    "web",
    "webchat",
    "api",
    "matrix",
    "teams",
}


def default_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".openclaw")


def _store_paths(root: str) -> list:
    return sorted(glob.glob(os.path.join(root, "agents", "*", "sessions", "sessions.json")))


def available() -> bool:
    return len(_store_paths(default_root())) > 0


def source_from_key(key: str, spawn_depth: int) -> str:
    k = (key or "").lower()
    if "heartbeat" in k:
        return "heartbeat"
    if spawn_depth and spawn_depth > 0:
        return "subagent"
    parts = [p for p in k.split(":") if p]
    if "cron" in parts:
        return "cron"
    for p in parts:
        if p in CHANNELS:
            return f"gateway:{p}"
    if not parts or parts[-1] == "main" or parts == ["agent"]:
        return "cli"
    if len(parts) >= 2 and parts[0] == "agent" and (len(parts) == 2 or parts[2:] == ["main"]):
        return "cli"
    return "cli" if "main" in parts else f"other:{parts[-1][:20]}"


def _ts(v) -> Optional[float]:
    if not isinstance(v, (int, float)) or v <= 0:
        return None
    return v / 1000.0 if v > 1e11 else float(v)


def load(
    db_path: Optional[str] = None,
    days: Optional[int] = 30,
    dumps_dir: Optional[str] = None,  # unused; kept for adapter interface parity
    now: Optional[float] = None,
) -> Snapshot:
    root = db_path or default_root()
    stores = [root] if root.endswith("sessions.json") else _store_paths(root)
    if not stores:
        raise FileNotFoundError(
            f"no OpenClaw session stores found under {root} "
            "(expected agents/*/sessions/sessions.json). Pass --db if it lives elsewhere."
        )
    now = now or time.time()
    since = now - days * 86400 if days else 0

    snap = Snapshot(agent="openclaw", source_path=root, generated_at=now, days=days)
    undifferentiated = 0

    for store in stores:
        try:
            with open(store, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            snap.warnings.append(f"could not read {store}: {e}")
            continue

        if isinstance(data, dict):
            items = list(data.items())
        elif isinstance(data, list):
            items = [(e.get("sessionKey") or e.get("key") or str(i), e) for i, e in enumerate(data)]
        else:
            snap.warnings.append(f"{store}: unrecognized store shape ({type(data).__name__})")
            continue

        for key, e in items:
            if not isinstance(e, dict):
                continue
            started = (
                _ts(e.get("sessionStartedAt"))
                or _ts(e.get("startedAt"))
                or _ts(e.get("lastInteractionAt"))
                or _ts(e.get("updatedAt"))
            )
            if days and started is not None and started < since:
                continue

            inp = int(e.get("inputTokens") or 0)
            out = int(e.get("outputTokens") or 0)
            cr = int(e.get("cacheRead") or 0)
            cw = int(e.get("cacheWrite") or 0)
            if inp + out + cr + cw == 0:
                total = int(e.get("totalTokens") or 0)
                if total > 0:  # store kept only the undifferentiated rollup
                    inp = total
                    undifferentiated += 1

            cost = e.get("estimatedCostUsd")
            depth = int(e.get("spawnDepth") or 0)
            sid = str(e.get("sessionId") or key)
            outcome = e.get("status") or ("aborted" if e.get("abortedLastRun") else None)
            if outcome and str(outcome).lower() not in ("running", "done"):
                snap.outcomes[sid] = str(outcome)
            snap.sessions.append(
                SessionRec(
                    id=sid,
                    source=source_from_key(str(key), depth),
                    model=e.get("modelOverride") or e.get("model"),
                    started_at=started,
                    ended_at=_ts(e.get("endedAt")),
                    parent_id=e.get("parentSessionKey"),
                    title=str(key)[:80],
                    api_calls=0,  # not recorded in the session store
                    input_tokens=inp,
                    output_tokens=out,
                    cache_read_tokens=cr,
                    cache_write_tokens=cw,
                    reasoning_tokens=0,
                    cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
                    cost_basis="estimated" if isinstance(cost, (int, float)) else "unknown",
                    message_count=1,
                    provider=e.get("providerOverride"),
                )
            )

    if not snap.sessions:
        raise RuntimeError(
            "OpenClaw store(s) found but no sessions parsed — schema may have changed; "
            "please open an issue with one (redacted) sessions.json entry."
        )

    # behavioral events from transcripts (<sessions dir>/<sessionId>.jsonl)
    _scan_transcripts(snap, stores)
    if undifferentiated:
        snap.warnings.append(
            f"{undifferentiated} OpenClaw session(s) only stored an undifferentiated total — "
            "counted as input tokens."
        )
    return snap


def _scan_transcripts(snap: Snapshot, stores: list, max_events: int = 60_000) -> None:
    """Tool-call/result events from per-session transcript JSONL files."""
    wanted = {s.id for s in snap.sessions}
    for store in stores:
        d = os.path.dirname(store)
        for sid in wanted:
            path = os.path.join(d, f"{sid}.jsonl")
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for raw in f:
                        if len(snap.events) >= max_events:
                            return
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                        content = msg.get("content")
                        if not isinstance(content, list):
                            continue
                        ts = obj.get("timestamp") or msg.get("timestamp")
                        ts = (ts / 1000.0 if isinstance(ts, (int, float)) and ts > 1e11 else ts) or None
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            kind = item.get("type")
                            if kind in ("toolCall", "tool_use", "tool-call"):
                                name = item.get("name") or item.get("toolName")
                                if not name:
                                    continue
                                snap.events.append(
                                    ActionEvent(
                                        session_id=sid,
                                        ts=ts,
                                        name=str(name)[:40],
                                        arg_key=salient_arg(item.get("arguments") or item.get("input")),
                                    )
                                )
                            elif kind in ("toolResult", "tool_result", "tool-result"):
                                is_err = bool(
                                    item.get("isError") or item.get("is_error")
                                    or str(item.get("status", "")).lower() == "error"
                                )
                                name = item.get("name") or item.get("toolName") or "tool"
                                snap.events.append(
                                    ActionEvent(
                                        session_id=sid,
                                        ts=ts,
                                        name=str(name)[:40],
                                        ok=not is_err,
                                    )
                                )
            except OSError:
                continue
