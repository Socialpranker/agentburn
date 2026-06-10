"""Adapter registry. One normalized model, one adapter per agent (~150 lines each)."""

from __future__ import annotations

from . import claude_code, hermes, openclaw

ADAPTERS = {
    "hermes": hermes,
    "openclaw": openclaw,
    "claude-code": claude_code,
}


def detect() -> list[str]:
    """Return adapter names whose data is present on this machine (registry order)."""
    return [name for name, mod in ADAPTERS.items() if mod.available()]
