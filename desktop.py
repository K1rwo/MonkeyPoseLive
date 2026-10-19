"""Native Windows entry point; owns an isolated loopback service and session.

No camera or model is opened on startup or during --self-test. Model Python
environments remain external and are configured through MONKEYPOSE_CONFIG.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VERSION = '0.1.0'


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def backend_resource_root():
    return resource_root() / 'backend' if getattr(sys, 'frozen', False) else resource_root()


def write_report(path, result):
    if path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + '.partial')
        partial.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        os.replace(partial, path)
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=True), flush=True)


class DesktopService:
    """Every instance binds its own port; never discovers or stops other apps."""
    def __init__(self, output=None, port=0):
        from http.server import ThreadingHTTPServer
        from server import LiveSession, handler_for
        from runtime_config import default_output_directory
        self.session = LiveSession(Path(output) if output else default_output_directory())
        self.closed = threading.Event()
        self.close_lock = threading.Lock()
        self.control_token = secrets.token_urlsafe(32)
        owner = self
        base = handler_for(self.session)

        class Handler(base):
            def do_POST(self):
                if self.path == '/api/desktop/shutdown':
                    token = self.headers.get('X-MonkeyPose-Desktop-Token', '')
                    if not self.allowed() or not secrets.compare_digest(token, owner.control_token):
                        self.response({'error': 'Desktop control token required'}, 403)
                        return
                    self.response({'status': 'shutting_down'})
                    threading.Thread(target=owner.close, daemon=True).start()
                    return
                return super().do_POST()

        self.http = ThreadingHTTPServer(('127.0.0.1', int(port)), Handler)
        self.http.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self.http.server_port)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .1},
                                       name='MonkeyPoseLive-HTTP', daemon=True)

    def start(self):
        self.thread.start()
        return self

    def descriptor(self):
        return dict(app='MonkeyPoseLive', version=VERSION, pid=os.getpid(), url=self.url,
                    shutdown_url=self.url + '/api/desktop/shutdown',
                    shutdown_header='X-MonkeyPose-Desktop-Token', shutdown_token=self.control_token,
                    resource_root=str(resource_root()), camera_opened=False, model_started=False)

    def close(self):
        with self.close_lock:
            if self.closed.is_set():
                return
            try:
                self.session.stop()
            finally:
                try:
                    if self.thread.is_alive():
                        self.http.shutdown()
                    self.http.server_close()
                    if self.thread is not threading.current_thread():
                        self.thread.join(timeout=5)
                finally:
                    self.closed.set()


def request_json(url, body=None, headers=None):
    request = Request(url, data=None if body is None else json.dumps(body).encode('utf-8'),
                      headers={'Content-Type': 'application/json', **(headers or {})})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def self_test(report=None):
    """Exercise actual bundled HTTP/resources and capture libraries, no devices."""
    checks = {}
    resources = [resource_root() / 'web/index.html']
    resources += [backend_resource_root() / name for name in
                  ['worker.py','models.py','runtime_config.py','adaptation_helpers.py']]
    for path in resources:
        name = path.relative_to(resource_root()).as_posix()
        if not path.is_file():
            raise RuntimeError('Missing bundled resource: ' + name)
        checks['resource:' + name] = hashlib.sha256(path.read_bytes()).hexdigest()
    import cv2
    import numpy as np
    sample = np.zeros((16, 24, 3), np.uint8)
    ok, png = cv2.imencode('.png', sample)
    assert ok and cv2.imdecode(png, cv2.IMREAD_COLOR).shape == sample.shape
    checks['opencv_numpy_no_camera'] = True
    with tempfile.TemporaryDirectory(prefix='MonkeyPoseLiveSelfTest-') as temp:
        service = DesktopService(Path(temp) / 'sessions', 0).start()
        try:
            with urlopen(service.url + '/', timeout=10) as response:
                page = response.read()
                assert response.status == 200 and b'<html' in page.lower()
            state = request_json(service.url + '/api/state')
            assert state['status'] == 'idle' and state['frames_captured'] == 0
            assert request_json(service.url + '/api/trajectory')['samples'] == []
            registry = request_json(service.url + '/api/models')
            assert isinstance(registry, dict) and len(registry) == 5
            try:
                request_json(service.url + '/api/desktop/shutdown', {})
            except HTTPError as ex:
                assert ex.code == 403
            else:
                raise AssertionError('Unauthenticated desktop shutdown accepted')
            request_json(service.url + '/api/desktop/shutdown', {},
                         {'X-MonkeyPose-Desktop-Token': service.control_token})
            assert service.closed.wait(10)
            checks.update(loopback_http=True, web_resource_served=True, models_declared=len(registry),
                          initial_idle=True, no_trajectory=True, authenticated_own_shutdown=True,
                          port=service.http.server_port)
        finally:
            service.close()
        assert not service.thread.is_alive()
        try:
            with socket.create_connection(('127.0.0.1', service.http.server_port), timeout=1):
                raise AssertionError('Owned HTTP port remains open')
        except (ConnectionRefusedError, OSError):
            pass
        checks['owned_server_closed'] = True
        checks['no_session_created'] = not (Path(temp) / 'sessions').exists()
        assert checks['no_session_created']
    result = dict(all_passed=True, app='MonkeyPoseLive', version=VERSION, frozen=bool(getattr(sys, 'frozen', False)),
                  physical_camera_opened=False, model_inference_started=False,
                  opencv=cv2.__version__, numpy=np.__version__, checks=checks)
    write_report(report, result)
    return result


def run_native(service, report=None, smoke=False):
    import webview
    webview.settings['ALLOW_FILE_URLS'] = False
    window = webview.create_window('MonkeyPoseLive', service.url, width=1260, height=900,
                                   min_size=(900, 650), background_color='#10151d')
    window.events.closed += service.close
    result = {}
    if smoke:
        def loaded():
            try:
                title = window.evaluate_js('document.title')
                assert title
                assert request_json(service.url + '/api/state')['frames_captured'] == 0
                result.update(all_passed=True, native_window_loaded=True, title=title,
                              renderer='edgechromium', physical_camera_opened=False, model_inference_started=False)
            except Exception as ex:
                result.update(all_passed=False, error=str(ex), traceback=traceback.format_exc())
            finally:
                window.destroy()
        window.events.loaded += loaded
    try:
        webview.start(gui='edgechromium', debug=False, private_mode=True)
    finally:
        service.close()
    if smoke:
        if not result:
            result.update(all_passed=False, error='Native window did not load')
        result['owned_server_closed'] = not service.thread.is_alive()
        write_report(report, result)
        if not result['all_passed']:
            raise RuntimeError(result['error'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--self-test', action='store_true', help='Headless HTTP/resource/library test; no camera/model')
    mode.add_argument('--gui-smoke-test', action='store_true', help='Load and close the native WebView2 window; no camera/model')
    mode.add_argument('--serve', action='store_true', help='Run isolated HTTP service for integration tests')
    mode.add_argument('--browser', action='store_true', help='Open a browser instead of the native window (test mode)')
    parser.add_argument('--no-window', action='store_true', help='Alias for --serve')
    parser.add_argument('--port', type=int, default=0, help='Loopback port; default0 asks Windows for an unused port')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--report', type=Path, help='Write a JSON result/control descriptor; needed for windowed EXE tests')
    args = parser.parse_args(argv)
    if args.config:
        os.environ['MONKEYPOSE_CONFIG'] = str(args.config.resolve())
    service = None
    try:
        if args.self_test:
            self_test(args.report)
            return 0
        service = DesktopService(args.output, args.port).start()
        if args.serve or args.no_window or args.browser:
            write_report(args.report, service.descriptor())
            if args.browser:
                import webbrowser
                webbrowser.open(service.url)
            while not service.closed.wait(.25):
                pass
        else:
            run_native(service, args.report, args.gui_smoke_test)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as ex:
        result = dict(all_passed=False, error_type=type(ex).__name__, error=str(ex), traceback=traceback.format_exc(),
                      physical_camera_opened=False, model_inference_started=False)
        write_report(args.report, result)
        if not args.report and os.name == 'nt' and not any([args.self_test,args.gui_smoke_test,args.serve,args.no_window,args.browser]):
            ctypes.windll.user32.MessageBoxW(None,
                'MonkeyPoseLive could not start.\n\n'+str(ex)+'\n\nThe desktop window requires Microsoft Edge WebView2 Runtime.',
                'MonkeyPoseLive', 0x10)
        return 1
    finally:
        if service is not None:
            service.close()


if __name__ == '__main__':
    raise SystemExit(main())
