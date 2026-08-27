"""Per-file parse cache.

Agent transcripts are append-only and large: a 30-day window over 3 GB of
Claude Code logs costs ~2 minutes of pure parsing, and today every command
(`report`, `limits`, `why`) pays it again from scratch. Almost none of those
files changed between two runs.

So each source file's parse result is cached under its (mtime_ns, size). A file
whose stamp still matches is loaded back as data instead of being re-parsed; a
file that grew is re-parsed whole and re-cached. The cache is a derived artifact
of files already on this machine — deleting it only costs time.

Layout: ~/.agentburn/cache/<agent>/<sha1(path)>.json, one file per source file
so a single changed transcript invalidates only itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

SCHEMA = 1
ENV_OFF = "AGENTBURN_NO_CACHE"
# Entries whose source file disappeared are swept after this long, so a machine
# that churns through projects doesn't grow an unbounded cache.
STALE_AFTER = 30 * 86400


def root(agent: str) -> str:
    return os.path.join(
        os.path.expanduser("~"), ".agentburn", "cache", agent.replace(os.sep, "_")
    )


def enabled() -> bool:
    return os.environ.get(ENV_OFF, "").strip().lower() not in ("1", "true", "yes")


def _entry_path(agent: str, path: str) -> str:
    digest = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()
    return os.path.join(root(agent), f"{digest}.json")


def stamp(path: str) -> dict:
    """Identity of a file's content, cheap enough to check on every run."""
    st = os.stat(path)
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


def get(agent: str, path: str, current: dict):
    """Cached parse for `path`, or None when absent or stale."""
    if not enabled():
        return None
    try:
        with open(_entry_path(agent, path), "r", encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if entry.get("schema") != SCHEMA or entry.get("key") != current:
        return None
    return entry.get("data")


def put(agent: str, path: str, current: dict, data) -> None:
    """Store a parse result. Never fatal: a cache miss only costs time."""
    if not enabled():
        return
    dest = _entry_path(agent, path)
    try:
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        tmp = f"{dest}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"schema": SCHEMA, "key": current, "source": path, "data": data},
                f,
                separators=(",", ":"),
            )
        os.replace(tmp, dest)  # atomic: a killed run never leaves a half entry
    except OSError:
        pass


def sweep(agent: str, now: float = None) -> int:
    """Drop entries whose source file is gone. Returns how many were removed."""
    now = now or time.time()
    directory = root(agent)
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json"):
            continue  # skips the .swept marker too
        full = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(full) < STALE_AFTER:
                continue
            with open(full, "r", encoding="utf-8") as f:
                source = json.load(f).get("source")
            if source and os.path.exists(source):
                continue
            os.remove(full)
            removed += 1
        except (OSError, json.JSONDecodeError):
            continue
    return removed


def maybe_sweep(agent: str, interval: float = 86400) -> None:
    """Run `sweep` at most once a day: it stats every entry, so it is not free."""
    marker = os.path.join(root(agent), ".swept")
    try:
        if time.time() - os.path.getmtime(marker) < interval:
            return
    except OSError:
        pass  # never swept, or no cache dir yet
    try:
        os.makedirs(root(agent), mode=0o700, exist_ok=True)
        sweep(agent)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def clear(agent: str = None) -> int:
    """Delete cached parses. Returns bytes freed."""
    base = (
        root(agent)
        if agent
        else os.path.join(os.path.expanduser("~"), ".agentburn", "cache")
    )
    freed = 0
    for dirpath, _, names in os.walk(base, topdown=False):
        for name in names:
            full = os.path.join(dirpath, name)
            try:
                freed += os.path.getsize(full)
                os.remove(full)
            except OSError:
                pass
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    return freed
