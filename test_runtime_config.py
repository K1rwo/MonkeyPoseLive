"""Portable/private configuration regressions; no model, tensor or camera imports."""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

import runtime_config as config


class RuntimeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='monkeypose-config-test-')
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'private/config.json'
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA': str(self.folder / 'local'),
                                                    'MONKEYPOSE_CONFIG': str(self.path)})
        self.environment.start()
        self.models = importlib.import_module('models')
        self.saved_registry = deepcopy(self.models.MODELS)
        self.saved_runtime = deepcopy(self.models.RUNTIME_CONFIG)
        self.saved_detector = deepcopy(self.models.DETECTOR_CONFIG)
        self.saved_yolo = self.models.YOLO_WEIGHTS

    def tearDown(self):
        self.models.MODELS.clear()
        self.models.MODELS.update(self.saved_registry)
        self.models.RUNTIME_CONFIG.clear()
        self.models.RUNTIME_CONFIG.update(self.saved_runtime)
        self.models.DETECTOR_CONFIG.clear()
        self.models.DETECTOR_CONFIG.update(self.saved_detector)
        self.models.YOLO_WEIGHTS = self.saved_yolo
        self.environment.stop()
        self.temp.cleanup()

    def write_raw(self, value, path=None):
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        return path

    def test_missing_config_lists_all_models_without_heavy_imports(self):
        loaded = config.load_config()
        self.assertFalse(loaded['_config_exists'])
        self.assertFalse(loaded['_errors'])
        self.models.refresh_registry()
        self.assertEqual(set(self.models.MODELS), set(config.MODEL_IDS))
        for record in self.models.MODELS.values():
            self.assertFalse(record['ready'])
            self.assertTrue(record['missing'])
            self.assertIsNone(record['configuration_error'])

    def test_empty_json_config_is_editable_and_does_not_create_files(self):
        self.write_raw('{}')
        loaded = config.load_config()
        self.assertFalse(loaded['_errors'])
        self.assertEqual(loaded['schema_version'], 1)
        self.assertEqual(loaded['base_python'], '')
        self.assertEqual(set(loaded['models']), set(config.MODEL_IDS))
        self.assertFalse(config.default_output_directory().exists())

    def test_bad_json_and_schema_expose_errors_without_registry_crash(self):
        bad_values = ['{broken', '[]', '{"schema_version":2}', '{"models":[]}',
                      '{"models":{"mmpose":{"python":42}}}', '{"output_directory":null}',
                      '{"models":{"xpose":{"checkpoint_sha256":123}}}']
        for raw in bad_values:
            with self.subTest(raw=raw):
                self.write_raw(raw)
                loaded = config.load_config(resolve_paths=True)
                self.assertTrue(loaded['_errors'])
                self.assertTrue(loaded['config_error'])
                self.models.refresh_registry()
                self.assertTrue(all(not r['ready'] and r['configuration_error'] for r in self.models.MODELS.values()))
                self.assertEqual(config.default_output_directory(), config.local_app_directory() / 'sessions')

    def test_raw_paths_remain_editable_and_resolved_paths_use_json_directory(self):
        value = {'base_python': 'runtime/python.exe', 'output_directory': 'results',
                 'detector': {'checkpoint': 'weights/detector.pt'},
                 'models': {'mmpose': {'python': 'env/python.exe', 'config': 'pose/model.py', 'checkpoint': 'weights/model.pth'}}}
        config.save_config(value)
        raw, resolved = config.load_config(), config.load_config(resolve_paths=True)
        self.assertEqual(raw['models']['mmpose']['checkpoint'], 'weights/model.pth')
        self.assertEqual(raw['output_directory'], 'results')
        for key, relative in [('base_python', 'runtime/python.exe'), ('output_directory', 'results')]:
            self.assertEqual(Path(resolved[key]), (self.path.parent / relative).resolve())
        self.assertEqual(Path(resolved['models']['mmpose']['checkpoint']), (self.path.parent / 'weights/model.pth').resolve())
        self.assertEqual(Path(resolved['detector']['checkpoint']), (self.path.parent / 'weights/detector.pt').resolve())

    def test_environment_variable_paths_are_resolved_only_on_request(self):
        with patch.dict(os.environ, {'MONKEYPOSE_TEST_ASSET': str(self.folder / 'assets')}):
            config.save_config({'output_directory': '%MONKEYPOSE_TEST_ASSET%/sessions'})
            self.assertEqual(config.load_config()['output_directory'], '%MONKEYPOSE_TEST_ASSET%/sessions')
            self.assertEqual(Path(config.load_config(resolve_paths=True)['output_directory']), (self.folder / 'assets/sessions').resolve())

    def test_default_and_configured_output_directory(self):
        self.assertEqual(config.default_output_directory(), self.folder / 'local/MonkeyPoseLive/sessions')
        self.assertEqual(config.default_config_path(), self.folder / 'local/MonkeyPoseLive/config.json')
        self.assertEqual(config.config_path(), self.path.resolve())
        config.save_config({'output_directory': 'session-output'})
        expected = (self.path.parent / 'session-output').resolve()
        self.assertEqual(config.default_output_directory(), expected)
        self.assertEqual(Path(config.load_config(resolve_paths=True)['output_directory']), expected)

    def test_explicit_config_does_not_inherit_another_configs_output(self):
        config.save_config({'output_directory': 'active-only-output'})
        alternate = self.write_raw('{}', self.folder / 'alternate/config.json')
        resolved = config.load_config(alternate, resolve_paths=True)
        self.assertEqual(Path(resolved['output_directory']), config.local_app_directory() / 'sessions')

    def test_atomic_save_preserves_old_bytes_in_adjacent_backup(self):
        config.save_config({'output_directory': 'first'})
        old_bytes = self.path.read_bytes()
        result = config.save_config({'output_directory': 'second'})
        backup = Path(result['_backup_path'])
        self.assertEqual(backup.parent, self.path.parent)
        self.assertEqual(backup.read_bytes(), old_bytes)
        self.assertEqual(config.load_config()['output_directory'], 'second')
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_failed_atomic_replace_keeps_existing_config_and_cleans_temp(self):
        config.save_config({'output_directory': 'keep'})
        original = self.path.read_bytes()
        with patch.object(config.os, 'replace', side_effect=OSError('simulated replacement failure')):
            with self.assertRaises(OSError):
                config.save_config({'output_directory': 'must-not-replace'})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_invalid_save_does_not_replace_existing_config(self):
        config.save_config({})
        original = self.path.read_bytes()
        for value in [{'output_directory': []}, {'models': {'unknown-model': {}}}, {'extra': float('nan')}]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    config.save_config(value)
                self.assertEqual(self.path.read_bytes(), original)

    def test_private_configuration_cannot_be_saved_into_source_directory(self):
        forbidden = Path(config.__file__).resolve().parent / ('forbidden-private-' + uuid.uuid4().hex + '.json')
        with self.assertRaises(ValueError):
            config.save_config({}, forbidden)
        self.assertFalse(forbidden.exists())

    def test_registry_refresh_mutates_original_object_and_reports_missing_files(self):
        original = self.models.MODELS
        original_id = id(original)
        self.models.refresh_registry()
        self.assertFalse(original['mmpose']['ready'])
        paths = {}
        for field, name in [('python', 'python.exe'), ('config', 'model.py'), ('checkpoint', 'model.pth')]:
            path = self.folder / 'fixture' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'configuration-only fixture; not model weights')
            paths[field] = str(path)
        config.save_config({'models': {'mmpose': paths}})
        self.models.reload_registry()
        self.assertIs(self.models.MODELS, original)
        self.assertEqual(id(self.models.MODELS), original_id)
        self.assertTrue(original['mmpose']['ready'])
        self.assertEqual(original['mmpose']['missing'], [])
        self.assertTrue(original['mmpose']['available'])
        self.assertTrue(original['vitpose_animal']['missing'])

    def test_empty_expected_hash_records_actual_hash_without_requiring_private_values(self):
        path = self.folder / 'hash-fixture.bin'
        path.write_bytes(b'local fixture')
        actual = self.models._verify(path, '')
        self.assertEqual(len(actual), 64)
        self.assertEqual(self.models._verify(path, actual), actual)
        with self.assertRaises(RuntimeError):
            self.models._verify(path, '0' * 64)
        self.assertNotIn('checkpoint_files_sha256', self.models._MODEL_METADATA['dlc'])

    def test_missing_configuration_worker_reports_error_before_model_import(self):
        script = Path(__file__).resolve().with_name('worker.py')
        result = subprocess.run([sys.executable, str(script), '--model', 'mmpose', '--device', 'cpu'],
                                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
        self.assertEqual(result.returncode, 1)
        markers = [s for s in result.stdout.splitlines() if s.startswith('RT_READY ')]
        self.assertEqual(len(markers), 1)
        message = json.loads(markers[0][len('RT_READY '):])
        self.assertEqual(message['status'], 'error')
        self.assertIn('not configured', message['error'])

    def test_registry_worker_helper_imports_are_standard_library_only(self):
        folder = str(Path(__file__).resolve().parent)
        code = ('import sys; sys.path.insert(0,' + repr(folder) + '); '
                'before=set(sys.modules); import models,worker,adaptation_helpers; '
                'assert not ({"torch","tensorflow","numpy","cv2"} & (set(sys.modules)-before)); print("stdlib-only")')
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('stdlib-only', result.stdout)


class AP10KSelectionTests(unittest.TestCase):
    def test_self_contained_expert_and_head_selection_without_torch(self):
        from adaptation_helpers import select_ap10k_state
        class TensorOperations:
            @staticmethod
            def cat(items, dim):
                if dim != 0:
                    raise AssertionError('Unexpected concatenation axis')
                return sum(items, [])
            @staticmethod
            def equal(left, right):
                return left == right
        original = {'backbone.mlp.fc2.weight': [1, 2], 'backbone.mlp.experts.3.weight': [3, 4],
                    'backbone.mlp.experts.1.weight': [99], 'keypoint_head.final_layer.weight': [-1],
                    'keypoint_head.final_layer.bias': [-2], 'associate_keypoint_heads.2.final_layer.weight': [17],
                    'associate_keypoint_heads.2.final_layer.bias': [18], 'shared.weight': [7]}
        selected = select_ap10k_state(original, TensorOperations)
        self.assertEqual(selected['backbone.mlp.fc2.weight'], [1, 2, 3, 4])
        self.assertEqual(selected['keypoint_head.final_layer.weight'], [17])
        self.assertEqual(selected['keypoint_head.final_layer.bias'], [18])
        self.assertEqual(selected['shared.weight'], [7])
        self.assertFalse(any('experts.' in key or key.startswith('associate_keypoint_heads.') for key in selected))


if __name__ == '__main__':
    unittest.main(verbosity=2)
