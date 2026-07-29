"""Audio chunking and persistent transcription orchestration."""

import json
import subprocess
from pathlib import Path
from typing import List, Optional

from .asr import AsrError, AsrProvider, merge_chunk_results
from .config import AppPaths
from .downloader import ffmpeg_executable
from .models import TranscriptionBatchResult, TranscriptionItemResult, TranscriptResult
from .lesson_paths import lessons_by_date
from .path_reports import write_stage_report
from .progress import ProgressSpinner, finish_progress
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
        show_progress: bool = True,
    ) -> None:
        self.paths = paths
        self.courses = courses
        self.lessons = lessons
        self.provider = provider
        self.show_progress = show_progress
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
        items = []
        total = len(lessons)
        try:
            for index, lesson in enumerate(lessons, 1):
                if lesson.media_hint == "no_media":
                    item = TranscriptionItemResult(lesson.id, "skipped")
                else:
                    item = self._transcribe_lesson(
                        lesson.id, force, index, total, lesson.title
                    )
                items.append(item)
                if self.show_progress:
                    label = {
                        "transcript_ready": "转写完成 ✓",
                        "skipped": "已跳过 ✓",
                        "audio_missing": "缺少音频 ✗",
                        "transcription_failed": "转写失败 ✗",
                    }.get(item.status, item.status)
                    finish_progress(
                        "  [{}/{}] {}  {}".format(
                            index, total, label, lesson.title[:40]
                        )
                    )
        finally:
            close = getattr(self.provider, "close", None)
            if callable(close):
                close()
        succeeded = sum(item.status == "transcript_ready" for item in items)
        skipped = sum(item.status == "skipped" for item in items)
        report_lessons = lessons_by_date(
            self.lessons.list_for_download(course_id)
        )

        def transcript_path(lesson_id: str) -> Optional[str]:
            transcript = self.lessons.transcript(lesson_id)
            if transcript is None or transcript["status"] != "transcript_ready":
                return None
            return str(Path(transcript["raw_file_path"]).with_name("transcript.txt"))

        report_path = write_stage_report(
            self.paths.courses_dir,
            course_id,
            "transcript",
            (
                (lesson.position, lesson.title, transcript_path(lesson.id))
                for lesson in report_lessons
            ),
        )
        if self.show_progress:
            print("转录文字清单：{}".format(report_path))
        return TranscriptionBatchResult(
            course_id=course_id,
            processed=len(items),
            succeeded=succeeded,
            skipped=skipped,
            failed=len(items) - succeeded - skipped,
            items=items,
        )

    def _transcribe_lesson(
        self,
        lesson_id: str,
        force: bool,
        progress_index: int = 0,
        progress_total: int = 0,
        title: str = "",
    ) -> TranscriptionItemResult:
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
            with ProgressSpinner(
                "  [{}/{}] 正在准备转写音频：{}".format(
                    progress_index, progress_total, title[:32]
                ),
                enabled=None if self.show_progress else False,
            ):
                chunks = self.chunker.split(
                    Path(artifact["file_path"]), lesson_dir / "asr_chunks"
                )
            results = []
            for chunk_index, chunk in enumerate(chunks, 1):
                with ProgressSpinner(
                    "  [{}/{}] 语音转文字：{}（分块 {}/{}）".format(
                        progress_index,
                        progress_total,
                        title[:32],
                        chunk_index,
                        len(chunks),
                    ),
                    enabled=None if self.show_progress else False,
                ):
                    results.append(self.provider.transcribe(chunk))
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
