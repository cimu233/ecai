import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from xiaoe_core.api import create_server
from xiaoe_core.database import Database
from xiaoe_core.services import CourseService, LessonService


class LocalApiTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "pipeline.sqlite3")
        courses = CourseService(self.database)
        lessons = LessonService(self.database)
        self.course = courses.add_course("https://school.example/course/1", "Course").course
        lessons.upsert(self.course.id, 1, "Lesson")
        self.server = create_server(self.database, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def get(self, path):
        with urlopen(self.base_url + path) as response:
            return response.status, json.loads(response.read().decode("utf-8")), response.headers

    def test_health_and_course_lessons(self):
        status, health, headers = self.get("/api/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", health["status"])
        self.assertEqual("no-store", headers["Cache-Control"])

        status, payload, _ = self.get("/api/courses/{}/lessons".format(self.course.id))
        self.assertEqual(200, status)
        self.assertEqual("Lesson", payload["lessons"][0]["title"])
        self.assertIsNone(payload["lessons"][0]["transcript_status"])

    def test_missing_route_is_404(self):
        with self.assertRaises(HTTPError) as context:
            urlopen(self.base_url + "/api/missing")
        self.assertEqual(404, context.exception.code)

    def test_non_loopback_binding_is_rejected(self):
        with self.assertRaises(ValueError):
            create_server(self.database, host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
