"""`agentburn explain` — optional LLM interpretation of the numbers.

Privacy rules (non-negotiable):
- default endpoint is LOCAL (ollama / LM Studio at localhost) — nothing leaves
  the machine, same promise as everywhere else;
- a remote endpoint must be chosen explicitly, triggers a visible notice, and
  receives a REDACTED payload only: session titles become session-1/2/…,
  file paths shrink to basenames — the same anonymity level as the share card;
- zero dependencies: plain stdlib HTTP to any OpenAI-compatible /chat/completions.

Yes, a cost profiler that spends ~3K tokens explaining costs. We keep the
payload compact and the answer short; the irony is documented.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = "http://localhost:11434/v1"  # ollama

SYSTEM_PROMPT = (
    "You are a pragmatic cost analyst for always-on AI agents. You receive JSON from "
    "agentburn: a spend report (by source: cron/heartbeat/gateways/subagents/cli; models; "
    "overnight window; fixed overhead per call) and behavioral findings (functions called, "
    "re-read loops, retry storms, idle heartbeats, failed-run costs). "
    "Write for the agent's OWNER, plain language, no jargon. Structure: "
    "(1) 2-4 sentences: where the money actually goes and the single biggest leak; "
    "(2) the 3 highest-impact actions, ranked, each with the expected monthly effect when "
    "the data supports an estimate — never invent numbers not derivable from the input; "
    "(3) one sentence on data quality if warnings are present. "
    "Be concrete, cite the numbers you base claims on, stay under 250 words."
)


def is_local(base_url: str) -> bool:
    host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def redact(payload: dict) -> dict:
    """Strip identifying bits for remote endpoints: titles → session-N, paths → basenames."""
    p = json.loads(json.dumps(payload))  # deep copy
    names = {}

    def alias(title: str) -> str:
        if title not in names:
            names[title] = f"session-{len(names) + 1}"
        return names[title]

    rep = p.get("report") or {}
    for r in rep.get("subagent_rollups") or []:
        r["title"] = alias(r.get("title", ""))
    why = p.get("why") or {}
    for r in why.get("rereads") or []:
        r["session"] = alias(r.get("session", ""))
        if r.get("arg"):
            r["arg"] = str(r["arg"]).replace("\\", "/").rsplit("/", 1)[-1][:40]
    for s in why.get("storms") or []:
        s["session"] = alias(s.get("session", ""))
    fc = why.get("failure_cost") or {}
    fc["examples"] = [alias(e) for e in fc.get("examples") or []]
    for r in why.get("reasoning_heavy") or []:
        r["session"] = alias(r.get("session", ""))
    return p


def compact(payload: dict, limit: int = 14_000) -> str:
    s = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return s[:limit]


def interpret(
    payload: dict,
    base_url: str,
    model: str,
    api_key: str = "",
    lang: str = "en",
    timeout: int = 90,
) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Respond in language: {lang}.\n\nagentburn data:\n{compact(payload)}",
            },
        ],
        "temperature": 0.2,
        "max_tokens": 600,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            **({"authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"LLM endpoint returned HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            f"cannot reach LLM endpoint {base_url} ({e}). Start a local model "
            "(e.g. `ollama serve` + `ollama pull llama3.1`) or pass --llm/--model "
            "for an OpenAI-compatible endpoint."
        ) from None
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected LLM response shape: {str(data)[:200]}") from None


def resolve_endpoint(args_llm, args_model, env=os.environ):
    """→ (base_url, model, api_key, local). Explicit flags > env > local ollama."""
    base = args_llm or env.get("AGENTBURN_LLM_BASE") or DEFAULT_BASE
    model = args_model or env.get("AGENTBURN_LLM_MODEL") or ""
    key = (
        env.get("AGENTBURN_LLM_KEY")
        or env.get("OPENROUTER_API_KEY")
        or env.get("OPENAI_API_KEY")
        or ""
    )
    return base, model, key, is_local(base)
