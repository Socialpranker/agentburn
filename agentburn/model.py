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
    project: Optional[str] = None  # working directory the session ran in, when recorded
    branch: Optional[str] = None  # git branch, when the agent records it

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
    session: str = ""  # owning session id, so a window can be split by project


@dataclass
class LimitHit:
    """The agent itself recorded that a usage limit was reached.

    Claude Code writes a synthetic assistant turn ("You've hit your session
    limit · resets 8:30pm (Europe/Amsterdam)") at the moment of the cut-off.
    That moment is a measured wall: the window that ended there is a ceiling
    nobody had to guess. `reset_at` is the window's scheduled end when the
    message stated one and it could be placed on the clock.
    """

    ts: float
    kind: str  # "session" (rolling 5h) | "weekly" | other wording, lowercased
    reset_at: Optional[float] = None


@dataclass
class RateLimitSample:
    """The provider's own reading of a usage window, as the agent recorded it.

    Codex writes `rate_limits.{primary,secondary}.used_percent` with every
    token count. Paired with our weighted usage in the same window that is a
    measured ceiling: weight_in_window / used_percent × 100.
    """

    ts: float
    window_minutes: int
    used_percent: float
    resets_at: Optional[float] = None


@dataclass
class ContextCall:
    """One API call's context size: what the model had to read before answering.

    On a subscription the context is the window: a call at 300k context costs
    the same cache-read volume as three calls at 100k. Adapters that see
    per-call usage fill these; `agentburn context` turns them into the price
    of long sessions and the saving of a `/clear` at a threshold.
    """

    ts: Optional[float]
    session: str
    model: Optional[str]
    context: int  # input + cache read + cache write, i.e. everything re-read
    output: int
    effort: Optional[str] = None


@dataclass
class SkillLoad:
    """A skill invocation and how much context it added (measured, not read
    from disk: bundled skills never touch the disk)."""

    session: str
    ts: Optional[float]
    skill: str
    tokens: int


@dataclass
class DumpComposition:
    """Input composition sampled from request dumps (optional, exact-ish)."""

    samples: int
    system_share: float
    tools_share: float
    history_share: float


_AGENT_LABELS = {
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "claude-code": "Claude Code",
    "codex": "Codex CLI",
    "gemini": "Gemini CLI",
    "opencode": "opencode",
}

# Storage the user would name in an upstream bug report, per agent.
_AGENT_STORES = {
    "hermes": "`~/.hermes/state.db`",
    "openclaw": "the local transcript store",
    "claude-code": "`~/.claude/projects/**.jsonl`",
    "codex": "`~/.codex/sessions/**/rollout-*.jsonl`",
    "gemini": "`~/.gemini/tmp/*/chats/session-*.json`",
    "opencode": "`~/.local/share/opencode/opencode.db`",
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
    agent: str  # "hermes" | "openclaw" | "claude-code" | "codex" | "gemini" | "opencode"
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
    # moments the agent itself recorded a limit cut-off (measured ceilings)
    limit_hits: list = field(default_factory=list)  # LimitHit
    # per-call context sizes (only adapters with per-call usage fill this)
    context_calls: list = field(default_factory=list)  # ContextCall
    # skill invocations with their measured context cost
    skill_loads: list = field(default_factory=list)  # SkillLoad
    # the provider's own window readings, when the agent records them
    rate_limits: list = field(default_factory=list)  # RateLimitSample
