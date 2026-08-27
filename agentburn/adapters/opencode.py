"""OpenCode adapter: reads ~/.local/share/opencode/opencode.db read-only.

OpenCode stores session-level accounting in the `session` table:

    id, project_id, title, directory, model, cost,
    tokens_input, tokens_output, tokens_reasoning,
    tokens_cache_read, tokens_cache_write,
    time_created, time_updated, ...

The `message` table contains one row per conversation message and is used
only for message_count.

The database is opened read-only. WAL mode is supported by SQLite's normal
read-only URI handling.

This adapter deliberately does not infer costs or token values from
individual `part` rows. Session-level accounting is the authoritative
rollup exposed by OpenCode.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional

from ..model import SessionRec, Snapshot


def default_db_path() -> str:
    """Return the default OpenCode database location."""
    return os.path.join(
        os.path.expanduser("~"),
        ".local",
        "share",
        "opencode",
        "opencode.db",
    )


def available() -> bool:
    """Return True when the default OpenCode database exists."""
    return os.path.isfile(default_db_path())


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    """Return the available columns for a table."""
    try:
        return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _json_object(value) -> dict:
    """Decode a JSON object, returning an empty dict on malformed input."""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}

    try:
        obj = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}

    return obj if isinstance(obj, dict) else {}


def _timestamp(value) -> Optional[float]:
    """Normalize OpenCode millisecond timestamps to Unix seconds."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None

    # OpenCode stores time_created/time_updated in milliseconds.
    return value / 1000.0 if value > 1e11 else float(value)


def _model_and_provider(value) -> tuple[Optional[str], Optional[str]]:
    """Extract model ID and provider ID from OpenCode's JSON model field."""
    obj = _json_object(value)

    model = obj.get("id") or obj.get("modelID")
    provider = obj.get("providerID")

    return (
        str(model) if model is not None else None,
        str(provider) if provider is not None else None,
    )


def load(
    db_path: Optional[str] = None,
    days: Optional[int] = 30,
    dumps_dir: Optional[str] = None,  # unused; adapter interface parity
    now: Optional[float] = None,
) -> Snapshot:
    """Load OpenCode sessions from its SQLite database.

    Args:
        db_path: Optional path to opencode.db.
        days: Number of recent days to include. None/0 means no time filter.
        dumps_dir: Unused; retained for adapter interface parity.
        now: Optional Unix timestamp, useful for tests.

    Returns:
        Snapshot containing OpenCode sessions.

    Raises:
        FileNotFoundError: if the database does not exist.
        RuntimeError: if the expected session table/schema is unavailable.
    """
    path = db_path or default_db_path()

    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"OpenCode database not found at {path}. "
            "Pass --db /path/to/opencode.db or run OpenCode on this machine."
        )

    now = now or time.time()
    since = now - days * 86400 if days else 0

    snap = Snapshot(
        agent="opencode",
        source_path=path,
        generated_at=now,
        days=days,
    )

    # SQLite read-only URI. This works with OpenCode's WAL-backed database
    # without opening it for writes.
    try:
        con = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
        )
    except sqlite3.Error as exc:
        raise RuntimeError(f"could not open OpenCode database read-only: {exc}") from exc

    try:
        con.row_factory = sqlite3.Row

        session_cols = _columns(con, "session")
        if "id" not in session_cols:
            raise RuntimeError(
                "session table not found — is this really an OpenCode database? "
                "(schema may have changed; please open an issue with "
                "`PRAGMA table_info(session)` output)"
            )

        # Read the columns that are actually available. This keeps the
        # adapter reasonably tolerant of older/newer OpenCode schemas.
        def col(name: str, default: str = "NULL") -> str:
            return name if name in session_cols else f"{default} AS {name}"

        fields = ", ".join(
            [
                "id",
                col("project_id"),
                col("workspace_id"),
                col("parent_id"),
                col("title", "''"),
                col("directory", "''"),
                col("model"),
                col("cost", "0"),
                col("tokens_input", "0"),
                col("tokens_output", "0"),
                col("tokens_reasoning", "0"),
                col("tokens_cache_read", "0"),
                col("tokens_cache_write", "0"),
                col("time_created", "0"),
                col("time_updated", "0"),
            ]
        )

        # OpenCode uses milliseconds for time_created.
        # Apply the date filter in Python so the adapter remains tolerant
        # of schema changes and timestamp representations.
        rows = con.execute(f"SELECT {fields} FROM session").fetchall()

        message_cols = _columns(con, "message")
        has_message_session_id = "session_id" in message_cols

        message_counts: dict[str, int] = {}

        if has_message_session_id:
            try:
                for row in con.execute(
                    """
                    SELECT session_id, COUNT(*) AS count
                    FROM message
                    GROUP BY session_id
                    """
                ):
                    message_counts[str(row["session_id"])] = int(row["count"])
            except sqlite3.Error as exc:
                snap.warnings.append(
                    f"could not read OpenCode message counts: {exc}"
                )

        for row in rows:
            started = _timestamp(row["time_created"])
            ended = _timestamp(row["time_updated"])

            # Exclude sessions whose creation time is outside the requested
            # window. Sessions with no usable timestamp are retained rather
            # than silently discarded.
            if days and started is not None and started < since:
                continue

            model, provider = _model_and_provider(row["model"])

            cost_value = row["cost"]
            try:
                cost = float(cost_value) if cost_value is not None else None
            except (TypeError, ValueError):
                cost = None

            # OpenCode's session.cost is the recorded session-level cost.
            # A numeric zero is a real value and must not be turned into None.
            cost_basis = "actual" if cost is not None else "unknown"

            def integer(value) -> int:
                try:
                    return int(value or 0)
                except (TypeError, ValueError):
                    return 0

            sid = str(row["id"])

            snap.sessions.append(
                SessionRec(
                    id=sid,
                    source="cli",
                    model=model,
                    started_at=started,
                    ended_at=ended,
                    parent_id=(
                        str(row["parent_id"])
                        if row["parent_id"] is not None
                        else None
                    ),
                    title=str(row["title"] or sid)[:80],
                    api_calls=0,
                    input_tokens=integer(row["tokens_input"]),
                    output_tokens=integer(row["tokens_output"]),
                    cache_read_tokens=integer(row["tokens_cache_read"]),
                    cache_write_tokens=integer(row["tokens_cache_write"]),
                    reasoning_tokens=integer(row["tokens_reasoning"]),
                    cost_usd=cost,
                    cost_basis=cost_basis,
                    message_count=message_counts.get(sid, 0),
                    provider=provider,
                )
            )

    except sqlite3.Error as exc:
        raise RuntimeError(f"could not read OpenCode database: {exc}") from exc
    finally:
        con.close()

    if not snap.sessions:
        raise RuntimeError(
            "OpenCode database found but no sessions were parsed in the requested "
            "window."
        )

    return snap