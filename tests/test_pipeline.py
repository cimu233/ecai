import unittest

from xiaoe_core.pipeline import PipelineRunner


class Result:
    def __init__(self, stage, failed=0, processed=1, succeeded=0, skipped=0):
        self.stage = stage
        self.failed = failed
        self.processed = processed
        self.succeeded = succeeded
        self.skipped = skipped

    def to_dict(self):
        return {"stage": self.stage, "failed": self.failed}


class Stage:
    def __init__(self, name, failed=0):
        self.name = name
        self.failed = failed
        self.calls = []

    def download_course(self, course_id, **kwargs):
        self.calls.append((course_id, kwargs))
        succeeded = 1 if self.failed == 0 and kwargs.get("limit", 0) != 0 else 0
        return Result(self.name, self.failed, succeeded=succeeded)

    def transcribe_course(self, course_id, **kwargs):
        self.calls.append((course_id, kwargs))
        return Result(self.name, self.failed, succeeded=1 if self.failed == 0 else 0)

    def structure_course(self, course_id, **kwargs):
        self.calls.append((course_id, kwargs))
        return Result(self.name, self.failed, succeeded=1 if self.failed == 0 else 0)


class PipelineRunnerTest(unittest.TestCase):
    def test_run_calls_all_stages_with_same_scope(self):
        download = Stage("download")
        transcription = Stage("transcription")
        structure = Stage("structure")

        result = PipelineRunner(download, transcription, structure).run(
            "course_1", lesson_id="lesson_1", limit=1
        )

        self.assertEqual("completed", result.status)
        self.assertEqual("lesson_1", download.calls[0][1]["lesson_id"])
        self.assertEqual(1, transcription.calls[0][1]["limit"])
        self.assertEqual("structure", result.structure["stage"])

    def test_any_stage_failure_marks_run_partial(self):
        result = PipelineRunner(Stage("download"), Stage("transcription", 1), Stage("structure")).run("c")
        self.assertEqual("partial", result.status)

    def test_zero_lesson_run_is_rejected(self):
        download = Stage("download")

        def empty_download(course_id, **kwargs):
            return Result("download", processed=0)

        download.download_course = empty_download
        with self.assertRaisesRegex(ValueError, "尚未扫描内容目录"):
            PipelineRunner(download, Stage("transcription"), Stage("structure")).run("c")


if __name__ == "__main__":
    unittest.main()
