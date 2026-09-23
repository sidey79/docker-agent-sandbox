"""SQLite job store.

Deliberately plain sqlite3 rather than an ORM: there is one table, the queries
fit on a line each, and the dispatcher's job is to talk to Docker, not to model
data. Every call takes its own short-lived connection, which keeps the store
usable from the worker threads without sharing handles across them.
"""

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Terminal states. A job in any of these is never picked up by a worker again.
DONE_STATES = ("succeeded", "failed", "timeout", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    task          TEXT NOT NULL,
    repo_url      TEXT,
    repo_ref      TEXT,
    callback_url  TEXT,
    container_id  TEXT,
    volume_name   TEXT,
    exit_code     INTEGER,
    result        TEXT,
    logs          TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs (status, created_at);
"""


class JobStore:
    def __init__(self, path: str) -> None:
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL lets the API read while a worker writes, which is the whole
        # concurrency story this store needs.
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def create(
        self,
        job_id: str,
        task: str,
        repo_url: str | None,
        repo_ref: str | None,
        callback_url: str | None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, status, created_at, task, repo_url, repo_ref, callback_url)"
                " VALUES (?, 'queued', ?, ?, ?, ?, ?)",
                (job_id, time.time(), task, repo_url, repo_ref, callback_url),
            )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "result" in fields and fields["result"] is not None:
            fields["result"] = json.dumps(fields["result"])
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*fields.values(), job_id),
            )

    def queued_ids(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
        return [row["id"] for row in rows]

    def fail_orphans(self, message: str) -> int:
        """Mark jobs left `running` by a previous process.

        Their containers are gone with the dispatcher that was watching them, so
        there is nothing left to wait on. Saying so is better than leaving a job
        that will never finish.
        """
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, finished_at = ?"
                " WHERE status = 'running'",
                (message, time.time()),
            )
            return cursor.rowcount


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    if job.get("result"):
        try:
            job["result"] = json.loads(job["result"])
        except json.JSONDecodeError:
            # A malformed result is still worth returning verbatim; losing it
            # would hide exactly the case someone is debugging.
            pass
    return job
