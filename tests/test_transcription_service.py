import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from xiaoe_core.config import AppPaths
from xiaoe_core.database import Database
from xiaoe_core.models import TranscriptResult, TranscriptSegment
from xiaoe_core.services import CourseService, LessonService
from xiaoe_core.transcription_service import AudioChunker, TranscriptionService


class FakeProvider:
    name = "fake"
    model = "fake-asr"

    def __init__(self):
        self.calls = []

    def transcribe(self, audio_path):
        self.calls.append(audio_path)
        text = "chunk {}".format(len(self.calls))
        return TranscriptResult(
            text=text,
            segments=[TranscriptSegment(0.0, None, text, language="en")],
            provider=self.name,
            model=self.model,
            raw={"text": text},
        )


class TranscriptionServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paths = AppPaths.resolve(str(Path(self.temp_dir.name) / "data"))
        self.paths.create()
        database = Database(self.paths.database_file)
        self.courses = CourseService(database)
        self.lessons = LessonService(database)
        self.course = self.courses.add_course("https://school.example/course/1", "Course").course
        self.lesson = self.lessons.upsert(self.course.id, 1, "Lesson")
        lesson_dir = self.paths.courses_dir / self.course.id / "lessons" / self.lesson.id
        lesson_dir.mkdir(parents=True)
        self.audio = lesson_dir / "audio.source.wav"
        with wave.open(str(self.audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000)
        self.lessons.record_audio_artifact(
            self.lesson.id, str(self.audio), "unused", self.audio.stat().st_size, 1.0, "wav", "pcm_s16le"
        )
        self.lessons.set_status(self.lesson.id, "audio_ready")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_transcription_persists_raw_normalized_and_text(self):
        provider = FakeProvider()
        service = TranscriptionService(
            self.paths, self.courses, self.lessons, provider, AudioChunker(chunk_seconds=240)
        )

        first = service.transcribe_course(self.course.id)
        second = service.transcribe_course(self.course.id)

        self.assertEqual(1, first.succeeded)
        self.assertEqual(1, second.skipped)
        self.assertEqual(1, len(provider.calls))
        text_path = Path(first.items[0].text_file)
        self.assertEqual("chunk 1\n", text_path.read_text(encoding="utf-8"))
        self.assertTrue(text_path.with_name("transcript.raw.json").is_file())
        self.assertTrue(text_path.with_name("transcript.json").is_file())
        row = self.lessons.transcript(self.lesson.id)
        self.assertEqual("transcript_ready", row["status"])
        self.assertEqual("fake", row["provider"])

    def test_chunker_ignores_tiny_trailing_segment(self):
        output_dir = Path(self.temp_dir.name) / "chunks"

        def create_chunks(*args, **kwargs):
            (output_dir / "chunk_0000.mp3").write_bytes(b"a" * 5000)
            (output_dir / "chunk_0001.mp3").write_bytes(b"b" * 719)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("xiaoe_core.transcription_service.subprocess.run", side_effect=create_chunks):
            chunks = AudioChunker(ffmpeg="ffmpeg").split(self.audio, output_dir)

        self.assertEqual([output_dir / "chunk_0000.mp3"], chunks)


if __name__ == "__main__":
    unittest.main()
