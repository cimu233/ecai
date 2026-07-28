import sqlite3
import tempfile
import unittest
from pathlib import Path

from xiaoe_core.database import Database, SCHEMA_VERSION


class DatabaseMigrationTest(unittest.TestCase):
    def test_v1_database_is_upgraded_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.sqlite3"
            connection = sqlite3.connect(str(path))
            connection.executescript(
                """
                CREATE TABLE courses (
                    id TEXT PRIMARY KEY, source_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE lessons (
                    id TEXT PRIMARY KEY, course_id TEXT NOT NULL, position INTEGER NOT NULL,
                    title TEXT NOT NULL, source_url TEXT, status TEXT NOT NULL, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(course_id, position)
                );
                CREATE TABLE artifacts (
                    id TEXT PRIMARY KEY, lesson_id TEXT NOT NULL, kind TEXT NOT NULL,
                    file_path TEXT NOT NULL, checksum TEXT, created_at TEXT NOT NULL,
                    UNIQUE(lesson_id, kind)
                );
                INSERT INTO courses VALUES (
                    'course_old', 'https://example.com/course/old', 'Old Course',
                    'pending_catalog', '2026-01-01', '2026-01-01'
                );
                PRAGMA user_version = 1;
                """
            )
            connection.commit()
            connection.close()

            Database(path).initialize()

            upgraded = sqlite3.connect(str(path))
            version = upgraded.execute("PRAGMA user_version").fetchone()[0]
            lesson_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(lessons)")}
            artifact_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(artifacts)")}
            title = upgraded.execute("SELECT title FROM courses WHERE id = 'course_old'").fetchone()[0]
            upgraded.close()

            self.assertEqual(SCHEMA_VERSION, version)
            self.assertIn("attempt_count", lesson_columns)
            self.assertIn("last_error_code", lesson_columns)
            self.assertIn("content_type", lesson_columns)
            self.assertIn("media_hint", lesson_columns)
            self.assertIn("duration_seconds", artifact_columns)
            self.assertIn("codec", artifact_columns)
            self.assertEqual("Old Course", title)


if __name__ == "__main__":
    unittest.main()
