"""Refresh public release text only after verifying frozen sources are unchanged.

The existing draft ZIP and a backup of replaced text are preserved. Any Python,
web or example-configuration change requires a new PyInstaller build instead.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid
import zipfile

from build_windows import (VERSION, copy_public_files, digest, inspect_release,
                           public_files, write_license_notices, verify_bundled_inputs)


def verify_frozen_inputs(source, manifest):
    mutable = set(public_files(source)) - {'config.example.json'}
    for name, expected in manifest['source_files'].items():
        if name in mutable:
            continue
        path = source / name
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError('Frozen source changed; rebuild the executable: '+name)


def refresh(source, app, archive):
    if archive.exists() or archive.with_suffix(archive.suffix+'.sha256').exists():
        raise FileExistsError('Preserve existing release: '+str(archive))
    manifest_path = app / 'BUILD_MANIFEST.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    verify_frozen_inputs(source, manifest)
    verify_bundled_inputs(app, manifest['source_files'])
    before = inspect_release(app)
    binary_hashes = {row['path']: row['sha256'] for row in before
                     if row['path'].lower().endswith(('.exe', '.dll', '.pyd', '.pyz'))}
    backup = app.parent / ('public-assets-before-refresh-'+uuid.uuid4().hex)
    backup.mkdir(parents=True, exist_ok=False)
    for name in ['BUILD_MANIFEST.json', 'THIRD_PARTY_NOTICES.json', 'THIRD_PARTY_NOTICES.txt', *public_files(app)]:
        old = app / name
        if old.is_file():
            target = backup / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, target)
    names = copy_public_files(source, app)
    inventory = json.loads((app/'THIRD_PARTY_NOTICES.json').read_text(encoding='utf-8'))
    packages = write_license_notices(app, inventory['packages'])
    manifest.setdefault('source_files_at_build', dict(manifest['source_files']))
    manifest['source_files'].update({name: digest(source/name) for name in names})
    manifest['public_asset_files'] = names
    manifest['packages'] = {record['name']:record['version'] for record in packages}
    manifest['public_assets_refreshed_at_utc'] = datetime.now(timezone.utc).isoformat()
    manifest['release_refresh_script_sha256'] = digest(Path(__file__))
    manifest['frozen_sources_rechecked'] = True
    manifest['binary_files_unchanged_by_refresh'] = True
    after = inspect_release(app)
    for row in after:
        if row['path'] in binary_hashes and binary_hashes[row['path']] != row['sha256']:
            raise RuntimeError('Unexpected binary modification: '+row['path'])
    manifest['files'] = [row for row in after if row['path'] != 'BUILD_MANIFEST.json']
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zipped:
        for path in sorted(app.rglob('*')):
            if path.is_file():
                zipped.write(path, (Path(app.name)/path.relative_to(app)).as_posix())
    archive.with_suffix(archive.suffix+'.sha256').write_text(digest(archive)+'  '+archive.name+'\n',encoding='ascii')
    result = dict(zip=str(archive),zip_sha256=digest(archive),public_asset_count=len(names),
                  frozen_sources_unchanged=True,binaries_unchanged=True,previous_public_assets=str(backup))
    (archive.parent/(archive.stem+'.result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir',type=Path,required=True)
    parser.add_argument('--archive',type=Path,required=True)
    args=parser.parse_args(argv)
    result=refresh(Path(__file__).resolve().parent,args.app_dir.resolve(),args.archive.resolve())
    print(json.dumps(result,ensure_ascii=True),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
