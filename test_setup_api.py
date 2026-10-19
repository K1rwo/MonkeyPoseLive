"""Real loopback setup-API tests using isolated private configuration directories.

Run: python -m unittest test_setup_api -v
Only the standard library is required. No model, worker, video or camera is opened.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen


MODEL_IDS = {"mmpose", "macaque3dpose_2d", "vitpose_animal", "xpose", "dlc"}


class SetupAPITests(unittest.TestCase):
    """Exercise the actual handler, validator, filesystem and registry together."""

    def setUp(self):
        self.original_environment = {
            name: os.environ.get(name) for name in ("MONKEYPOSE_CONFIG", "LOCALAPPDATA")
        }
        self.temporary = tempfile.TemporaryDirectory(prefix="monkeypose-setup-api-")
        self.root = Path(self.temporary.name).resolve()
        self.configuration_file = self.root / "private" / "overridden-settings.json"
        self.local_appdata = self.root / "local-appdata"
        self.http_server = None
        self.http_thread = None
        self.models = None
        self.app = None
        self.runtime = None
        self.addCleanup(self._cleanup)
        os.environ["MONKEYPOSE_CONFIG"] = str(self.configuration_file)
        os.environ["LOCALAPPDATA"] = str(self.local_appdata)
        self.modules_before = set(sys.modules)
        self.models = importlib.import_module("models")
        self.models.refresh_registry()
        self.app = importlib.import_module("server")
        self.runtime = importlib.import_module("runtime_config")
        self.session = self.app.LiveSession(self.runtime.default_output_directory())
        self.http_server = ThreadingHTTPServer(
            ("127.0.0.1", 0), self.app.handler_for(self.session)
        )
        self.http_server.daemon_threads = True
        self.http_thread = threading.Thread(
            target=lambda: self.http_server.serve_forever(poll_interval=0.01),
            name="setup-api-test-http",
            daemon=True,
        )
        self.http_thread.start()
        self.base_url = f"http://127.0.0.1:{self.http_server.server_port}"

    def _cleanup(self):
        try:
            if self.http_server is not None:
                self.http_server.shutdown()
                self.http_server.server_close()
            if self.http_thread is not None:
                self.http_thread.join(timeout=2)
        finally:
            try:
                for name, value in self.original_environment.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                if self.models is not None:
                    self.models.refresh_registry()
                if self.app is not None and self.runtime is not None:
                    self.app.DEFAULT_OUTPUT = self.runtime.default_output_directory()
            finally:
                self.temporary.cleanup()

    def call(self, route="/api/setup", body=None, *, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        request = Request(
            self.base_url + route,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)

    @staticmethod
    def configuration(output="sessions-one"):
        return {
            "schema_version": 1,
            "base_python": "",
            "output_directory": output,
            "detector": {"checkpoint": "", "device": "cpu"},
            "models": {},
        }

    def backups(self):
        return sorted(
            self.configuration_file.parent.glob(
                self.configuration_file.stem + ".backup-*" + self.configuration_file.suffix
            )
        )

    @contextmanager
    def alive_session_thread(self, attribute):
        """A real waiting thread represents an unreleased owner; it does no I/O."""
        release = threading.Event()
        thread = threading.Thread(
            target=release.wait, name="setup-api-test-session-guard", daemon=True
        )
        thread.start()
        setattr(self.session, attribute, thread)
        try:
            self.assertTrue(thread.is_alive())
            yield
        finally:
            release.set()
            thread.join(timeout=2)
            setattr(self.session, attribute, None)

    def test_fresh_setup_uses_override_and_reports_five_missing_models(self):
        status, result = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(Path(result["config_path"]).resolve(), self.configuration_file)
        self.assertEqual(set(result["models"]), MODEL_IDS)
        self.assertTrue(all(item["ready"] is False for item in result["models"].values()))
        self.assertTrue(all(item["missing"] for item in result["models"].values()))
        self.assertIsNone(result["configuration_error"])
        self.assertIs(result["detector_ready"], False)
        self.assertEqual(
            Path(result["output_directory"]).resolve(),
            self.local_appdata / "MonkeyPoseLive" / "sessions",
        )
        self.assertFalse(self.configuration_file.exists())
        self.assertFalse((self.local_appdata / "MonkeyPoseLive" / "config.json").exists())

    def test_valid_save_changes_actual_output_directory_without_starting_session(self):
        status, saved = self.call(body=self.configuration("relative-output"))
        expected = (self.configuration_file.parent / "relative-output").resolve()
        self.assertEqual(status, 200)
        self.assertEqual(Path(saved["output_directory"]), expected)
        self.assertEqual(self.session.output, expected)
        self.assertEqual(saved["configuration"]["output_directory"], "relative-output")
        self.assertEqual(json.loads(self.configuration_file.read_text(encoding="utf-8"))["schema_version"], 1)
        self.assertEqual(self.call()[1]["output_directory"], str(expected))
        self.assertIsNone(self.session.thread)
        self.assertIsNone(self.session.capture_thread)
        self.assertIsNone(self.session.proc)

    def test_second_save_preserves_previous_bytes_and_leaves_no_temporary_file(self):
        self.assertEqual(self.call(body=self.configuration())[0], 200)
        previous = self.configuration_file.read_bytes()
        self.assertEqual(self.backups(), [])
        status, saved = self.call(body=self.configuration("second-output"))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_bytes(), previous)
        self.assertEqual(saved["configuration"]["output_directory"], "second-output")
        self.assertEqual(list(self.configuration_file.parent.glob("*.tmp")), [])

    def test_invalid_schema_is_400_and_preserves_config_and_backup_set(self):
        self.assertEqual(self.call(body=self.configuration())[0], 200)
        previous = self.configuration_file.read_bytes()
        for invalid in (
            {"schema_version": 2, "models": {}},
            {"schema_version": True, "models": {}},
            {"schema_version": 1, "models": []},
        ):
            with self.subTest(payload=invalid):
                status, result = self.call(body=invalid)
                self.assertEqual(status, 400)
                self.assertTrue(result.get("error"))
                self.assertEqual(self.configuration_file.read_bytes(), previous)
                self.assertEqual(self.backups(), [])

    def test_alive_model_owner_blocks_setup_with_409_without_mutating_file(self):
        self.assertEqual(self.call(body=self.configuration())[0], 200)
        previous = self.configuration_file.read_bytes()
        with self.alive_session_thread("thread"):
            status, result = self.call(body=self.configuration("rejected-output"))
            self.assertEqual(status, 409)
            self.assertTrue(result.get("error"))
        self.assertEqual(self.configuration_file.read_bytes(), previous)
        self.assertEqual(self.backups(), [])

    def test_unreleased_capture_owner_blocks_setup_with_409_without_mutating_file(self):
        self.assertEqual(self.call(body=self.configuration())[0], 200)
        previous = self.configuration_file.read_bytes()
        with self.alive_session_thread("capture_thread"):
            self.assertIsNone(self.session.thread)
            status, result = self.call(body=self.configuration("rejected-output"))
            self.assertEqual(status, 409)
            self.assertTrue(result.get("error"))
        self.assertEqual(self.configuration_file.read_bytes(), previous)
        self.assertEqual(self.backups(), [])

    def test_cross_origin_read_and_save_are_403_and_do_not_modify_config(self):
        self.assertEqual(self.call(body=self.configuration())[0], 200)
        previous = self.configuration_file.read_bytes()
        for body in (None, self.configuration("rejected-output")):
            with self.subTest(method="GET" if body is None else "POST"):
                status, result = self.call(body=body, origin="https://unrelated.invalid")
                self.assertEqual(status, 403)
                self.assertTrue(result.get("error"))
        self.assertEqual(self.configuration_file.read_bytes(), previous)
        self.assertEqual(self.backups(), [])

    def test_damaged_json_remains_repairable_and_backup_preserves_original_bytes(self):
        damaged = b'\xff\x00{"schema_version":1, unfinished'
        self.configuration_file.parent.mkdir(parents=True)
        self.configuration_file.write_bytes(damaged)
        self.models.refresh_registry()
        status, broken = self.call()
        self.assertEqual(status, 200)
        self.assertTrue(broken["configuration_error"])
        self.assertTrue(all(item["ready"] is False for item in broken["models"].values()))
        self.assertEqual(self.configuration_file.read_bytes(), damaged)
        status, repaired = self.call(body=self.configuration("repaired-output"))
        self.assertEqual(status, 200)
        self.assertIsNone(repaired["configuration_error"])
        self.assertIsNone(self.call()[1]["configuration_error"])
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_bytes(), damaged)
        self.assertEqual(list(self.configuration_file.parent.glob("*.tmp")), [])

    def test_models_endpoint_matches_setup_readiness_without_loading_inference_libraries(self):
        status, models = self.call("/api/models")
        self.assertEqual(status, 200)
        setup = self.call()[1]
        self.assertEqual(set(models), MODEL_IDS)
        self.assertEqual(models, setup["models"])
        newly_loaded = set(sys.modules) - self.modules_before
        self.assertFalse(
            any(name.split(".")[0] in {"torch", "tensorflow", "cv2"} for name in newly_loaded),
            "Setup metadata must not import inference or capture libraries",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
