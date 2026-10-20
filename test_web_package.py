"""Release boundary tests use synthetic fixtures, never local deployment data."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import build_web


class WebPackageTests(unittest.TestCase):
    def fixture(self, root):
        source = root / 'public-source'
        source.mkdir()
        expected = {}
        for name in build_web.FILES:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = ('Public fixture: ' + name + '\n').encode('utf-8')
            path.write_bytes(payload)
            expected[name] = payload
        for name in ['licenses/MIT-upstream.txt', 'licenses/Apache-upstream.txt']:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = ('Public license fixture: ' + name + '\n').encode('utf-8')
            path.write_bytes(payload)
            expected[name] = payload
        return source, expected

    def test_allowlist_excludes_credentials_media_weights_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, expected = self.fixture(root)
            excluded = [
                'config.json', 'auth.json', 'access.txt', '.env',
                'remote-server.log', 'private-diagnostic.json', 'predictions.jsonl',
                'trajectory.csv', 'source.mp4', 'recording.mov', 'model.pt',
                'web/private-frame.jpg', 'docs/private-run.md',
                'docs/private-screen.png', 'weights/checkpoint.pth',
                'uploads/input.video', 'results/annotated.mp4',
                'private/config.json', 'private/auth.json', 'private/access.txt',
                'private/logs/service.log', 'licenses/private-diagnostic.json',
            ]
            private_marker = b'PRIVATE-FIXTURE-MUST-NEVER-BE-RELEASED'
            for name in excluded:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(private_marker + b':' + name.encode('ascii'))

            result = build_web.build(source, root / 'release')
            prefix = f'MonkeyPoseLive-Web-{build_web.VERSION}/'
            with zipfile.ZipFile(result['archive']) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {prefix + name for name in expected} | {prefix + 'WEB_MANIFEST.json'},
                )
                for member in archive.infolist():
                    self.assertFalse(member.filename.startswith('/'))
                    self.assertNotIn('..', Path(member.filename).parts)
                    self.assertNotIn(private_marker, archive.read(member))
                for name in excluded:
                    self.assertNotIn(prefix + name, archive.namelist())
                for name, payload in expected.items():
                    self.assertEqual(archive.read(prefix + name), payload)

    def test_manifest_and_checksum_match_actual_archive_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, expected = self.fixture(root)
            result = build_web.build(source, root / 'release')
            archive_path = Path(result['archive'])
            prefix = f'MonkeyPoseLive-Web-{build_web.VERSION}/'
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read(prefix + 'WEB_MANIFEST.json'))
                self.assertEqual(manifest['version'], build_web.VERSION)
                self.assertEqual(manifest['distribution'], 'web-host-source')
                self.assertFalse(manifest['models_and_weights_bundled'])
                self.assertFalse(manifest['private_data_bundled'])
                self.assertEqual(set(manifest['files']), set(expected))
                for name, recorded_digest in manifest['files'].items():
                    self.assertEqual(
                        recorded_digest,
                        hashlib.sha256(archive.read(prefix + name)).hexdigest(),
                        name,
                    )
            actual_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            self.assertEqual(result['sha256'], actual_digest)
            self.assertEqual(result['file_count'], len(expected))
            self.assertEqual(result['bytes'], archive_path.stat().st_size)
            self.assertEqual(
                archive_path.with_suffix('.sha256').read_text(encoding='ascii'),
                actual_digest + '  ' + archive_path.name + '\n',
            )

    def test_existing_archive_or_checksum_is_never_overwritten(self):
        for existing_suffix in ['.zip', '.sha256']:
            with self.subTest(existing_suffix=existing_suffix):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source, _ = self.fixture(root)
                    release = root / 'release'
                    release.mkdir()
                    existing = release / (f'MonkeyPoseLive-Web-{build_web.VERSION}' + existing_suffix)
                    sentinel = b'existing published artifact, preserve exact bytes'
                    existing.write_bytes(sentinel)
                    with self.assertRaises(FileExistsError):
                        build_web.build(source, release)
                    self.assertEqual(existing.read_bytes(), sentinel)
                    self.assertEqual(list(release.iterdir()), [existing])

    def test_missing_required_document_fails_before_writing_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = self.fixture(root)
            (source / 'docs/WEB_DEPLOYMENT.md').unlink()
            release = root / 'release'
            with self.assertRaises(FileNotFoundError):
                build_web.build(source, release)
            self.assertFalse(list(release.glob('*.zip')))
            self.assertFalse(list(release.glob('*.sha256')))


if __name__ == '__main__':
    unittest.main()
