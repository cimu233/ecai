"""Audio chunking and persistent transcription orchestration."""

import json
import subprocess
from pathlib import Path
from typing import List, Optional

from .asr import AsrError, AsrProvider, merge_chunk_results
from .config import AppPaths
from .downloader import ffmpeg_executable
from .models import TranscriptionBatchResult, TranscriptionItemResult, TranscriptResult
from .services import CourseService, LessonService


class AudioChunker:
    def __init__(self, ffmpeg: Optional[str] = None, chunk_seconds: int = 240, output_format: str = "mp3") -> None:
        self.ffmpeg = ffmpeg or ffmpeg_executable()
        self.chunk_seconds = chunk_seconds
        self.output_format = output_format

    def split(self, source: Path, output_dir: Path) -> List[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        pattern = output_dir / "chunk_%04d.{}".format(self.output_format)
        encoding = ["-c:a", "pcm_s16le"] if self.output_format == "wav" else ["-b:a", "64k"]
        command = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
        ] + encoding + [
            "-f", "segment", "-segment_time", str(self.chunk_seconds), "-reset_timestamps", "1", str(pattern),
        ]
        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if process.returncode != 0:
            raise AsrError("audio_chunk_failed", "ffmpeg could not prepare audio for transcription.")
        chunks = [
            chunk
            for chunk in sorted(output_dir.glob("chunk_*.{}".format(self.output_format)))
            if chunk.stat().st_size >= 4096
        ]
        if not chunks:
            raise AsrError("audio_chunk_failed", "ffmpeg produced no transcription chunks.")
        return chunks


class TranscriptionService:
    def __init__(
        self,
        paths: AppPaths,
        courses: CourseService,
        lessons: LessonService,
        provider: AsrProvider,
        chunker: Optional[AudioChunker] = None,
    ) -> None:
        self.paths = paths
        self.courses = courses
        self.lessons = lessons
        self.provider = provider
        self.chunker = chunker or AudioChunker(
            chunk_seconds=int(getattr(provider, "chunk_seconds", 240)),
            output_format=str(getattr(provider, "chunk_format", "mp3")),
        )

    def transcribe_course(
        self,
        course_id: str,
        lesson_id: Optional[str] = None,
        limit: Optional[int] = None,
        force: bool = False,
        positions: Optional[set] = None,
    ) -> TranscriptionBatchResult:
        if self.courses.get(course_id) is None:
            raise ValueError("Course does not exist: {}".format(course_id))
        lessons = self.lessons.list_for_download(course_id, lesson_id, limit, positions)
        if lesson_id and not lessons:
            raise ValueError("Lesson does not exist in course: {}".format(lesson_id))
        try:
            items = [self._transcribe_lesson(lesson.id, force) for lesson in lessons]
        finally:
            close = getattr(self.provider, "close", None)
            if callable(close):
                close()
        succeeded = sum(item.status == "transcript_ready" for item in items)
        skipped = sum(item.status == "skipped" for item in items)
        return TranscriptionBatchResult(
            course_id=course_id,
            processed=len(items),
            succeeded=succeeded,
            skipped=skipped,
            failed=len(items) - succeeded - skipped,
            items=items,
        )

    def _transcribe_lesson(self, lesson_id: str, force: bool) -> TranscriptionItemResult:
        existing = self.lessons.transcript(lesson_id)
        if not force and existing is not None and existing["status"] == "transcript_ready":
            raw_path = Path(existing["raw_file_path"])
            text_path = raw_path.with_name("transcript.txt")
            if raw_path.is_file() and text_path.is_file():
                return TranscriptionItemResult(lesson_id, "skipped", str(text_path))
        artifact = self.lessons.audio_artifact(lesson_id)
        if artifact is None or not Path(artifact["file_path"]).is_file():
            return TranscriptionItemResult(lesson_id, "audio_missing", error_code="audio_missing", error="Download audio first.")

        lesson_dir = Path(artifact["file_path"]).parent
        self.lessons.begin_transcription(lesson_id, self.provider.name, self.provider.model)
        try:
            chunks = self.chunker.split(Path(artifact["file_path"]), lesson_dir / "asr_chunks")
            results = [self.provider.transcribe(chunk) for chunk in chunks]
            offsets = [index * float(self.chunker.chunk_seconds) for index in range(len(chunks))]
            result = merge_chunk_results(results, offsets)
            raw_path = lesson_dir / "transcript.raw.json"
            text_path = lesson_dir / "transcript.txt"
            normalized_path = lesson_dir / "transcript.json"
            self._write_json(raw_path, result.raw)
            self._write_json(normalized_path, result.to_dict())
            text_path.write_text(result.text + "\n", encoding="utf-8")
            self.lessons.complete_transcription(
                lesson_id, result.text, [segment.to_dict() for segment in result.segments], str(raw_path)
            )
            return TranscriptionItemResult(lesson_id, "transcript_ready", str(text_path))
        except AsrError as error:
            self.lessons.fail_transcription(lesson_id, error.code, str(error))
            return TranscriptionItemResult(lesson_id, "transcription_failed", error_code=error.code, error=str(error))

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
