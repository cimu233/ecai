import contextlib
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from test_downloader import LocalMediaServer
from xiaoe_core.browser import BrowserError
from xiaoe_cli.main import main
from xiaoe_core.config import AppPaths
from xiaoe_core.database import Database
from xiaoe_core.download_service import DownloadService
from xiaoe_core.downloader import AudioDownloader, ffmpeg_executable
from xiaoe_core.media import MediaSelector, StoredUrlResolver
from xiaoe_core.services import CourseService, LessonService


class DownloadServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.media_dir = self.root / "media"
        self.media_dir.mkdir()
        self.audio_path = self.media_dir / "lesson.wav"
        with wave.open(str(self.audio_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000)
        self.server = LocalMediaServer(self.media_dir, require_auth=False)
        self.server.start()

        self.paths = AppPaths.resolve(str(self.root / "data"))
        self.paths.create()
        database = Database(self.paths.database_file)
        self.courses = CourseService(database)
        self.lessons = LessonService(database)
        self.course = self.courses.add_course("https://school.example.com/course/1", "Course One").course
        self.lesson = self.lessons.upsert(
            course_id=self.course.id,
            position=1,
            title="Lesson One",
            source_url=self.server.base_url + "/lesson.wav",
        )
        self.service = DownloadService(
            paths=self.paths,
            courses=self.courses,
            lessons=self.lessons,
            resolver=StoredUrlResolver(),
            selector=MediaSelector(),
            downloader=AudioDownloader(ffmpeg=ffmpeg_executable(), show_progress=False),
        )

    def tearDown(self) -> None:
        self.server.stop()
        self.temp_dir.cleanup()

    def test_download_records_artifact_and_second_run_skips(self) -> None:
        first = self.service.download_course(self.course.id)
        second = self.service.download_course(self.course.id)

        self.assertEqual(1, first.succeeded)
        self.assertEqual(0, first.failed)
        self.assertEqual(1, second.skipped)
        lesson = self.lessons.get(self.lesson.id)
        self.assertIsNotNone(lesson)
        self.assertEqual("audio_ready", lesson.status)
        self.assertEqual(1, lesson.attempt_count)

        artifact = self.lessons.audio_artifact(self.lesson.id)
        self.assertIsNotNone(artifact)
        self.assertTrue(Path(artifact["file_path"]).is_file())
        metadata = Path(artifact["file_path"]).with_name("download.json").read_text(encoding="utf-8")
        self.assertIn('"source_host": "127.0.0.1:', metadata)
        self.assertNotIn("lesson.wav", metadata)
        self.assertNotIn("Cookie", metadata)

    def test_transcribed_lesson_reuses_valid_audio_without_resolving_again(self) -> None:
        first = self.service.download_course(self.course.id)
        self.assertEqual(1, first.succeeded)
        self.lessons.set_status(self.lesson.id, "transcript_ready")

        second = self.service.download_course(self.course.id)

        self.assertEqual(1, second.skipped)
        self.assertEqual(1, self.lessons.get(self.lesson.id).attempt_count)
        self.assertEqual("transcript_ready", self.lessons.get(self.lesson.id).status)

    def test_missing_database_artifact_is_recovered_from_download_metadata(self) -> None:
        first = self.service.download_course(self.course.id)
        self.assertEqual(1, first.succeeded)
        with self.courses.database.connect() as connection:
            connection.execute(
                "DELETE FROM artifacts WHERE lesson_id = ?", (self.lesson.id,)
            )
        resolver = mock.Mock()
        service = DownloadService(
            paths=self.paths,
            courses=self.courses,
            lessons=self.lessons,
            resolver=resolver,
            selector=MediaSelector(),
            downloader=AudioDownloader(ffmpeg=ffmpeg_executable(), show_progress=False),
        )

        recovered = service.download_course(self.course.id)

        self.assertEqual(1, recovered.skipped)
        resolver.resolve.assert_not_called()
        self.assertIsNotNone(self.lessons.audio_artifact(self.lesson.id))

    def test_browser_interruption_aborts_batch_without_recording_download_failure(self) -> None:
        resolver = mock.Mock()
        resolver.resolve.side_effect = BrowserError("ego_user_control", "Agent control stopped.")
        service = DownloadService(
            paths=self.paths,
            courses=self.courses,
            lessons=self.lessons,
            resolver=resolver,
            selector=MediaSelector(),
            downloader=AudioDownloader(ffmpeg=ffmpeg_executable(), show_progress=False),
        )

        with self.assertRaises(BrowserError):
            service.download_course(self.course.id)

        lesson = self.lessons.get(self.lesson.id)
        self.assertEqual("pending_source", lesson.status)
        self.assertIsNone(lesson.last_error_code)

    def test_legacy_browser_failures_are_recovered_before_retry(self) -> None:
        self.lessons.set_failure(
            self.lesson.id,
            "download_failed",
            "unexpected_error",
            "Unexpected download failure: BrowserError",
        )
        resolver = mock.Mock()
        resolver.resolve.side_effect = BrowserError("ego_user_control", "Agent control stopped.")
        service = DownloadService(
            paths=self.paths,
            courses=self.courses,
            lessons=self.lessons,
            resolver=resolver,
            selector=MediaSelector(),
            downloader=AudioDownloader(ffmpeg=ffmpeg_executable(), show_progress=False),
        )

        with self.assertRaises(BrowserError):
            service.download_course(self.course.id)

        lesson = self.lessons.get(self.lesson.id)
        self.assertEqual("pending_source", lesson.status)
        self.assertIsNone(lesson.error)

    def test_cli_download_outputs_frontend_ready_json(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            exit_code = main(
                [
                    "--data-dir",
                    str(self.paths.data_dir),
                    "download",
                    self.course.id,
                    "--json",
                ]
            )

        self.assertEqual(0, exit_code, errors.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(1, payload["succeeded"])
        self.assertEqual("audio_ready", payload["items"][0]["status"])


if __name__ == "__main__":
    unittest.main()
