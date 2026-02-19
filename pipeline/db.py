from __future__ import annotations

import sqlite3
from dataclasses import dataclass
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
    size_bytes: int
    duration_seconds: int | None


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
    size_bytes INTEGER NOT NULL,
    duration_seconds INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS processed_emails (
    r2_key TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
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
            "ALTER TABLE episodes "
            "ADD COLUMN feed_slug TEXT NOT NULL DEFAULT 'general'"
        )
        category_ddl = (
            "ALTER TABLE episodes "
            "ADD COLUMN category TEXT NOT NULL DEFAULT 'News'"
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
                    size_bytes,
                    duration_seconds
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                episode.size_bytes,
                episode.duration_seconds,
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
                    size_bytes,
                    duration_seconds
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
                    size_bytes,
                    duration_seconds
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
                size_bytes=row["size_bytes"],
                duration_seconds=row["duration_seconds"],
            )
            for row in rows
        ]

    def list_feed_slugs(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT feed_slug FROM episodes ORDER BY feed_slug ASC"
        ).fetchall()
        return [str(row["feed_slug"]) for row in rows if row["feed_slug"]]
