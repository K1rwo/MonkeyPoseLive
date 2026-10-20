"""Package an allowlisted web-host source distribution, without private data."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

VERSION='0.2.0'
FILES=['remote_server.py','web_inference.py','tracking.py','worker.py','models.py','runtime_config.py',
       'adaptation_helpers.py','config.example.json','requirements-web.txt','Start-Web.ps1','Stop-Web.ps1',
       'Web-Common.ps1','Install-WebTunnel.ps1','README.md','LICENSE','NOTICE',
       'docs/WEB_DEPLOYMENT.md','docs/BACKENDS.md','docs/WEB_VALIDATION.md',
       'web/remote.html','web/remote.js','web/remote.css']


def build(source,output):
    source=Path(source).resolve()
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    name=f'MonkeyPoseLive-Web-{VERSION}'
    archive=output/(name+'.zip')
    checksum=output/(name+'.sha256')
    if archive.exists() or checksum.exists():
        raise FileExistsError('Preserve existing release files; choose a new output directory')
    files=FILES+[p.relative_to(source).as_posix() for p in sorted((source/'licenses').glob('*.txt'))]
    manifest={'version':VERSION,'distribution':'web-host-source','python':'3.12',
              'models_and_weights_bundled':False,'private_data_bundled':False,
              'files':{n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in files}}
    with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as zipped:
        for n in files:
            zipped.write(source/n,name+'/'+n)
        zipped.writestr(name+'/WEB_MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(digest+'  '+archive.name+'\n',encoding='ascii')
    return {'archive':str(archive),'sha256':digest,'file_count':len(files),'bytes':archive.stat().st_size}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=Path('dist/web'))
    args=parser.parse_args()
    print(json.dumps(build(Path(__file__).parent,args.output_dir),indent=2))
