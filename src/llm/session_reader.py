"""Session and conversation history persistence via SQLite."""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSION_TIMEOUT_SECONDS = 1800  # 30 minutes


class SessionReader:
    """Manages SQLite-backed session tracking and turn persistence."""

    def __init__(self, db_path: str | Path) -> None:
        """Initialize SQLite database and verify schema.

        Args:
            db_path: Path to SQLite database file or ':memory:'.
        """
        self.db_path = db_path
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """Create required tables if missing."""
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    started_at TIMESTAMP NOT NULL
                );
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    user_text TEXT NOT NULL,
                    at_text TEXT NOT NULL,
                    metrics_json TEXT,
                    is_empty INTEGER DEFAULT 0,
                    is_error INTEGER DEFAULT 0
                );
                """
            )

    def get_or_create_session(self) -> str:
        """Retrieve current active session ID or create a new one.

        Creates a new session if none exists or if the last activity was > 30 min ago.

        Returns:
            Session ID string.
        """
        cursor = self._conn.cursor()
        now = datetime.now(timezone.utc)

        # Check latest turn
        cursor.execute(
            "SELECT session_id, timestamp FROM turns ORDER BY id DESC LIMIT 1"
        )
        turn_row = cursor.fetchone()
        if turn_row is not None:
            sess_id = turn_row["session_id"]
            ts_str = turn_row["timestamp"]
            try:
                last_ts = datetime.fromisoformat(ts_str)
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                if (now - last_ts).total_seconds() <= SESSION_TIMEOUT_SECONDS:
                    return sess_id
            except Exception as exc:
                logger.warning("Failed to parse turn timestamp '%s': %s", ts_str, exc)
        else:
            # No turns yet, check latest session created
            cursor.execute(
                "SELECT id, started_at FROM sessions ORDER BY started_at DESC LIMIT 1"
            )
            sess_row = cursor.fetchone()
            if sess_row is not None:
                sess_id = sess_row["id"]
                ts_str = sess_row["started_at"]
                try:
                    start_ts = datetime.fromisoformat(ts_str)
                    if start_ts.tzinfo is None:
                        start_ts = start_ts.replace(tzinfo=timezone.utc)
                    if (now - start_ts).total_seconds() <= SESSION_TIMEOUT_SECONDS:
                        return sess_id
                except Exception as exc:
                    logger.warning(
                        "Failed to parse session timestamp '%s': %s", ts_str, exc
                    )

        # Create new session
        new_id = str(uuid.uuid4())
        with self._conn:
            self._conn.execute(
                "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
                (new_id, now.isoformat()),
            )
        logger.info("Created new session %s", new_id)
        return new_id

    def get_recent_turns(
        self, session_id: str, max_turns: int = 40
    ) -> list[dict[str, str]]:
        """Retrieve recent turns in chronological order as chat messages.

        Args:
            session_id: Session ID to filter.
            max_turns: Maximum number of turns (rows) to retrieve.

        Returns:
            Chronological list of {"role": "user"|"assistant", "content": ...} dicts.
        """
        cursor = self._conn.cursor()
        cursor.execute(
            """
            SELECT user_text, at_text
            FROM turns
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, max_turns),
        )
        rows = cursor.fetchall()

        turns: list[dict[str, str]] = []
        for row in reversed(rows):
            turns.append({"role": "user", "content": row["user_text"]})
            turns.append({"role": "assistant", "content": row["at_text"]})
        return turns

    def write_turn(
        self,
        session_id: str,
        user_text: str,
        at_text: str,
        metrics: dict[str, Any] | None = None,
        is_empty: int = 0,
        is_error: int = 0,
    ) -> None:
        """Record completed turn to database.

        Args:
            session_id: Current session ID.
            user_text: Incoming user message.
            at_text: Generated assistant response.
            metrics: Generation and post-processing metrics.
            is_empty: Flag indicating empty response.
            is_error: Flag indicating generation error.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        metrics_json = json.dumps(metrics) if metrics is not None else "{}"

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO turns (
                    session_id,
                    timestamp,
                    user_text,
                    at_text,
                    metrics_json,
                    is_empty,
                    is_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    now_iso,
                    user_text,
                    at_text,
                    metrics_json,
                    is_empty,
                    is_error,
                ),
            )

    def close(self) -> None:
        """Close database connection."""
        self._conn.close()
