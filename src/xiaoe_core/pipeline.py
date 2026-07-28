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
        unavailable_statuses = {"source_unavailable", "unsupported_drm", "no_media"}
        download_items = getattr(download, "items", [])
        if (
            download.succeeded == 0
            and download_items
            and all(item.status in unavailable_statuses for item in download_items)
        ):
            raise ValueError("该课程所有课时都未发现可用音频源。")
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
