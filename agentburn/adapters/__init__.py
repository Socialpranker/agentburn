"""Adapter registry. v0.1 ships Hermes; OpenClaw and Claude Code are next."""

from __future__ import annotations

from . import hermes

ADAPTERS = {
    "hermes": hermes,
}


def detect() -> list[str]:
    """Return adapter names whose data is present on this machine."""
    return [name for name, mod in ADAPTERS.items() if mod.available()]
