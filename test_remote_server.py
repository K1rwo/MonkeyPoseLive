"""Adversarial HTTP tests for the remote service, with no model or camera access.

These tests exercise real sockets, cookies and job ownership. Private fixtures live
in an isolated temporary directory and are never added to a release artifact.
"""
from __future__ import annotations

import http.client
import importlib
import json
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
import unittest


class JPEGAllocationLimitTests(unittest.TestCase):
    """Reject dimensions before the image reaches a native image decoder."""

    @staticmethod
    def header(width, height, *, progressive=False):
        marker = b"\xff\xc2" if progressive else b"\xff\xc0"
        return (b"\xff\xd8" + marker + b"\x00\x11\x08" +
                height.to_bytes(2, "big") + width.to_bytes(2, "big") +
                b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9")

    def test_normal_and_progressive_camera_dimensions_are_accepted(self):
        from web_inference import jpeg_dimensions
        for width, height in ((640, 480), (1920, 1080), (2, 2)):
            for progressive in (False, True):
                with self.subTest(width=width, height=height, progressive=progressive):
                    self.assertEqual(
                        jpeg_dimensions(self.header(width, height, progressive=progressive)),
                        (width, height),
                    )

    def test_compressed_header_cannot_claim_oversized_or_degenerate_image(self):
        from web_inference import jpeg_dimensions
        for width, height in ((65535, 65535), (4097, 2), (2, 4097),
                              (4096, 4096), (4000, 3000), (0, 480), (640, 1)):
            with self.subTest(width=width, height=height):
                with self.assertRaises(ValueError):
                    jpeg_dimensions(self.header(width, height))

    def test_truncated_and_non_jpeg_headers_fail_closed(self):
        from web_inference import jpeg_dimensions
        for payload in (b"", b"not a jpeg", b"\xff\xd8", b"\xff\xd8\xff\xc0\x00\x11",
                        b"\xff\xd8\xff\xe0\x00\x00", b"\xff\xd8\xff\xe0\xff\xffx",
                        b"\xff\xd8\xff\xda\x00\x08xxxxxxxx"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    jpeg_dimensions(payload)


class FakeEngine:
    """Transport fixture: never imports or launches an inference backend."""

    def run(self, job):
        job.update(status="running")
        if job.kind == "video":
            with job.lock:
                job.preview = b"private-preview"
                job.result = {"sequence": 1, "points": [[1, 2, 0.9]]}
            if job.config["record"]:
                (job.directory / "predictions.jsonl").write_bytes(b'{"sequence":1}\n')
                (job.directory / "trajectory.csv").write_bytes(b"sequence,x,y\n1,1,2\n")
                (job.directory / "annotated.mp4").write_bytes(b"fixture-video")
            job.update(processed=1, total_frames=1)
            return
        while not job.stop_event.is_set():
            try:
                sequence, timestamp, payload, future = job.frames.get(timeout=0.02)
            except queue.Empty:
                continue
            if not future.done():
                future.set_result({"sequence": sequence, "timestamp": timestamp,
                                   "points": [[1, 2, 0.9]], "received_bytes": len(payload)})


class RemoteHTTPTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("remote_server")
        self.temporary = tempfile.TemporaryDirectory(prefix="monkeypose-remote-http-")
        self.root = Path(self.temporary.name).resolve()
        self.app = None
        self.http = None
        self.thread = None
        self.addCleanup(self.cleanup)
        self.app = self.module.WebApplication(
            self.root,
            engine_factory=FakeEngine,
            models_provider=lambda: [
                {"id": "mmpose", "label": "Test pose", "ready": True,
                 "detector_ready": False, "detector_supported": False,
                 "device_supported": ["cpu"]},
            ],
        )
        self.http = self.module.make_server(self.app, host="127.0.0.1", port=0)
        self.thread = threading.Thread(
            target=lambda: self.http.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()
        self.host = f"127.0.0.1:{self.http.server_port}"
        self.origin = f"http://{self.host}"
        self.password = self.app.password_file.read_text(encoding="utf-8").strip()

    def cleanup(self):
        try:
            if self.http is not None:
                self.http.shutdown()
                self.http.server_close()
            if self.thread is not None:
                self.thread.join(timeout=2)
            if self.app is not None:
                self.app.close()
        finally:
            self.temporary.cleanup()

    def request(self, method, path, body=None, *, cookie=None, csrf=None,
                origin=True, host=None, headers=None, raw=False):
        supplied = dict(headers or {})
        supplied["Host"] = host or self.host
        if origin is True:
            supplied["Origin"] = self.origin
        elif origin is not None:
            supplied["Origin"] = origin
        if cookie is not None:
            supplied["Cookie"] = cookie
        if csrf is not None:
            supplied["X-CSRF-Token"] = csrf
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
            supplied.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            payload = response.read()
            response_headers = dict(response.getheaders())
            if raw:
                return response.status, payload, response_headers
            return response.status, json.loads(payload), response_headers
        finally:
            connection.close()

    def login(self):
        status, result, headers = self.request(
            "POST", "/api/login", {"password": self.password}
        )
        self.assertEqual(status, 200, result)
        return headers["Set-Cookie"].split(";", 1)[0], result["csrf"]

    def create_job(self, cookie, csrf, *, kind="video", **changes):
        config = {"kind": kind, "model": "mmpose", "roi": [0, 0, 1, 1],
                  "threshold": 0.2, "detector": False, "record": True}
        config.update(changes)
        status, job, _ = self.request("POST", "/api/jobs", config, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 201, job)
        return job["id"]

    def wait_for_status(self, cookie, identifier, expected):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, result, _ = self.request("GET", f"/api/jobs/{identifier}", cookie=cookie)
            self.assertEqual(status, 200, result)
            if result["status"] in expected:
                return result
            time.sleep(0.02)
        self.fail(f"Job did not reach {expected}: {result}")

    def upload(self, cookie, csrf, identifier, payload=b"fixture-input", *,
               offset=0, final="1", total=None):
        return self.request(
            "PUT", f"/api/jobs/{identifier}/video?offset={offset}&final={final}",
            payload, cookie=cookie, csrf=csrf,
            headers={"X-Upload-Length": str(total if total is not None else len(payload))},
        )

    def raw_request(self, method, path, headers, body=b""):
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def test_unauthenticated_api_cannot_disclose_state_or_edit_private_settings(self):
        for method, route in (
            ("GET", "/api/session"), ("POST", "/api/jobs"),
            ("GET", "/api/jobs/unknown/result"),
            ("GET", "/api/setup"), ("POST", "/api/setup"),
            ("GET", "/api/models"), ("GET", "/api/state"),
            ("GET", "/api/desktop/shutdown"),
        ):
            with self.subTest(method=method, route=route):
                status, result, _ = self.request(method, route, {} if method == "POST" else None)
                self.assertIn(status, {401, 403, 404})
                serialized = json.dumps(result)
                self.assertNotIn(str(self.root), serialized)
                self.assertNotIn(self.password, serialized)

    def test_login_rejects_wrong_password_without_issuing_a_cookie(self):
        status, result, headers = self.request("POST", "/api/login", {"password": "incorrect"})
        self.assertIn(status, {401, 403})
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn(self.password, json.dumps(result))

    def test_login_and_static_pages_reject_untrusted_host_and_origin(self):
        for host, origin in (
            ("attacker.invalid", self.origin),
            (self.host, "https://attacker.invalid"),
            (self.host, "null"),
            (self.host, self.origin + ".attacker.invalid"),
        ):
            for method, path, body in (
                ("POST", "/api/login", {"password": self.password}),
                ("GET", "/", None),
            ):
                with self.subTest(host=host, origin=origin, path=path):
                    status, _, headers = self.request(method, path, body, host=host, origin=origin)
                    self.assertEqual(status, 403)
                    self.assertNotIn("Set-Cookie", headers)

    def test_mutations_require_origin_and_session_bound_csrf(self):
        cookie_a, csrf_a = self.login()
        cookie_b, csrf_b = self.login()
        self.assertNotEqual(cookie_a, cookie_b)
        self.assertNotEqual(csrf_a, csrf_b)
        for cookie, csrf, origin in (
            (cookie_a, None, True),
            (cookie_a, csrf_b, True),
            (cookie_a, csrf_a, "https://attacker.invalid"),
            (cookie_a, csrf_a, None),
        ):
            with self.subTest(csrf=csrf, origin=origin):
                status, _, _ = self.request(
                    "POST", "/api/logout", {}, cookie=cookie, csrf=csrf, origin=origin
                )
                self.assertEqual(status, 403)
        self.assertEqual(self.request("GET", "/api/session", cookie=cookie_a)[0], 200)
        self.assertEqual(self.request("GET", "/api/session", cookie=cookie_b)[0], 200)

    def test_logout_revokes_cookie_and_does_not_revoke_another_browser(self):
        cookie_a, csrf_a = self.login()
        cookie_b, _ = self.login()
        status, _, headers = self.request(
            "POST", "/api/logout", {}, cookie=cookie_a, csrf=csrf_a
        )
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))
        self.assertEqual(self.request("GET", "/api/session", cookie=cookie_a)[0], 401)
        self.assertEqual(self.request("GET", "/api/session", cookie=cookie_b)[0], 200)

    def test_authenticated_session_does_not_expose_private_configuration(self):
        cookie, _ = self.login()
        status, result, headers = self.request("GET", "/api/session", cookie=cookie)
        self.assertEqual(status, 200)
        serialized = json.dumps(result)
        for private in (str(self.root), self.password, "checkpoint", "base_python", "config_path"):
            self.assertNotIn(private, serialized)
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        for route in ("/api/setup", "/api/config", "/api/state", "/api/models",
                      "/worker.py", "/models.py", "/access.txt", "/config.json",
                      "/../access.txt", "/%2e%2e/access.txt", "/web/../access.txt"):
            with self.subTest(route=route):
                status, _, _ = self.request("GET", route, cookie=cookie)
                self.assertIn(status, {403, 404})

    def test_login_cookie_has_http_only_same_site_and_not_path_scoped_to_login(self):
        status, _, headers = self.request("POST", "/api/login", {"password": self.password})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)

    def test_non_object_or_non_finite_json_cannot_enter_a_mutating_route(self):
        cookie, csrf = self.login()
        for body in (b"null", b"[]", b"true", b"1", b"{", b'{"threshold":NaN}',
                     b'{"threshold":Infinity}'):
            with self.subTest(body=body):
                status, _, _ = self.request(
                    "POST", "/api/jobs", body, cookie=cookie, csrf=csrf,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(status, 400)

    def test_json_body_limit_is_enforced_before_deserialization(self):
        status, _, headers = self.request(
            "POST", "/api/login", b"x" * (16 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
        self.assertIn(status, {400, 413})
        self.assertNotIn("Set-Cookie", headers)

    def test_other_browser_cannot_read_mutate_download_or_cancel_owned_job(self):
        cookie_a, csrf_a = self.login()
        cookie_b, csrf_b = self.login()
        identifier = self.create_job(cookie_a, csrf_a)
        self.assertEqual(self.upload(cookie_a, csrf_a, identifier)[0], 200)
        self.wait_for_status(cookie_a, identifier, {"completed"})
        for method, tail, body, headers in (
            ("GET", "", None, {}),
            ("GET", "/result", None, {}),
            ("GET", "/preview.jpg", None, {}),
            ("GET", "/download/json", None, {}),
            ("GET", "/download/csv", None, {}),
            ("GET", "/download/video", None, {}),
            ("DELETE", "", b"", {}),
            ("POST", "/frame?sequence=1&timestamp=0.1", b"x", {"Content-Type": "image/jpeg"}),
            ("PUT", "/video?offset=0&final=1", b"x", {"X-Upload-Length": "1"}),
        ):
            with self.subTest(method=method, tail=tail):
                status, _, _ = self.request(
                    method, f"/api/jobs/{identifier}{tail}", body,
                    cookie=cookie_b, csrf=csrf_b, headers=headers,
                )
                self.assertEqual(status, 404)
        self.assertEqual(self.request("GET", f"/api/jobs/{identifier}/result", cookie=cookie_a)[0], 200)
        status, payload, headers = self.request(
            "GET", f"/api/jobs/{identifier}/download/json", cookie=cookie_a, raw=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, b'{"sequence":1}\n')
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="predictions.jsonl"')
        self.assertEqual(self.request("GET", f"/api/jobs/{identifier}", cookie=cookie_a)[1]["status"], "completed")

    def test_invalid_chunk_retries_do_not_append_or_duplicate_bytes(self):
        cookie, csrf = self.login()
        identifier = self.create_job(cookie, csrf)
        self.assertEqual(self.upload(cookie, csrf, identifier, b"abc", final="0", total=6)[0], 200)
        for offset, final, total, payload, expected in (
            (0, "1", 6, b"def", 409),
            (3, "1", 7, b"defg", 409),
            (3, "1", 6, b"de", 400),
            (3, "0", 6, b"def", 400),
            (3, "1", 6, b"defg", 400),
        ):
            with self.subTest(offset=offset, final=final, total=total, payload=payload):
                status, _, _ = self.upload(cookie, csrf, identifier, payload, offset=offset, final=final, total=total)
                self.assertEqual(status, expected)
                state = self.request("GET", f"/api/jobs/{identifier}", cookie=cookie)[1]
                self.assertEqual(state["received"], 3)
                self.assertEqual((self.app.jobs[identifier].directory / "upload.bin").read_bytes(), b"abc")
        self.assertEqual(self.upload(cookie, csrf, identifier, b"def", offset=3, total=6)[0], 200)
        self.wait_for_status(cookie, identifier, {"completed"})
        self.assertEqual((self.app.jobs[identifier].directory / "upload.bin").read_bytes(), b"abcdef")
        self.assertEqual(self.upload(cookie, csrf, identifier, b"def", offset=3, total=6)[0], 409)

    def test_camera_frame_validation_and_session_owned_cancellation(self):
        cookie_a, csrf_a = self.login()
        cookie_b, csrf_b = self.login()
        identifier = self.create_job(cookie_a, csrf_a, kind="camera")
        self.wait_for_status(cookie_a, identifier, {"running"})
        payload = JPEGAllocationLimitTests.header(640, 480)
        route = f"/api/jobs/{identifier}/frame?sequence=1&timestamp=0.1"
        status, result, _ = self.request("POST", route, payload, cookie=cookie_a, csrf=csrf_a,
                                         headers={"Content-Type": "image/jpeg"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["sequence"], 1)
        for tail, body, content_type, expected in (
            ("sequence=1&timestamp=0.2", payload, "image/jpeg", 400),
            ("sequence=2&timestamp=NaN", payload, "image/jpeg", 400),
            ("sequence=2&timestamp=0.2", b"invalid", "image/jpeg", 400),
            ("sequence=2&timestamp=0.2", payload, "application/octet-stream", 415),
            ("sequence=2&timestamp=0.2", JPEGAllocationLimitTests.header(65535, 65535), "image/jpeg", 400),
        ):
            with self.subTest(tail=tail, content_type=content_type):
                status, _, _ = self.request(
                    "POST", f"/api/jobs/{identifier}/frame?{tail}", body,
                    cookie=cookie_a, csrf=csrf_a, headers={"Content-Type": content_type},
                )
                self.assertEqual(status, expected)
        self.assertEqual(self.request("DELETE", f"/api/jobs/{identifier}", b"", cookie=cookie_b, csrf=csrf_b)[0], 404)
        self.assertEqual(self.request("DELETE", f"/api/jobs/{identifier}", b"", cookie=cookie_a, csrf=csrf_a)[0], 200)
        self.wait_for_status(cookie_a, identifier, {"cancelled"})
        self.assertEqual(self.request("POST", route, payload, cookie=cookie_a, csrf=csrf_a,
                                      headers={"Content-Type": "image/jpeg"})[0], 409)

    def test_record_opt_out_does_not_create_downloadable_coordinates_or_video(self):
        cookie, csrf = self.login()
        identifier = self.create_job(cookie, csrf, record=False)
        self.assertEqual(self.upload(cookie, csrf, identifier)[0], 200)
        state = self.wait_for_status(cookie, identifier, {"completed"})
        self.assertTrue(all(value is False for value in state["downloads"].values()))
        for file in ("predictions.jsonl", "trajectory.csv", "annotated.mp4"):
            self.assertFalse((self.app.jobs[identifier].directory / file).exists())
        for key in ("json", "csv", "video", "../job.json", "job.json", "worker.stderr.log"):
            with self.subTest(key=key):
                self.assertEqual(self.request("GET", f"/api/jobs/{identifier}/download/{key}", cookie=cookie)[0], 404)

    def test_ambiguous_or_missing_request_lengths_are_rejected(self):
        for framing in (
            [], [("Content-Length", "2"), ("Content-Length", "2")],
            [("Content-Length", "2"), ("Content-Length", "3")],
            [("Content-Length", "-1")], [("Content-Length", "2, 2")],
            [("Transfer-Encoding", "chunked"), ("Content-Length", "2")],
        ):
            with self.subTest(framing=framing):
                status, _, headers = self.raw_request("POST", "/api/login", [
                    ("Host", self.host), ("Origin", self.origin),
                    ("Content-Type", "application/json"), *framing,
                ], b"{}")
                self.assertEqual(status, 411)
                self.assertNotIn("Set-Cookie", headers)

    def test_logout_cancels_only_the_logging_out_browser_jobs(self):
        cookie_a, csrf_a = self.login()
        cookie_b, csrf_b = self.login()
        a = self.create_job(cookie_a, csrf_a, kind="camera")
        b = self.create_job(cookie_b, csrf_b, kind="camera")
        self.wait_for_status(cookie_a, a, {"running"})
        self.assertEqual(self.request("POST", "/api/logout", b"", cookie=cookie_a, csrf=csrf_a)[0], 200)
        self.assertTrue(self.app.jobs[a].stop_event.is_set())
        self.assertFalse(self.app.jobs[b].stop_event.is_set())
        self.wait_for_status(cookie_b, b, {"running"})

    def test_expired_cookie_cannot_read_or_keep_a_camera_job_active(self):
        cookie, csrf = self.login()
        identifier = self.create_job(cookie, csrf, kind="camera")
        self.wait_for_status(cookie, identifier, {"running"})
        token = cookie.split("=", 1)[1]
        self.app.sessions[token]["expires"] = time.monotonic() - 1
        self.assertEqual(self.request("GET", "/api/session", cookie=cookie)[0], 401)
        self.assertTrue(self.app.jobs[identifier].stop_event.is_set())

    def test_login_rate_limit_bounds_password_verification_work(self):
        for index in range(12):
            with self.subTest(attempt=index):
                self.assertEqual(self.request("POST", "/api/login", {"password": "wrong"})[0], 401)
        status, _, headers = self.request("POST", "/api/login", {"password": self.password})
        self.assertEqual(status, 429)
        self.assertNotIn("Set-Cookie", headers)

    def test_cancelled_inflight_upload_cannot_append_or_requeue(self):
        cookie, csrf = self.login()
        for action in ("cancel", "logout"):
            with self.subTest(action=action):
                identifier = self.create_job(cookie, csrf)
                job = self.app.jobs[identifier]
                connection = socket.create_connection(("127.0.0.1", self.http.server_port), timeout=5)
                try:
                    request = (
                        f"PUT /api/jobs/{identifier}/video?offset=0&final=1 HTTP/1.1\r\n"
                        f"Host: {self.host}\r\nOrigin: {self.origin}\r\n"
                        f"Cookie: {cookie}\r\nX-CSRF-Token: {csrf}\r\n"
                        "X-Upload-Length: 6\r\nContent-Length: 6\r\n\r\n"
                    ).encode("ascii")
                    connection.sendall(request + b"abc")
                    deadline = time.monotonic() + 2
                    while not job.upload_lock.locked() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(job.upload_lock.locked(), "Upload handler did not start reading the body")
                    if action == "cancel":
                        status = self.request("DELETE", f"/api/jobs/{identifier}", b"", cookie=cookie, csrf=csrf)[0]
                    else:
                        status = self.request("POST", "/api/logout", b"", cookie=cookie, csrf=csrf)[0]
                    self.assertEqual(status, 200)
                    connection.sendall(b"def")
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    response.read()
                    self.assertEqual(response.status, 409)
                    self.assertEqual(job.received, 0)
                    self.assertFalse((job.directory / "upload.bin").exists())
                    self.assertEqual(job.snapshot()["status"], "cancelled")
                finally:
                    connection.close()

    def test_duplicate_browser_security_headers_are_rejected(self):
        payload = json.dumps({"password": self.password}).encode("utf-8")
        status, _, headers = self.raw_request("POST", "/api/login", [
            ("Host", self.host), ("Origin", self.origin), ("Origin", "https://attacker.invalid"),
            ("Content-Type", "application/json"), ("Content-Length", str(len(payload))),
        ], payload)
        self.assertEqual(status, 403)
        self.assertNotIn("Set-Cookie", headers)
        cookie, csrf = self.login()
        for repeated in (
            [("Cookie", cookie), ("Cookie", "monkeypose_session=untrusted"), ("X-CSRF-Token", csrf)],
            [("Cookie", cookie), ("X-CSRF-Token", csrf), ("X-CSRF-Token", "untrusted")],
        ):
            status, _, _ = self.raw_request("POST", "/api/logout", [
                ("Host", self.host), ("Origin", self.origin), ("Content-Length", "0"), *repeated,
            ])
            self.assertIn(status, {401, 403})
        self.assertEqual(self.request("GET", "/api/session", cookie=cookie)[0], 200)

    def test_head_response_does_not_corrupt_next_keep_alive_response(self):
        cookie, _ = self.login()
        connection = http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=5)
        try:
            headers = {"Host": self.host, "Origin": self.origin, "Cookie": cookie}
            connection.request("HEAD", "/api/session", headers=headers)
            response = connection.getresponse()
            self.assertEqual(response.read(), b"")
            connection.request("GET", "/api/session", headers=headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["authenticated"])
        finally:
            connection.close()

    def test_job_list_restores_only_the_authenticated_browser_jobs_in_creation_order(self):
        cookie_a, csrf_a = self.login()
        cookie_b, csrf_b = self.login()
        first = self.create_job(cookie_a, csrf_a)
        other = self.create_job(cookie_b, csrf_b)
        second = self.create_job(cookie_a, csrf_a)
        status, result, headers = self.request("GET", "/api/jobs", cookie=cookie_a)
        self.assertEqual(status, 200)
        self.assertEqual([job["id"] for job in result["jobs"]], [first, second])
        self.assertNotIn(other, json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertIn("no-store", headers["Cache-Control"])
        status, result, _ = self.request("GET", "/api/jobs", cookie=cookie_b)
        self.assertEqual(status, 200)
        self.assertEqual([job["id"] for job in result["jobs"]], [other])

    def test_https_tunnel_uses_exact_configured_origin_and_secure_cookie(self):
        public = "https://pose.example.test"
        self.app.public_url = public
        status, result, headers = self.request(
            "POST", "/api/login", {"password": self.password},
            host="pose.example.test", origin=public,
        )
        self.assertEqual(status, 200, result)
        self.assertIn("; Secure", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("GET", "/api/session", cookie=cookie,
                                      host="pose.example.test", origin=public)[0], 200)
        for host, origin, forwarded in (
            ("pose.example.test", "http://pose.example.test", {}),
            ("pose.example.test", "https://pose.example.test.attacker.invalid", {}),
            ("attacker.invalid", public, {"X-Forwarded-Host": "pose.example.test"}),
            ("pose.example.test", "https://attacker.invalid", {"X-Forwarded-Proto": "https"}),
        ):
            with self.subTest(host=host, origin=origin):
                self.assertEqual(self.request("GET", "/api/session", cookie=cookie,
                                              host=host, origin=origin, headers=forwarded)[0], 403)

    def test_local_admin_shutdown_cannot_be_called_through_public_tunnel_host(self):
        self.app.public_url = "https://pose.example.test"
        status, _, _ = self.request(
            "POST", "/_local/shutdown", {}, host="pose.example.test",
            origin=self.app.public_url, headers={"X-Admin-Token": self.app.shutdown_token},
        )
        self.assertEqual(status, 403)
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.request("POST", "/_local/shutdown", {},
                                      headers={"X-Admin-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/_local/shutdown", {},
                                      headers={"X-Admin-Token": self.app.shutdown_token})[0], 200)
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def test_deployment_rejects_non_https_origins_and_repository_data_directories(self):
        for invalid in ("http://pose.example.test", "https://user:secret@pose.example.test",
                        "https://pose.example.test/subpath", "https://pose.example.test/?token=secret",
                        "https://pose.example.test/#fragment"):
            with self.subTest(origin=invalid):
                with self.assertRaises(ValueError):
                    self.module.WebApplication(self.root / "invalid-origin", invalid,
                                               engine_factory=FakeEngine)
        forbidden = Path(self.module.HERE) / "private-data-must-not-be-created"
        self.assertFalse(forbidden.exists())
        with self.assertRaises(ValueError):
            self.module.WebApplication(forbidden, engine_factory=FakeEngine)
        self.assertFalse(forbidden.exists())

    def test_camera_and_upload_limits_reject_before_reading_oversized_body(self):
        cookie, csrf = self.login()
        video = self.create_job(cookie, csrf)
        status, _, _ = self.request(
            "PUT", f"/api/jobs/{video}/video?offset=0&final=1", b"", cookie=cookie, csrf=csrf,
            headers={"X-Upload-Length": str(self.module.CHUNK + 1),
                     "Content-Length": str(self.module.CHUNK + 1)},
        )
        self.assertEqual(status, 413)
        self.assertEqual(self.app.jobs[video].received, 0)
        self.assertEqual(self.upload(cookie, csrf, video, b"x", total=self.module.MAX_UPLOAD + 1)[0], 413)
        camera = self.create_job(cookie, csrf, kind="camera")
        self.wait_for_status(cookie, camera, {"running"})
        status, _, _ = self.request(
            "POST", f"/api/jobs/{camera}/frame?sequence=1&timestamp=0.1", b"", cookie=cookie, csrf=csrf,
            headers={"Content-Type": "image/jpeg", "Content-Length": str(2 * 1024 * 1024 + 1)},
        )
        self.assertEqual(status, 413)
        self.assertEqual(self.app.jobs[camera].last_sequence, -1)

    def test_remote_job_parameters_cannot_supply_paths_or_open_host_cameras(self):
        cookie, csrf = self.login()
        for changes in ({"source": str(self.root / "private.mp4")},
                        {"image_path": str(self.root / "private.jpg")},
                        {"python": "attacker-interpreter.exe"},
                        {"kind": "usb"}, {"kind": "camera", "camera_index": 0},
                        {"model": "not-installed"}, {"threshold": True},
                        {"roi": [0, 0, 0, 1]}, {"roi": [False, 0, 1, 1]},
                        {"detector": "true"}, {"record": 1}):
            with self.subTest(changes=changes):
                payload = {"kind": "video", "model": "mmpose", **changes}
                status, _, _ = self.request("POST", "/api/jobs", payload, cookie=cookie, csrf=csrf)
                self.assertEqual(status, 400)
        self.assertEqual(self.app.jobs, {})

    def test_per_browser_pending_job_limit_does_not_consume_other_browser_capacity(self):
        cookie_a, csrf_a = self.login()
        cookie_b, csrf_b = self.login()
        for _ in range(3):
            self.create_job(cookie_a, csrf_a)
        status, _, _ = self.request("POST", "/api/jobs", {"kind": "video", "model": "mmpose"},
                                     cookie=cookie_a, csrf=csrf_a)
        self.assertEqual(status, 429)
        other = self.create_job(cookie_b, csrf_b)
        self.assertEqual(self.request("GET", f"/api/jobs/{other}", cookie=cookie_b)[0], 200)

    def test_bodyless_delete_requires_owned_session_and_csrf_but_not_content_length(self):
        cookie_a, csrf_a = self.login()
        cookie_b, csrf_b = self.login()
        identifier = self.create_job(cookie_a, csrf_a)
        path = f"/api/jobs/{identifier}"
        base = [("Host", self.host), ("Origin", self.origin)]
        for credentials, expected in (
            ([], 401),
            ([("Cookie", cookie_a)], 403),
            ([("Cookie", cookie_b), ("X-CSRF-Token", csrf_b)], 404),
        ):
            with self.subTest(credentials=credentials):
                status, _, _ = self.raw_request("DELETE", path, base + credentials)
                self.assertEqual(status, expected)
                self.assertFalse(self.app.jobs[identifier].stop_event.is_set())
        authenticated = base + [("Cookie", cookie_a), ("X-CSRF-Token", csrf_a)]
        for framing in (
            [("Content-Length", "0"), ("Content-Length", "0")],
            [("Content-Length", "0"), ("Content-Length", "1")],
            [("Transfer-Encoding", "chunked")],
            [("Transfer-Encoding", "chunked"), ("Content-Length", "0")],
            [("Transfer-Encoding", ""), ("Content-Length", "0")],
        ):
            with self.subTest(framing=framing):
                status, _, _ = self.raw_request("DELETE", path, authenticated + framing)
                self.assertEqual(status, 411)
                self.assertFalse(self.app.jobs[identifier].stop_event.is_set())
        status, result, _ = self.raw_request("DELETE", path, authenticated)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(self.app.jobs[identifier].stop_event.is_set())

    def test_cancelled_job_cannot_download_a_file_until_inference_closes_it(self):
        release = threading.Event()
        cancelled_seen = threading.Event()
        self.addCleanup(release.set)

        class HoldingEngine:
            def run(self, job):
                with (job.directory / "predictions.jsonl").open("wb") as stream:
                    stream.write(b'{"sequence":1}\n')
                    stream.flush()
                    job.update(status="running")
                    job.stop_event.wait(timeout=3)
                    cancelled_seen.set()
                    release.wait(timeout=3)

        self.app.engine_factory = HoldingEngine
        cookie, csrf = self.login()
        identifier = self.create_job(cookie, csrf, kind="camera")
        self.wait_for_status(cookie, identifier, {"running"})
        queued = self.create_job(cookie, csrf, kind="camera")
        self.assertEqual(self.request("GET", f"/api/jobs/{queued}", cookie=cookie)[1]["status"], "queued")
        status, queued_state, _ = self.request("DELETE", f"/api/jobs/{queued}", b"", cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(queued_state["status"], "cancelled")
        self.assertFalse(self.app.jobs[queued].engine_active)
        status, state, _ = self.request("DELETE", f"/api/jobs/{identifier}", b"", cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(state["status"], "cancelling")
        self.assertTrue(cancelled_seen.wait(timeout=2))
        self.assertFalse(state["downloads"]["json"])
        self.assertTrue(self.app.jobs[identifier].engine_active)
        self.assertEqual(self.request("GET", f"/api/jobs/{identifier}", cookie=cookie)[1]["status"], "cancelling")
        self.assertFalse(self.app.jobs[identifier].outputs_closed)
        self.assertEqual(self.request("GET", f"/api/jobs/{identifier}/download/json", cookie=cookie)[0], 404)
        release.set()
        deadline = time.monotonic() + 2
        while not self.app.jobs[identifier].outputs_closed and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.app.jobs[identifier].outputs_closed)
        self.assertFalse(self.app.jobs[identifier].engine_active)
        status, state, _ = self.request("GET", f"/api/jobs/{identifier}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(state["status"], "cancelled")
        self.assertTrue(state["downloads"]["json"])
        status, payload, _ = self.request("GET", f"/api/jobs/{identifier}/download/json", cookie=cookie, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload, b'{"sequence":1}\n')

    def test_bodyless_logout_revokes_only_its_session_with_strict_explicit_framing(self):
        cookie_a, csrf_a = self.login()
        cookie_b, _ = self.login()
        headers = [("Host", self.host), ("Origin", self.origin),
                   ("Cookie", cookie_a), ("X-CSRF-Token", csrf_a)]
        for framing in (
            [("Content-Length", "0"), ("Content-Length", "0")],
            [("Transfer-Encoding", "chunked")],
            [("Transfer-Encoding", ""), ("Content-Length", "0")],
        ):
            with self.subTest(framing=framing):
                self.assertEqual(self.raw_request("POST", "/api/logout", headers + framing)[0], 411)
                self.assertEqual(self.request("GET", "/api/session", cookie=cookie_a)[0], 200)
        status, result, response_headers = self.raw_request("POST", "/api/logout", headers)
        self.assertEqual(status, 200)
        self.assertFalse(result["authenticated"])
        self.assertIn("Max-Age=0", response_headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/api/session", cookie=cookie_a)[0], 401)
        self.assertEqual(self.request("GET", "/api/session", cookie=cookie_b)[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
