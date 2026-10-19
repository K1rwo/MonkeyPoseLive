"""Desktop lifecycle and release-boundary tests; never open a camera/model."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import build_windows
import desktop
import refresh_release


class DesktopLifecycleTests(unittest.TestCase):
    def test_two_instances_have_independent_ports_and_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            first = desktop.DesktopService(Path(temp)/'one',0).start()
            second = desktop.DesktopService(Path(temp)/'two',0).start()
            try:
                self.assertNotEqual(first.url, second.url)
                self.assertEqual(desktop.request_json(first.url+'/api/state')['status'],'idle')
                with self.assertRaises(HTTPError) as rejected:
                    desktop.request_json(first.url+'/api/desktop/shutdown',{})
                self.assertEqual(rejected.exception.code,403)
                desktop.request_json(first.url+'/api/desktop/shutdown',{},
                                     {'X-MonkeyPose-Desktop-Token':first.control_token})
                self.assertTrue(first.closed.wait(10))
                self.assertEqual(desktop.request_json(second.url+'/api/state')['frames_captured'],0)
                self.assertFalse((Path(temp)/'one').exists())
                self.assertFalse((Path(temp)/'two').exists())
            finally:
                first.close()
                second.close()
            self.assertFalse(first.thread.is_alive())
            self.assertFalse(second.thread.is_alive())

    def test_windowed_report_does_not_require_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'result.json'
            with patch.object(sys,'stdout',None):
                desktop.write_report(path,{'all_passed':True})
            self.assertTrue(json.loads(path.read_text())['all_passed'])

    def test_resource_root_honors_frozen_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(sys,'_MEIPASS',temp,create=True):
                self.assertEqual(desktop.resource_root(),Path(temp))
                with patch.object(sys,'frozen',True,create=True):
                    self.assertEqual(desktop.backend_resource_root(),Path(temp)/'backend')

    def test_real_headless_entry_with_missing_private_config(self):
        with tempfile.TemporaryDirectory() as temp:
            report=Path(temp)/'self-test.json'
            missing=Path(temp)/'not-configured.json'
            run=subprocess.run([sys.executable,str(Path(desktop.__file__).resolve()),'--self-test',
                                '--config',str(missing),'--report',str(report)],
                               capture_output=True,timeout=90)
            self.assertEqual(run.returncode,0,run.stderr.decode('utf-8','replace'))
            result=json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(result['all_passed'])
            self.assertFalse(result['physical_camera_opened'])
            self.assertFalse(result['model_inference_started'])
            self.assertFalse(missing.exists())


class ReleaseBoundaryTests(unittest.TestCase):
    def test_staging_is_allowlisted_and_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source';source.mkdir()
            for name in build_windows.SOURCE_FILES:
                (source/name).write_text('public fixture',encoding='utf-8')
            (source/'web').mkdir()
            (source/'web/index.html').write_text('<html></html>')
            (source/'docs').mkdir()
            (source/'docs/BACKENDS.md').write_text('public backend documentation')
            (source/'docs/private.png').write_bytes(b'private QA media')
            (source/'licenses').mkdir()
            (source/'licenses/upstream.txt').write_text('upstream license')
            (source/'config.json').write_text('private machine data')
            (source/'weights').mkdir()
            (source/'weights/private.pt').write_bytes(b'private weights')
            target=Path(temp)/'staged'
            names=build_windows.stage_sources(source,target)
            self.assertIn('web/index.html',names)
            self.assertIn('docs/BACKENDS.md',names)
            self.assertIn('licenses/upstream.txt',names)
            self.assertFalse((target/'docs/private.png').exists())
            self.assertFalse((target/'config.json').exists())
            self.assertFalse((target/'weights').exists())
            with self.assertRaises(FileExistsError):
                build_windows.stage_sources(source,target)

    def test_release_inspector_rejects_research_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            app=Path(temp)
            (app/'predictions.jsonl').write_text('{}\n')
            with self.assertRaisesRegex(RuntimeError,'Private/model/result'):
                build_windows.inspect_release(app)

    def test_document_refresh_rejects_modified_frozen_sources(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)
            (source/'worker.py').write_text('original public worker')
            (source/'README.md').write_text('original public documentation')
            manifest={'source_files':{name:build_windows.digest(source/name)
                                      for name in ['worker.py','README.md']}}
            (source/'README.md').write_text('final public documentation')
            refresh_release.verify_frozen_inputs(source,manifest)
            (source/'worker.py').write_text('changed code needs a new build')
            with self.assertRaisesRegex(RuntimeError,'Frozen source changed'):
                refresh_release.verify_frozen_inputs(source,manifest)

    def test_external_worker_bundle_is_separate_from_host_extensions(self):
        with tempfile.TemporaryDirectory() as temp:
            app=Path(temp)
            backend=app/'_internal/backend';backend.mkdir(parents=True)
            hashes={}
            for name in build_windows.BACKEND_FILES:
                (backend/name).write_text('public backend fixture')
                hashes[name]=build_windows.digest(backend/name)
            build_windows.verify_bundled_inputs(app,hashes)
            (app/'_internal/worker.py').write_text('incorrect host extension directory')
            with self.assertRaisesRegex(RuntimeError,'must be isolated'):
                build_windows.verify_bundled_inputs(app,hashes)


if __name__=='__main__':
    unittest.main()
