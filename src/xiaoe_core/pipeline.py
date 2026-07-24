"""End-to-end pipeline orchestration."""

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from .download_service import DownloadService
from .structure_service import StructureService
from .transcription_service import TranscriptionService


@dataclass(frozen=True)
class PipelineRunResult:
    course_id: str
    status: str
    download: Dict[str, Any]
    transcription: Dict[str, Any]
    structure: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PipelineRunner:
    def __init__(
        self,
        downloads: DownloadService,
        transcriptions: TranscriptionService,
        structures: StructureService,
    ) -> None:
        self.downloads = downloads
        self.transcriptions = transcriptions
        self.structures = structures

    def run(
        self,
        course_id: str,
        lesson_id: Optional[str] = None,
        limit: Optional[int] = None,
        force_transcription: bool = False,
        force_structure: bool = False,
    ) -> PipelineRunResult:
        download = self.downloads.download_course(course_id, lesson_id=lesson_id, limit=limit)
        if download.processed == 0:
            raise ValueError("该课程尚未扫描内容目录，请先执行「扫描课程目录」。")
        if download.succeeded == 0 and download.failed > 0:
            # All lessons failed — likely a text/image-only course with no audio.
            raise ValueError("该课程所有课时均无可下载的音频，可能为图文公告或纯视频课程（无音频流）。")
        transcription = self.transcriptions.transcribe_course(
            course_id, lesson_id=lesson_id, limit=limit, force=force_transcription
        )
        structure = self.structures.structure_course(
            course_id, lesson_id=lesson_id, limit=limit, force=force_structure
        )
        status = "completed" if not (download.failed or transcription.failed or structure.failed) else "partial"
        return PipelineRunResult(
            course_id=course_id,
            status=status,
            download=download.to_dict(),
            transcription=transcription.to_dict(),
            structure=structure.to_dict(),
        )
