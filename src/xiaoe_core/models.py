"""Domain models shared by the CLI and future API."""

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Course:
    id: str
    source_url: str
    title: str
    status: str
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AddCourseResult:
    course: Course
    created: bool

    def to_dict(self) -> Dict[str, Any]:
        return {"created": self.created, "course": self.course.to_dict()}


@dataclass(frozen=True)
class Lesson:
    id: str
    course_id: str
    position: int
    title: str
    source_url: Optional[str]
    status: str
    error: Optional[str]
    attempt_count: int
    last_error_code: Optional[str]
    last_error_at: Optional[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DownloadItemResult:
    lesson_id: str
    status: str
    file_path: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DownloadBatchResult:
    course_id: str
    processed: int
    succeeded: int
    skipped: int
    failed: int
    items: List[DownloadItemResult]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PipelineStatus:
    total_courses: int
    course_statuses: Dict[str, int]
    total_lessons: int
    lesson_statuses: Dict[str, int]
    active_course_id: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: Optional[float]
    text: str
    speaker: Optional[str] = None
    language: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    segments: List[TranscriptSegment]
    provider: str
    model: str
    raw: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "segments": [segment.to_dict() for segment in self.segments],
            "provider": self.provider,
            "model": self.model,
        }


@dataclass(frozen=True)
class TranscriptionItemResult:
    lesson_id: str
    status: str
    text_file: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptionBatchResult:
    course_id: str
    processed: int
    succeeded: int
    skipped: int
    failed: int
    items: List[TranscriptionItemResult]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StructureItemResult:
    lesson_id: str
    status: str
    markdown_file: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StructureBatchResult:
    course_id: str
    processed: int
    succeeded: int
    skipped: int
    failed: int
    items: List[StructureItemResult]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
