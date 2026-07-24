"""Loopback-only read API for optional local frontends."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Type
from urllib.parse import urlparse

from .database import Database
from .services import CourseService


def create_server(database: Database, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("The local API may only bind to a loopback address.")
    database.initialize()

    class Handler(BaseHTTPRequestHandler):
        server_version = "XiaoeLocalApi/0.1"

        def do_GET(self) -> None:
            route = urlparse(self.path).path.rstrip("/") or "/"
            if route == "/api/health":
                self._send(200, {"status": "ok"})
                return
            if route == "/api/status":
                self._send(200, CourseService(database).status().to_dict())
                return
            if route == "/api/courses":
                courses = [course.to_dict() for course in CourseService(database).list_courses()]
                self._send(200, {"courses": courses})
                return
            match = re.fullmatch(r"/api/courses/([^/]+)/lessons", route)
            if match:
                course_id = match.group(1)
                if CourseService(database).get(course_id) is None:
                    self._send(404, {"error": "course_not_found"})
                    return
                rows = database.rows(
                    """
                    SELECT l.*, t.status AS transcript_status, t.raw_file_path,
                           n.status AS note_status, n.markdown_file_path
                    FROM lessons l
                    LEFT JOIN transcripts t ON t.lesson_id = l.id
                    LEFT JOIN structured_notes n ON n.lesson_id = l.id
                    WHERE l.course_id = ?
                    ORDER BY l.position, l.id
                    """,
                    (course_id,),
                )
                lessons = [dict(row) for row in rows]
                self._send(200, {"course_id": course_id, "lessons": lessons})
                return
            self._send(404, {"error": "not_found"})

        def _send(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
