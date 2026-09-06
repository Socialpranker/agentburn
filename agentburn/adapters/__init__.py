"""Adapter registry. One normalized model, one adapter per agent (~150 lines each)."""

from __future__ import annotations

from . import claude_code, codex, gemini, hermes, openclaw, opencode

ADAPTERS = {
    "hermes": hermes,
    "openclaw": openclaw,
    "claude-code": claude_code,
    "codex": codex,
    "gemini": gemini,
    "opencode": opencode,
}


def detect() -> list[str]:
    """Return adapter names whose data is present on this machine (registry order)."""
    return [name for name, mod in ADAPTERS.items() if mod.available()]
