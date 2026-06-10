"""Embedded model-price snapshot (USD per 1M tokens), pulled from the
OpenRouter endpoints API on AS_OF — median endpoint per model, not the
cheapest outlier. No network at runtime; every figure that uses these is
labeled with the snapshot date. Verify current rates before big decisions.
"""

from __future__ import annotations

import re
from typing import Optional

AS_OF = "2026-06-10"

# slug → (prompt_usd_per_mtok, completion_usd_per_mtok)
PRICES = {
    "anthropic/claude-opus-4.6": (5.0, 25.0),
    "anthropic/claude-sonnet-4.6": (3.0, 15.0),
    "deepseek/deepseek-chat": (0.32, 0.89),
    "deepseek/deepseek-v3.2": (0.269, 0.4),
    "google/gemini-3-flash-preview": (0.5, 3.0),
    "meta-llama/llama-3.1-8b-instruct": (0.05, 0.08),
    "minimax/minimax-m2.7": (0.3, 1.2),
    "moonshotai/kimi-k2.5": (0.54, 2.7),
    "openai/gpt-5.4": (2.5, 15.0),
    "qwen/qwen3.6-plus": (0.325, 1.95),
    "stepfun/step-3.5-flash": (0.09, 0.3),
    "z-ai/glm-5-turbo": (1.2, 4.0),
}

CHEAP_REFERENCE = "deepseek/deepseek-chat"

_DATE_SUFFIX = re.compile(r"[-:]\d{8}$|[-:]\d{4}$|[-:]20\d{2}-?\d{2}-?\d{2}$")


def _norm(model: str) -> str:
    m = (model or "").strip().lower()
    m = m.split(":free")[0]
    m = _DATE_SUFFIX.sub("", m)
    return m


def lookup(model: str) -> Optional[tuple]:
    """Price for a model slug; tolerant to date suffixes and reorderings
    (anthropic/claude-4.6-opus-20260205 → anthropic/claude-opus-4.6)."""
    m = _norm(model)
    if m in PRICES:
        return PRICES[m]
    for slug, p in PRICES.items():
        if m.startswith(slug) or slug.startswith(m):
            return p
    # token-set match within the same author (handles "claude-4.6-opus" vs "claude-opus-4.6")
    if "/" in m:
        author, name = m.split("/", 1)
        toks = set(re.split(r"[-.]", name)) - {""}
        for slug, p in PRICES.items():
            sa, sn = slug.split("/", 1)
            if sa == author and toks and toks == (set(re.split(r"[-.]", sn)) - {""}):
                return p
    return None


def cost_usd(model: str, in_tokens: float, out_tokens: float) -> Optional[float]:
    p = lookup(model)
    if not p:
        return None
    return in_tokens / 1e6 * p[0] + out_tokens / 1e6 * p[1]


def cheap_cost_usd(in_tokens: float, out_tokens: float) -> float:
    p = PRICES[CHEAP_REFERENCE]
    return in_tokens / 1e6 * p[0] + out_tokens / 1e6 * p[1]
