"""Normalized, agent-agnostic data model.

Every adapter converts its agent's storage into these records. The analyzer
never sees agent-specific structures — that is what keeps the core reusable
for OpenClaw / Claude Code adapters later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SessionRec:
    id: str
    source: str  # normalized: cli | cron | subagent | gateway:<platform> | other:<raw>
    model: Optional[str]
    started_at: Optional[float]  # unix seconds
    ended_at: Optional[float]
    parent_id: Optional[str]
    title: Optional[str]
    api_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    cost_usd: Optional[float]  # actual > estimated > None
    cost_basis: str  # "actual" | "estimated" | "unknown"
    message_count: int = 0
    provider: Optional[str] = None  # billing provider, for doctor diagnostics

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.reasoning_tokens
        )


@dataclass
class ToolStat:
    name: str
    calls: int
    result_tokens: int  # tokens of tool results carried into context


@dataclass
class ActionEvent:
    """One observed agent action (tool call), normalized across agents."""

    session_id: str
    ts: Optional[float]
    name: str  # tool name
    arg_key: Optional[str] = (
        None  # salient argument (file path / command / url), truncated
    )
    ok: Optional[bool] = None  # False when the agent recorded an error result
    tokens: Optional[int] = None  # result weight when the agent recorded it


# Usage is bucketed at this resolution before it reaches the analyzer. Fine
# enough for a rolling 5-hour window (60 buckets), coarse enough that a month
# of heavy use stays a few thousand cells instead of a million call records.
BUCKET_SECONDS = 300


@dataclass
class UsageCell:
    """Usage inside one time bucket, split by source and model.

    Subscription limits are windowed, and a single session routinely spans
    several windows — so `SessionRec` totals cannot answer "how full is the
    current window". Adapters that can see per-call timestamps fill these;
    the others leave the list empty and `agentburn limits` says so instead of
    guessing.
    """

    start: int  # unix seconds, floored to BUCKET_SECONDS
    source: str
    model: Optional[str]
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class DumpComposition:
    """Input composition sampled from request dumps (optional, exact-ish)."""

    samples: int
    system_share: float
    tools_share: float
    history_share: float


_AGENT_LABELS = {
    "codex": "Codex CLI",
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "claude-code": "Claude Code",
}

# Storage the user would name in an upstream bug report, per agent.
_AGENT_STORES = {
    "codex": "`~/.codex/sessions/**/*.jsonl`",
    "hermes": "`~/.hermes/state.db`",
    "openclaw": "the local transcript store",
    "claude-code": "`~/.claude/projects/**.jsonl`",
}


def agent_key(agent: str) -> str:
    """Bare adapter key. `behavior` may append ` · <project>` to Snapshot.agent."""
    return agent.split(" · ", 1)[0]


def agent_label(agent: str) -> str:
    """Human-facing agent name ("claude-code" → "Claude Code")."""
    key = agent_key(agent)
    return _AGENT_LABELS.get(key, key)


def agent_store(agent: str) -> str:
    """How to refer to this agent's local data in a report."""
    return _AGENT_STORES.get(agent_key(agent), "its local store")


@dataclass
class Snapshot:
    agent: str  # "hermes" | "openclaw" | "claude-code"
    source_path: str
    generated_at: float
    days: Optional[int]
    sessions: list[SessionRec] = field(default_factory=list)
    tools: list[ToolStat] = field(default_factory=list)
    composition: Optional[DumpComposition] = None
    warnings: list[str] = field(default_factory=list)
    # behavioral layer (filled when the adapter can see actions/outcomes)
    events: list[ActionEvent] = field(default_factory=list)
    outcomes: dict = field(
        default_factory=dict
    )  # session_id → "failed" | "timeout" | …
    compactions: dict = field(
        default_factory=dict
    )  # session_id → count of context compactions
    # windowed usage (only adapters with per-call timestamps fill this)
    usage_cells: list[UsageCell] = field(default_factory=list)
