from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Episode:
    id: str
    title: str
    slug: str
    pub_date: str
    r2_key: str
    feed_slug: str
    category: str
    source_tag: str | None
    preset_name: str
    source_url: str | None
    size_bytes: int
    duration_seconds: int | None
    summary: str | None = None
    articles_json: str | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    pub_date TEXT NOT NULL,
    r2_key TEXT NOT NULL,
    feed_slug TEXT NOT NULL DEFAULT 'general',
    category TEXT NOT NULL DEFAULT 'News',
    source_tag TEXT,
    preset_name TEXT NOT NULL DEFAULT 'General Newsletter',
    source_url TEXT,
    size_bytes INTEGER NOT NULL,
    duration_seconds INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS processed_emails (
    r2_key TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pending_things_happen (
    id TEXT PRIMARY KEY,
    email_r2_key TEXT NOT NULL,
    date_str TEXT NOT NULL,
    links_json TEXT NOT NULL,
    process_after TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pending_fp_digest (
    id TEXT PRIMARY KEY,
    date_str TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    process_after TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pending_the_rundown (
    id TEXT PRIMARY KEY,
    date_str TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    process_after TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        existing_cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(episodes)").fetchall()
        }

        feed_slug_ddl = (
            "ALTER TABLE episodes ADD COLUMN feed_slug TEXT NOT NULL DEFAULT 'general'"
        )
        category_ddl = (
            "ALTER TABLE episodes ADD COLUMN category TEXT NOT NULL DEFAULT 'News'"
        )
        preset_name_ddl = (
            "ALTER TABLE episodes "
            "ADD COLUMN preset_name TEXT NOT NULL DEFAULT 'General Newsletter'"
        )
        migrations = {
            "feed_slug": feed_slug_ddl,
            "category": category_ddl,
            "source_tag": "ALTER TABLE episodes ADD COLUMN source_tag TEXT",
            "preset_name": preset_name_ddl,
            "source_url": "ALTER TABLE episodes ADD COLUMN source_url TEXT",
            "summary": "ALTER TABLE episodes ADD COLUMN summary TEXT",
            "articles_json": "ALTER TABLE episodes ADD COLUMN articles_json TEXT",
        }
        for column, ddl in migrations.items():
            if column not in existing_cols:
                self._conn.execute(ddl)

    def close(self) -> None:
        self._conn.close()

    def is_processed(self, r2_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_emails WHERE r2_key = ?",
            (r2_key,),
        ).fetchone()
        return row is not None

    def mark_processed(self, r2_key: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO processed_emails (r2_key) VALUES (?)",
            (r2_key,),
        )
        self._conn.commit()

    def insert_episode(self, episode: Episode) -> None:
        self._conn.execute(
            """
            INSERT INTO episodes
                (
                    id,
                    title,
                    slug,
                    pub_date,
                    r2_key,
                    feed_slug,
                    category,
                    source_tag,
                    preset_name,
                    source_url,
                    size_bytes,
                    duration_seconds,
                    summary,
                    articles_json
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode.id,
                episode.title,
                episode.slug,
                episode.pub_date,
                episode.r2_key,
                episode.feed_slug,
                episode.category,
                episode.source_tag,
                episode.preset_name,
                episode.source_url,
                episode.size_bytes,
                episode.duration_seconds,
                episode.summary,
                episode.articles_json,
            ),
        )
        self._conn.commit()

    def list_episodes(self, feed_slug: str | None = None) -> list[Episode]:
        if feed_slug:
            rows = self._conn.execute(
                """
                SELECT
                    id,
                    title,
                    slug,
                    pub_date,
                    r2_key,
                    feed_slug,
                    category,
                    source_tag,
                    preset_name,
                    source_url,
                    size_bytes,
                    duration_seconds,
                    summary,
                    articles_json
                FROM episodes
                WHERE feed_slug = ?
                ORDER BY created_at DESC
                """,
                (feed_slug,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT
                    id,
                    title,
                    slug,
                    pub_date,
                    r2_key,
                    feed_slug,
                    category,
                    source_tag,
                    preset_name,
                    source_url,
                    size_bytes,
                    duration_seconds,
                    summary,
                    articles_json
                FROM episodes
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [
            Episode(
                id=row["id"],
                title=row["title"],
                slug=row["slug"],
                pub_date=row["pub_date"],
                r2_key=row["r2_key"],
                feed_slug=row["feed_slug"],
                category=row["category"],
                source_tag=row["source_tag"],
                preset_name=row["preset_name"],
                source_url=row["source_url"],
                size_bytes=row["size_bytes"],
                duration_seconds=row["duration_seconds"],
                summary=row["summary"],
                articles_json=row["articles_json"],
            )
            for row in rows
        ]

    def list_feed_slugs(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT feed_slug FROM episodes ORDER BY feed_slug ASC"
        ).fetchall()
        return [str(row["feed_slug"]) for row in rows if row["feed_slug"]]

    def days_since_last_episode(self, feed_slug: str) -> int | None:
        """Return days since the last episode for a feed.

        Returns None if no episodes exist or all dates are unparseable.
        """
        rows = self._conn.execute(
            "SELECT pub_date FROM episodes WHERE feed_slug = ?",
            (feed_slug,),
        ).fetchall()
        if not rows:
            return None
        dates: list[datetime] = []
        for r in rows:
            try:
                dates.append(parsedate_to_datetime(r["pub_date"]).astimezone(UTC))
            except Exception:
                pass
        if not dates:
            return None
        latest = max(dates)
        return (datetime.now(tz=UTC) - latest).days

    def insert_pending_things_happen(
        self,
        email_r2_key: str,
        date_str: str,
        links_json: str,
        delay_hours: int = 0,
    ) -> str:
        job_id = str(uuid.uuid4())
        process_after = (
            datetime.now(tz=UTC) + timedelta(hours=delay_hours)
        ).isoformat()
        self._conn.execute(
            """INSERT INTO pending_things_happen
               (id, email_r2_key, date_str, links_json, process_after, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, email_r2_key, date_str, links_json, process_after, "pending"),
        )
        self._conn.commit()
        return job_id

    def list_due_things_happen(self) -> list[dict]:
        now = datetime.now(tz=UTC).isoformat()
        rows = self._conn.execute(
            """SELECT id, email_r2_key, date_str, links_json, process_after
               FROM pending_things_happen
               WHERE status = 'pending' AND process_after <= ?
               ORDER BY process_after ASC""",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_things_happen_completed(self, job_id: str) -> None:
        self._conn.execute(
            "UPDATE pending_things_happen SET status = 'completed' WHERE id = ?",
            (job_id,),
        )
        self._conn.commit()

    def insert_pending_fp_digest(self, date_str: str) -> str | None:
        """Insert a pending fp_digest job.

        Return job_id or None if date already exists.
        """
        job_id = str(uuid.uuid4())
        process_after = datetime.now(tz=UTC).isoformat()
        try:
            self._conn.execute(
                """INSERT INTO pending_fp_digest
                   (id, date_str, status, process_after)
                   VALUES (?, ?, ?, ?)""",
                (job_id, date_str, "pending", process_after),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            return None
        return job_id

    def list_due_fp_digest(self) -> list[dict]:
        """Return pending fp_digest jobs where process_after <= now."""
        now = datetime.now(tz=UTC).isoformat()
        rows = self._conn.execute(
            """SELECT id, date_str, status, process_after
               FROM pending_fp_digest
               WHERE status = 'pending' AND process_after <= ?
               ORDER BY process_after ASC""",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_fp_digest_completed(self, job_id: str) -> None:
        """Set fp_digest job status to 'completed'."""
        self._conn.execute(
            "UPDATE pending_fp_digest SET status = 'completed' WHERE id = ?",
            (job_id,),
        )
        self._conn.commit()

    def insert_pending_the_rundown(self, date_str: str) -> str | None:
        """Insert a pending the_rundown job.

        Return job_id or None if date already exists.
        """
        job_id = str(uuid.uuid4())
        process_after = datetime.now(tz=UTC).isoformat()
        try:
            self._conn.execute(
                """INSERT INTO pending_the_rundown
                   (id, date_str, status, process_after)
                   VALUES (?, ?, ?, ?)""",
                (job_id, date_str, "pending", process_after),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            return None
        return job_id

    def list_due_the_rundown(self) -> list[dict]:
        """Return pending the_rundown jobs where process_after <= now."""
        now = datetime.now(tz=UTC).isoformat()
        rows = self._conn.execute(
            """SELECT id, date_str, status, process_after
               FROM pending_the_rundown
               WHERE status = 'pending' AND process_after <= ?
               ORDER BY process_after ASC""",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_the_rundown_completed(self, job_id: str) -> None:
        """Set the_rundown job status to 'completed'."""
        self._conn.execute(
            "UPDATE pending_the_rundown SET status = 'completed' WHERE id = ?",
            (job_id,),
        )
        self._conn.commit()
