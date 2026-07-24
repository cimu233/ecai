"""SQLite schema and connection management."""

import sqlite3
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 4


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS courses (
                    id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lessons (
                    id TEXT PRIMARY KEY,
                    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    source_url TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    last_error_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(course_id, position)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    lesson_id TEXT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    checksum TEXT,
                    size_bytes INTEGER,
                    duration_seconds REAL,
                    container TEXT,
                    codec TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(lesson_id, kind)
                );

                CREATE INDEX IF NOT EXISTS idx_lessons_course_status
                    ON lessons(course_id, status);

                CREATE TABLE IF NOT EXISTS transcripts (
                    lesson_id TEXT PRIMARY KEY REFERENCES lessons(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    text TEXT,
                    segments_json TEXT,
                    raw_file_path TEXT,
                    error_code TEXT,
                    error TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_transcripts_status
                    ON transcripts(status);

                CREATE TABLE IF NOT EXISTS structured_notes (
                    lesson_id TEXT PRIMARY KEY REFERENCES lessons(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    markdown_file_path TEXT,
                    data_file_path TEXT,
                    error_code TEXT,
                    error TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._add_column(connection, "lessons", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
            self._add_column(connection, "lessons", "last_error_code", "TEXT")
            self._add_column(connection, "lessons", "last_error_at", "TEXT")
            self._add_column(connection, "artifacts", "size_bytes", "INTEGER")
            self._add_column(connection, "artifacts", "duration_seconds", "REAL")
            self._add_column(connection, "artifacts", "container", "TEXT")
            self._add_column(connection, "artifacts", "codec", "TEXT")
            connection.execute("PRAGMA user_version = {}".format(SCHEMA_VERSION))

    @staticmethod
    def _add_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row[1] for row in connection.execute("PRAGMA table_info({})".format(table))}
        if column not in existing:
            connection.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, definition))

    def rows(self, query: str, parameters: tuple = ()) -> Iterator[sqlite3.Row]:
        with self.connect() as connection:
            cursor = connection.execute(query, parameters)
            yield from cursor.fetchall()
