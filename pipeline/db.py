from __future__ import annotations

import json
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
    show_notes_html: str | None = None


@dataclass(frozen=True)
class RetryUpdate:
    failure_count: int
    process_after: str | None
    status: str
    exhausted: bool


MAX_RETRY_FAILURES = 51


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
    summary TEXT,
    articles_json TEXT,
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
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pending_the_rundown (
    id TEXT PRIMARY KEY,
    date_str TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    process_after TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
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
            "show_notes_html": "ALTER TABLE episodes ADD COLUMN show_notes_html TEXT",
        }
        for column, ddl in migrations.items():
            if column not in existing_cols:
                self._conn.execute(ddl)

        self._ensure_pending_retry_columns("pending_fp_digest")
        self._ensure_pending_retry_columns("pending_the_rundown")

    def _ensure_pending_retry_columns(self, table_name: str) -> None:
        existing_cols = {
            row["name"]
            for row in self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if "failure_count" not in existing_cols:
            self._conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
            )
        if "last_error" not in existing_cols:
            self._conn.execute(f"ALTER TABLE {table_name} ADD COLUMN last_error TEXT")

    def _next_retry_process_after(self, failure_count: int) -> str:
        # Backoff schedule: 1m, 2m, 4m, 8m, then cap at 15m.
        delay_minutes = min(15, 2 ** max(0, failure_count - 1))
        return (datetime.now(tz=UTC) + timedelta(minutes=delay_minutes)).isoformat()

    def _mark_pending_job_failed(
        self, table_name: str, job_id: str, error: str
    ) -> RetryUpdate:
        row = self._conn.execute(
            f"SELECT failure_count FROM {table_name} WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown job id: {job_id}")
        failure_count = int(row["failure_count"] or 0) + 1
        if failure_count >= MAX_RETRY_FAILURES:
            self._conn.execute(
                f"UPDATE {table_name} SET status = 'errored', failure_count = ?, last_error = ? WHERE id = ?",
                (failure_count, error[:500], job_id),
            )
            self._conn.commit()
            return RetryUpdate(
                failure_count=failure_count,
                process_after=None,
                status="errored",
                exhausted=True,
            )
        process_after = self._next_retry_process_after(failure_count)
        self._conn.execute(
            f"UPDATE {table_name} SET status = 'pending', failure_count = ?, last_error = ?, process_after = ? WHERE id = ?",
            (failure_count, error[:500], process_after, job_id),
        )
        self._conn.commit()
        return RetryUpdate(
            failure_count=failure_count,
            process_after=process_after,
            status="pending",
            exhausted=False,
        )

    def _mark_pending_job_completed(self, table_name: str, job_id: str) -> None:
        self._conn.execute(
            f"UPDATE {table_name} SET status = 'completed', failure_count = 0, last_error = NULL WHERE id = ?",
            (job_id,),
        )
        self._conn.commit()

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
                    articles_json,
                    show_notes_html
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                episode.show_notes_html,
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
                    articles_json,
                    show_notes_html
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
                    articles_json,
                    show_notes_html
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
                show_notes_html=row["show_notes_html"],
            )
            for row in rows
        ]

    def recent_coverage_summary(self, feed_slug: str, days: int = 3) -> list[dict]:
        """Return coverage frequency of themes from recent episodes.

        Queries articles_json from the most recent episodes within the
        given day window.  Returns a list of dicts:
          {"theme": str, "days_covered": int, "article_count": int,
           "episode_dates": list[str], "was_lead": bool}
        sorted by days_covered descending, then article_count descending.
        """
        episodes = self.list_episodes(feed_slug=feed_slug)
        now = datetime.now(tz=UTC)
        cutoff = now - timedelta(days=days)

        theme_stats: dict[str, dict] = {}

        for ep in episodes:
            try:
                ep_dt = parsedate_to_datetime(ep.pub_date)
            except Exception:
                continue
            if ep_dt < cutoff:
                continue
            if not ep.articles_json:
                continue
            try:
                articles = json.loads(ep.articles_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not articles:
                continue

            date_str = ep_dt.strftime("%Y-%m-%d")
            lead_theme = articles[0].get("theme", "") if articles else ""

            seen_themes_this_ep: dict[str, int] = {}
            for art in articles:
                theme = art.get("theme", "")
                if not theme:
                    continue
                seen_themes_this_ep[theme] = seen_themes_this_ep.get(theme, 0) + 1

            for theme, count in seen_themes_this_ep.items():
                if theme not in theme_stats:
                    theme_stats[theme] = {
                        "dates": set(),
                        "articles": 0,
                        "lead_count": 0,
                    }
                theme_stats[theme]["dates"].add(date_str)
                theme_stats[theme]["articles"] += count
                if theme == lead_theme:
                    theme_stats[theme]["lead_count"] += 1

        result = []
        for theme, stats in theme_stats.items():
            result.append(
                {
                    "theme": theme,
                    "days_covered": len(stats["dates"]),
                    "article_count": stats["articles"],
                    "episode_dates": sorted(stats["dates"]),
                    "was_lead": stats["lead_count"] > 0,
                }
            )

        result.sort(key=lambda r: (-r["days_covered"], -r["article_count"]))
        return result

    def recent_article_urls(self, feed_slug: str, days: int = 3) -> set[str]:
        """Return URLs of articles used in recent episodes.

        Queries articles_json from episodes within the given day window
        and extracts all non-null URLs.  Used to deduplicate across days.
        """
        episodes = self.list_episodes(feed_slug=feed_slug)
        now = datetime.now(tz=UTC)
        cutoff = now - timedelta(days=days)

        urls: set[str] = set()
        for ep in episodes:
            try:
                ep_dt = parsedate_to_datetime(ep.pub_date)
            except Exception:
                continue
            if ep_dt < cutoff:
                continue
            if not ep.articles_json:
                continue
            try:
                articles = json.loads(ep.articles_json)
            except (json.JSONDecodeError, TypeError):
                continue
            for art in articles:
                url = art.get("url")
                if url:
                    urls.add(url)
        return urls

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
        self._mark_pending_job_completed("pending_fp_digest", job_id)

    def mark_fp_digest_failed(self, job_id: str, error: str) -> RetryUpdate:
        """Increment failure count and schedule the next fp_digest retry."""
        return self._mark_pending_job_failed("pending_fp_digest", job_id, error)

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
        self._mark_pending_job_completed("pending_the_rundown", job_id)

    def mark_the_rundown_failed(self, job_id: str, error: str) -> RetryUpdate:
        """Increment failure count and schedule the next the_rundown retry."""
        return self._mark_pending_job_failed("pending_the_rundown", job_id, error)

    # ------------------------------------------------------------------ #
    # Shared daily-job helpers                                             #
    # ------------------------------------------------------------------ #

    _FEED_SLUG_TO_TABLE: dict[str, str] = {
        "fp-digest": "pending_fp_digest",
        "the-rundown": "pending_the_rundown",
    }

    def list_daily_jobs(self, feed_slug: str, status: str) -> list[dict]:
        """Return daily jobs for *feed_slug* filtered by *status*.

        Returns a list of row dicts ordered by date_str descending.
        Raises ValueError for unknown feed slugs.
        """
        table = self._FEED_SLUG_TO_TABLE.get(feed_slug)
        if table is None:
            raise ValueError(f"Unknown feed_slug: {feed_slug!r}")
        rows = self._conn.execute(
            f"SELECT id, date_str, status, process_after, failure_count, last_error"
            f" FROM {table}"
            f" WHERE status = ?"
            f" ORDER BY date_str DESC",
            (status,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _reset_daily_job(self, table: str, job_id: str) -> None:
        """Reset a daily job row to pending with zeroed failure state.

        Raises ValueError if no row with *job_id* exists.
        """
        process_after = datetime.now(tz=UTC).isoformat()
        cursor = self._conn.execute(
            f"UPDATE {table}"
            f" SET status = 'pending', failure_count = 0, last_error = NULL,"
            f"     process_after = ?"
            f" WHERE id = ?",
            (process_after, job_id),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(f"Unknown job id: {job_id}")

    def reset_fp_digest_job(self, job_id: str) -> None:
        """Reset an fp_digest job back to pending so the consumer will retry it."""
        self._reset_daily_job("pending_fp_digest", job_id)

    def reset_the_rundown_job(self, job_id: str) -> None:
        """Reset a the_rundown job back to pending so the consumer will retry it."""
        self._reset_daily_job("pending_the_rundown", job_id)
