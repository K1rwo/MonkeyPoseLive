"""Build a private-data-free Windows onedir app and ZIP in a new directory.

Official references:
https://pyinstaller.org/en/stable/spec-files.html
https://pywebview.flowrl.com/guide/freezing.html
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

SOURCE_FILES = ['desktop.py', 'server.py', 'tracking.py', 'models.py', 'worker.py',
                'runtime_config.py', 'adaptation_helpers.py', 'config.example.json',
                'MonkeyPoseLive.spec', 'requirements-desktop.txt', 'LICENSE',
                'README.md', 'BUILD.md', 'Install-CurrentUser.ps1', 'NOTICE']
PUBLIC_FILES = ['LICENSE', 'NOTICE', 'README.md', 'BUILD.md',
                'config.example.json', 'Install-CurrentUser.ps1']
PUBLIC_TREES = {'docs': {'.md'}, 'licenses': {'.txt', '.md'}}
REQUIRED_LICENSES = {'pyinstaller','pywebview','pythonnet','numpy','opencv-python-headless','proxy-tools'}
VERSION = '0.1.0'
BACKEND_FILES = ['worker.py','models.py','runtime_config.py','adaptation_helpers.py']


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(data)
    return sha.hexdigest()


def public_files(source):
    """Only deliberate public text assets, never QA media or local config."""
    names = list(PUBLIC_FILES)
    for folder, extensions in PUBLIC_TREES.items():
        for path in sorted((source / folder).rglob('*')):
            if path.is_file() and path.suffix.lower() in extensions:
                names.append(path.relative_to(source).as_posix())
    return names


def copy_public_files(source, destination):
    names = public_files(source)
    for name in names:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    return names


def stage_sources(source, destination):
    destination.mkdir(parents=True, exist_ok=False)
    copied = []
    for name in SOURCE_FILES:
        path = source / name
        if not path.is_file():
            raise FileNotFoundError('Missing public build input: '+name)
        shutil.copy2(path, destination / name)
        copied.append(name)
    for name in copy_public_files(source, destination):
        if name not in copied:
            copied.append(name)
    for path in (source / 'web').rglob('*'):
        if path.is_file() and path.suffix.lower() in {'.html','.js','.css','.svg','.ico','.woff','.woff2'}:
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(relative.as_posix())
    return copied


def collect_licenses(app):
    directory = app / 'third-party-licenses'
    directory.mkdir(exist_ok=False)
    records = []
    licensed = set()
    for dist in sorted(metadata.distributions(), key=lambda d: normalized(d.metadata['Name'])):
        name = normalized(dist.metadata['Name'])
        if name == 'pip':
            continue
        copied = []
        for entry in dist.files or []:
            if not Path(str(entry)).name.upper().startswith(('LICENSE','LICENCE','COPYING','NOTICE')):
                continue
            source = Path(dist.locate_file(entry))
            if not source.is_file():
                continue
            parts = [p for p in Path(str(entry)).parts if p not in {'.','..','\\','/'}]
            target = directory / name / Path(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(target.relative_to(app).as_posix())
        if copied:
            licensed.add(name)
        records.append(dict(name=name, version=dist.version,
                            license=dist.metadata.get('License-Expression') or dist.metadata.get('License') or '',
                            homepage=dist.metadata.get('Home-page',''), license_files=copied,
                            role='build tool' if name.startswith('pyinstaller') else 'desktop dependency or dependency tool'))
    candidates = [Path(sys.base_prefix)/name for name in ['LICENSE.txt','LICENSE','LICENSE_PYTHON']]
    candidates += list(Path(sys.base_prefix).glob('licenses/*python*'))
    python_files = []
    for path in candidates:
        if path.is_file():
            target = directory / 'python' / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path,target)
            python_files.append(target.relative_to(app).as_posix())
    if not python_files:
        raise RuntimeError('Python license text not found in the build interpreter installation')
    records.append(dict(name='python',version=platform.python_version(),license_files=python_files,role='embedded runtime'))
    return write_license_notices(app, records)


def write_license_notices(app, records):
    """Preserve wheel notices plus upstream texts omitted from wheel metadata."""
    proxy = next(record for record in records if record['name']=='proxy-tools')
    proxy_license = 'licenses/proxy-tools-LICENSE.txt'
    if (app/proxy_license).is_file() and proxy_license not in proxy['license_files']:
        proxy['license_files'].append(proxy_license)
        proxy['license_note'] = 'Wheel metadata declares MIT; the declared upstream license text is BSD. Both the metadata and verbatim upstream notice are preserved; see licenses/proxy-tools-SOURCE.md.'
    sdk_names = ['licenses/WebView2-SDK-1.0.3856.49-LICENSE.txt',
                 'licenses/WebView2-SDK-1.0.3856.49-NOTICE.txt']
    if all((app/name).is_file() for name in sdk_names):
        if not any(record['name']=='microsoft-webview2-sdk' for record in records):
            records.append(dict(name='microsoft-webview2-sdk',version='1.0.3856.49',
                                license_files=sdk_names,role='SDK DLLs vendored by pywebview',
                                homepage='https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.3856.49'))
    for component, binary, text, default_version in [
            ('openssl','libcrypto-3-x64.dll','licenses/OpenSSL-3.5.8-LICENSE.txt','not embedded'),
            ('libffi','libffi-8.dll','licenses/libffi-LICENSE.txt','ABI 8; exact source version not embedded')]:
        path=app/'_internal'/binary
        if not path.is_file() or not (app/text).is_file():
            continue
        version=default_version
        import pefile
        with pefile.PE(str(path),fast_load=True) as executable:
            executable.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
            for group in getattr(executable,'FileInfo',[]):
                for info in group:
                    for table in getattr(info,'StringTable',[]):
                        if b'ProductVersion' in table.entries:
                            version=table.entries[b'ProductVersion'].decode('utf-8','replace')
        if not any(record['name']==component for record in records):
            records.append(dict(name=component,version=version,license_files=[text],
                                role='native dependency of bundled Python',binary_sha256=digest(path)))
    licensed = {record['name'] for record in records if record['license_files']}
    missing = (REQUIRED_LICENSES | {'microsoft-webview2-sdk'}) - licensed
    if missing:
        raise RuntimeError('Missing third-party license texts: '+', '.join(sorted(missing)))
    manifest = dict(packages=records, exact_terms='See the copied license texts, including NumPy/OpenCV third-party components and the PyInstaller bootloader exception.',
                    webview2='Uses the separately installed Microsoft Edge WebView2 Runtime; no Evergreen runtime installer is included.')
    (app/'THIRD_PARTY_NOTICES.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines = ['MonkeyPoseLive third-party notices','',
             'Exact license texts are in third-party-licenses. PyInstaller is a build tool; its bootloader distribution exception is preserved in its license.',
             'Microsoft WebView2 SDK DLL notices are in licenses/; the Evergreen Runtime is supplied separately by Microsoft.',
             'proxy-tools wheel license metadata differs from its upstream text; both are preserved in the JSON inventory and licenses/.','']
    lines += [r['name']+' '+r['version']+' — '+str(len(r['license_files']))+' license text(s)' for r in records]
    (app/'THIRD_PARTY_NOTICES.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return records


def inspect_release(app):
    forbidden = {'.pt','.pth','.ckpt','.safetensors','.onnx','.h5','.mp4','.avi','.mkv','.mov','.jsonl','.csv','.log'}
    files = []
    for path in app.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(app).as_posix()
        if path.suffix.lower() in forbidden or path.name.lower() in {'config.json','config.local.json','deployment_manifest.json'}:
            raise RuntimeError('Private/model/result file unexpectedly in release: '+relative)
        if any(part.lower() in {'torch','tensorflow','checkpoints','sessions','weights'} for part in path.relative_to(app).parts):
            raise RuntimeError('External model runtime unexpectedly bundled: '+relative)
        files.append(dict(path=relative,bytes=path.stat().st_size,sha256=digest(path)))
    return files


def verify_bundled_inputs(app, source_files):
    """The external worker sources live apart from host Python extensions."""
    for name in BACKEND_FILES:
        path = app/'_internal/backend'/name
        if not path.is_file() or digest(path) != source_files[name]:
            raise RuntimeError('Bundled backend source mismatch: '+name)
        if (app/'_internal'/name).exists():
            raise RuntimeError('Backend source must be isolated from host extensions: '+name)
    for name, expected in source_files.items():
        if name.startswith('web/') or name == 'config.example.json':
            path = app/'_internal'/name
            if not path.is_file() or digest(path) != expected:
                raise RuntimeError('Bundled resource mismatch: '+name)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=Path(__file__).resolve().parent/'dist')
    parser.add_argument('--staging-root',type=Path)
    parser.add_argument('--skip-self-test',action='store_true')
    args=parser.parse_args(argv)
    if os.name!='nt':
        raise RuntimeError('Build Windows executables on Windows')
    output=args.output_dir.resolve()
    if output.exists():
        raise FileExistsError('Preserve existing build; choose a new --output-dir: '+str(output))
    source=Path(__file__).resolve().parent
    parent=args.staging_root.resolve() if args.staging_root else Path(tempfile.gettempdir())
    parent.mkdir(parents=True,exist_ok=True)
    stage=parent/('MonkeyPoseLiveBuild-'+uuid.uuid4().hex)
    copied=stage_sources(source,stage/'source')
    output.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ,PYINSTALLER_CONFIG_DIR=str(stage/'pyinstaller-cache'),
             OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',OPENBLAS_NUM_THREADS='4',NUMEXPR_NUM_THREADS='4',
             PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1')
    command=[sys.executable,'-m','PyInstaller','--distpath',str(output),'--workpath',str(stage/'work'),str(stage/'source/MonkeyPoseLive.spec')]
    with (stage/'pyinstaller.log').open('xb') as log:
        subprocess.run(command,cwd=stage/'source',env=env,stdout=log,stderr=log,check=True)
    app=output/'MonkeyPoseLive'
    copy_public_files(stage/'source', app)
    packages=collect_licenses(app)
    self_test=None
    if not args.skip_self_test:
        report=stage/'self-test.json'
        subprocess.run([str(app/'MonkeyPoseLive.exe'),'--self-test','--config',str(stage/'missing-test-config.json'),
                        '--report',str(report)],env=env,timeout=120,check=True)
        self_test=json.loads(report.read_text(encoding='utf-8'))
        if not self_test.get('all_passed'):
            raise RuntimeError('Built executable self-test failed; inspect '+str(report))
    source_hashes={name:digest(stage/'source'/name) for name in copied}
    verify_bundled_inputs(app,source_hashes)
    files=inspect_release(app)
    manifest=dict(app='MonkeyPoseLive',version=VERSION,created_at_utc=datetime.now(timezone.utc).isoformat(),
                  python=platform.python_version(),architecture=platform.machine(),
                  source_files=source_hashes,
                  backend_resource_directory='_internal/backend',
                  build_script_sha256=digest(Path(__file__)),
                  public_asset_files=public_files(stage/'source'),
                  packages={r['name']:r['version'] for r in packages},
                  headless_self_test_passed=bool(self_test and self_test['all_passed']),
                  physical_camera_opened=False,model_inference_started=False,
                  private_configuration_included=False,weights_or_research_data_included=False,files=files)
    (app/'BUILD_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    archive=output/f'MonkeyPoseLive-{VERSION}-windows-x64.zip'
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zipped:
        for path in sorted(app.rglob('*')):
            if path.is_file():
                zipped.write(path,path.relative_to(output).as_posix())
    (output/(archive.name+'.sha256')).write_text(digest(archive)+'  '+archive.name+'\n',encoding='ascii')
    result=dict(executable=str(app/'MonkeyPoseLive.exe'),zip=str(archive),zip_sha256=digest(archive),
                self_test=self_test,build_log=str(stage/'pyinstaller.log'),staging_directory=str(stage))
    (output/'build_result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
