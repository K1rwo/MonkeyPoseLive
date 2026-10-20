"""Authenticated remote web service; separate from the desktop loopback API.

Deploy behind an HTTPS reverse tunnel. This handler deliberately has no host
camera, filesystem browsing, arbitrary model path or setup/config endpoints.
"""
from __future__ import annotations
import argparse
from concurrent.futures import Future, TimeoutError as FutureTimeout
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import queue
import secrets
import socket
import threading
import time
import traceback
from urllib.parse import urlsplit, parse_qs
import uuid

VERSION='0.2.0'
CHUNK=4*1024*1024
MAX_UPLOAD=512*1024*1024
MAX_STORAGE=5*1024**3
TERMINAL={'completed','cancelled','error'}
HERE=Path(__file__).resolve().parent


class HTTPError(Exception):
    def __init__(self,status,message):
        super().__init__(message)
        self.status=status


def safe_models():
    from models import MODELS
    return [{k:v.get(k) for k in ('id','label','ready','detector_supported','score_semantics','caveat')}
            for v in MODELS.values()]


class Job:
    def __init__(self,app,owner,config):
        self.id=uuid.uuid4().hex
        self.owner=owner
        self.config=config
        self.kind=config['kind']
        self.directory=app.data_dir/'jobs'/self.id
        self.directory.mkdir(parents=True,exist_ok=False)
        self.lock=threading.RLock()
        self.upload_lock=threading.Lock()
        self.frame_lock=threading.Lock()
        self.stop_event=threading.Event()
        self.frames=queue.Queue(maxsize=1)
        self.preview=None
        self.result={}
        self.outputs_closed=False
        self.engine_active=False
        self.received=0
        self.upload_total=None
        self.last_sequence=-1
        self.storage_usage=app.storage_usage
        self.storage_limit=MAX_STORAGE
        self.state=dict(id=self.id,kind=self.kind,model=config['model'],status='uploading' if self.kind=='video' else 'queued',
                        processed=0,total_frames=0,progress=0,error=None,created_at=time.time(),record=config['record'])
        (self.directory/'job.json').write_text(json.dumps(dict(config=config,job_id=self.id,created_at=self.state['created_at'],
            training=False,reference_accuracy_validated=False),ensure_ascii=False,indent=2),encoding='utf-8')

    def update(self,**changes):
        with self.lock:
            self.state.update(changes)

    def snapshot(self):
        with self.lock:
            downloads={name:False for name in ('json','csv','video')}
            if self.outputs_closed and self.state['status'] in TERMINAL and self.config['record']:
                downloads={key:(self.directory/filename).is_file() for key,filename in
                           [('json','predictions.jsonl'),('csv','trajectory.csv'),('video','annotated.mp4')]}
            snapshot=dict(self.state,received=self.received,total=self.upload_total,downloads=downloads)
            if self.state['status']=='cancelled' and self.engine_active and not self.outputs_closed:
                snapshot['status']='cancelling'
            return snapshot


class WebApplication:
    def __init__(self,data_dir,public_url=None,engine_factory=None,models_provider=None):
        self.data_dir=Path(data_dir).resolve()
        # Public sources must never become a private deployment data directory.
        if self.data_dir==HERE or HERE in self.data_dir.parents:
            raise ValueError('Web data directory must be outside the source repository')
        self.data_dir.mkdir(parents=True,exist_ok=True)
        self.public_url=None
        if public_url:
            parsed=urlsplit(public_url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
                raise ValueError('--public-url must be an exact HTTPS origin')
            self.public_url='https://'+parsed.netloc.lower()
        self.models_provider=models_provider or safe_models
        if engine_factory is None:
            from web_inference import InferenceEngine
            engine_factory=InferenceEngine
        self.engine_factory=engine_factory
        self.lock=threading.RLock()
        self.sessions={}
        self.jobs={}
        self.queue=queue.Queue(maxsize=10)
        self.closed=threading.Event()
        self.login_attempts=[]
        self.password_file=self.data_dir/'access.txt'
        credential_file=self.data_dir/'auth.json'
        if credential_file.exists():
            self.credential=json.loads(credential_file.read_text(encoding='utf-8'))
        else:
            password=secrets.token_urlsafe(24)
            salt=secrets.token_hex(16)
            digest=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
            self.credential=dict(salt=salt,digest=digest)
            credential_file.write_text(json.dumps(self.credential),encoding='utf-8')
            self.password_file.write_text(password+'\n',encoding='utf-8')
            if os.name!='nt':
                credential_file.chmod(0o600)
                self.password_file.chmod(0o600)
        self.shutdown_token=secrets.token_urlsafe(32)
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.thread.start()

    def storage_usage(self):
        return sum(p.stat().st_size for p in self.data_dir.glob('jobs/*/*') if p.is_file())

    def login(self,password):
        if not isinstance(password,str) or len(password)>256:
            raise HTTPError(401,'密码不正确')
        now=time.monotonic()
        with self.lock:
            self.login_attempts=[t for t in self.login_attempts if now-t<60]
            if len(self.login_attempts)>=12:
                raise HTTPError(429,'登录尝试过多，请稍后再试')
            self.login_attempts.append(now)
            if len(self.sessions)>=64:
                self._expire_sessions()
                if len(self.sessions)>=64:
                    raise HTTPError(429,'会话数量达到上限')
            # Bound simultaneous expensive password verification by this lock.
            digest=hashlib.scrypt(password.encode(),salt=bytes.fromhex(self.credential['salt']),n=16384,r=8,p=1).hex()
            if not hmac.compare_digest(digest,self.credential['digest']):
                raise HTTPError(401,'密码不正确')
            token=secrets.token_urlsafe(32)
            session=dict(id=secrets.token_hex(16),csrf=secrets.token_urlsafe(24),expires=now+12*3600)
            self.sessions[token]=session
            return token,session

    def _expire_sessions(self):
        for token,s in list(self.sessions.items()):
            if s['expires']<time.monotonic():
                self.logout(token)

    def logout(self,token):
        with self.lock:
            session=self.sessions.pop(token,None)
            if session:
                for job in self.jobs.values():
                    with job.lock:
                        if job.owner==session['id'] and job.state['status'] not in TERMINAL:
                            job.stop_event.set()
                            job.update(status='cancelled')

    def session(self,token):
        with self.lock:
            s=self.sessions.get(token)
            if not s or s['expires']<time.monotonic():
                self.logout(token)
                raise HTTPError(401,'请先登录')
            return s

    def create_job(self,owner,c):
        if not isinstance(c,dict) or set(c)-{'kind','model','threshold','roi','detector','record'}:
            raise HTTPError(400,'任务参数无效')
        models={m['id']:m for m in self.models_provider()}
        model=c.get('model')
        if model not in models or not models[model].get('ready'):
            raise HTTPError(400,'模型尚未准备好')
        kind=c.get('kind')
        if kind not in {'video','camera'}:
            raise HTTPError(400,'未知输入类型')
        roi=c.get('roi',[0.,0.,1.,1.])
        finite=lambda n:isinstance(n,(float,int)) and not isinstance(n,bool) and math.isfinite(n)
        if not isinstance(roi,list) or len(roi)!=4 or not all(finite(v) for v in roi) or not (0<=roi[0]<roi[2]<=1 and 0<=roi[1]<roi[3]<=1):
            raise HTTPError(400,'ROI须为0–1范围的有效矩形')
        threshold=c.get('threshold',.2)
        if not finite(threshold) or not 0<=threshold<=1:
            raise HTTPError(400,'阈值须在0–1之间')
        if any(k in c and not isinstance(c[k],bool) for k in ('detector','record')):
            raise HTTPError(400,'开关参数须为布尔值')
        if c.get('detector',False) and not models[model].get('detector_supported'):
            raise HTTPError(400,'此模型不支持自动定位')
        with self.lock:
            if len(self.jobs)>=100 or sum(j.owner==owner and j.snapshot()['status'] not in TERMINAL for j in self.jobs.values())>=3:
                raise HTTPError(429,'任务数量达到上限')
            # Reserve a full upload per outstanding video to bound disk usage.
            reservations=sum(MAX_UPLOAD-j.received for j in self.jobs.values()
                             if j.kind=='video' and j.snapshot()['status']=='uploading')
            if self.storage_usage()+reservations+MAX_UPLOAD>MAX_STORAGE:
                raise HTTPError(507,'本机存储配额不足，请管理员归档旧任务')
            job=Job(self,owner,dict(kind=kind,model=model,threshold=float(threshold),roi=roi,
                                  detector=c.get('detector',False),record=c.get('record',True)))
            self.jobs[job.id]=job
            if kind=='camera':
                self.enqueue(job)
            return job

    def enqueue(self,job):
        if job.stop_event.is_set():
            raise HTTPError(409,'任务已停止')
        job.update(status='queued')
        try:
            self.queue.put_nowait(job)
        except queue.Full:
            job.update(status='error',error='任务队列已满')
            raise HTTPError(429,'任务队列已满')

    def get_job(self,owner,identifier):
        with self.lock:
            job=self.jobs.get(identifier)
            if not job or job.owner!=owner:
                raise HTTPError(404,'任务不存在')
            return job

    def _run(self):
        while not self.closed.is_set():
            with self.lock:
                self._expire_sessions()
            try:
                job=self.queue.get(timeout=.3)
            except queue.Empty:
                continue
            with job.lock:
                if job.stop_event.is_set():
                    continue
                job.engine_active=True
            outcome=dict(status='error',error='任务未能完成；请由主机管理员检查本机日志及输入格式')
            try:
                self.engine_factory().run(job)
                outcome=dict(status='cancelled' if job.stop_event.is_set() else 'completed')
            except InterruptedError:
                outcome=dict(status='cancelled')
            except Exception:
                with (job.directory/'private_error.log').open('a',encoding='utf-8') as log:
                    traceback.print_exc(file=log)
                outcome=dict(status='cancelled' if job.stop_event.is_set() else 'error',
                             error='任务未能完成；请由主机管理员检查本机日志及输入格式')
            finally:
                with job.lock:
                    job.outputs_closed=True
                    job.engine_active=False
                    # Publish terminal status together with artifact readiness.
                    # Polling clients must see cancellation through teardown.
                    outcome['status']='cancelled' if job.stop_event.is_set() else outcome['status']
                    job.state.update(outcome)

    def close(self):
        self.closed.set()
        with self.lock:
            for job in self.jobs.values():
                job.stop_event.set()
        self.thread.join(timeout=12)


def make_server(app,host='127.0.0.1',port=0):
    if host not in {'127.0.0.1','localhost','::1'}:
        raise ValueError('Use a loopback listener behind an HTTPS tunnel')
    class Handler(BaseHTTPRequestHandler):
        server_version='MonkeyPoseWeb'
        sys_version=''
        protocol_version='HTTP/1.1'

        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self,*args):
            pass

        def send_data(self,status,data,ctype='application/json; charset=utf-8',headers=None):
            if not isinstance(data,bytes):
                data=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',ctype)
            self.send_header('Content-Length',str(len(data)))
            if self.close_connection:
                self.send_header('Connection','close')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Permissions-Policy','camera=(self), microphone=()')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for name,value in (headers or {}).items():
                self.send_header(name,value)
            self.end_headers()
            if self.command!='HEAD':
                self.wfile.write(data)

        def origin_check(self,unsafe=False,local=False):
            if len(self.headers.get_all('Host',[]))!=1:
                raise HTTPError(403,'Host不被允许')
            host_header=self.headers.get('Host','').lower()
            local_hosts={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
            public_host=urlsplit(app.public_url).netloc if app.public_url else None
            if host_header not in local_hosts and (local or host_header!=public_host):
                raise HTTPError(403,'Host不被允许')
            origins=self.headers.get_all('Origin',[])
            if len(origins)>1:
                raise HTTPError(403,'Origin不被允许')
            origin=origins[0] if origins else None
            expected=app.public_url if host_header==public_host else 'http://'+host_header
            if origin is not None and origin!=expected:
                raise HTTPError(403,'Origin不被允许')
            # Login and all browser writes require explicit same-origin proof.
            if unsafe and not local and origin!=expected:
                raise HTTPError(403,'需要同源请求')
            if self.headers.get('Sec-Fetch-Site') in {'cross-site','same-site'}:
                raise HTTPError(403,'跨站请求被拒绝')

        def body(self,limit):
            values=self.headers.get_all('Content-Length',[])
            if self.headers.get_all('Transfer-Encoding',[]) or len(values)!=1 or not values[0].isdigit():
                raise HTTPError(411,'需要单一有效Content-Length')
            size=int(values[0])
            if size>limit:
                raise HTTPError(413,'请求超过大小限制')
            data=self.rfile.read(size)
            self._body_read=len(data)
            if len(data)!=size:
                raise HTTPError(400,'上传不完整')
            return data

        def discard_small_body(self):
            """Avoid a Windows TCP reset hiding an already-sent error response.

            An early Host/auth rejection can leave a small, legitimate POST body
            unread. Bound both work and time; never wait for an untrusted large
            upload or accept ambiguous request framing during error handling.
            """
            values=self.headers.get_all('Content-Length',[])
            if self.headers.get_all('Transfer-Encoding',[]) or len(values)!=1 or not values[0].isdigit():
                return
            remaining=int(values[0])-getattr(self,'_body_read',0)
            if not 0<remaining<=65536:
                return
            deadline=time.monotonic()+.05
            try:
                while remaining and time.monotonic()<deadline:
                    self.connection.settimeout(max(.001,deadline-time.monotonic()))
                    chunk=self.rfile.read1(min(4096,remaining))
                    if not chunk:
                        break
                    remaining-=len(chunk)
            except OSError:
                pass

        def control_body(self):
            # These authenticated control routes accept a bodyless request.
            # A reverse proxy can remove Content-Length: 0. Explicit framing
            # still goes through the same strict size and ambiguity checks.
            if self.headers.get_all('Content-Length',[]) or self.headers.get_all('Transfer-Encoding',[]):
                return self.body(16384)
            return b''

        def json_body(self):
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                raise HTTPError(415,'需要application/json')
            try:
                value=json.loads(self.body(16384),parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError,UnicodeError,RecursionError):
                raise HTTPError(400,'JSON无效')
            if not isinstance(value,dict):
                raise HTTPError(400,'需要JSON对象')
            return value

        def token(self):
            cookie=SimpleCookie()
            try:
                values=self.headers.get_all('Cookie',[])
                if len(values)!=1:
                    return ''
                cookie.load(values[0])
                return cookie['monkeypose_session'].value if 'monkeypose_session' in cookie else ''
            except Exception:
                return ''

        def auth(self,unsafe):
            s=app.session(self.token())
            if unsafe:
                values=self.headers.get_all('X-CSRF-Token',[])
                if len(values)!=1 or not hmac.compare_digest(values[0],s['csrf']):
                    raise HTTPError(403,'CSRF验证失败')
            return s

        def cookie(self,token='',clear=False):
            return 'monkeypose_session='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+('0' if clear else '43200')+('; Secure' if app.public_url else '')

        def dispatch(self):
            self._body_read=0
            try:
                parsed=urlsplit(self.path)
                if parsed.scheme or parsed.netloc or '%' in parsed.path or '\\' in parsed.path or '..' in parsed.path:
                    raise HTTPError(404,'页面不存在')
                path=parsed.path
                unsafe=self.command not in {'GET','HEAD'}
                if path=='/_local/shutdown':
                    self.origin_check(local=True)
                    if self.command!='POST' or self.client_address[0] not in {'127.0.0.1','::1'} or not hmac.compare_digest(self.headers.get('X-Admin-Token',''),app.shutdown_token):
                        raise HTTPError(403,'管理验证失败')
                    self.body(16384)
                    self.send_data(200,{'stopping':True})
                    threading.Thread(target=self.server.shutdown,daemon=True).start()
                    return
                self.origin_check(unsafe)
                if self.command=='GET' and path=='/health':
                    self.send_data(200,dict(app='MonkeyPoseLiveWeb',version=VERSION))
                    return
                if self.command=='GET' and path in {'/','/remote.js','/remote.css'}:
                    name={'/':'remote.html','/remote.js':'remote.js','/remote.css':'remote.css'}[path]
                    ctype={'/':'text/html; charset=utf-8','/remote.js':'text/javascript; charset=utf-8','/remote.css':'text/css; charset=utf-8'}[path]
                    self.send_data(200,(HERE/'web'/name).read_bytes(),ctype)
                    return
                if path=='/api/login' and self.command=='POST':
                    token,session=app.login(self.json_body().get('password'))
                    self.send_data(200,dict(authenticated=True,csrf=session['csrf']),headers={'Set-Cookie':self.cookie(token)})
                    return
                session=self.auth(unsafe)
                if path=='/api/session' and self.command=='GET':
                    self.send_data(200,dict(authenticated=True,csrf=session['csrf'],models=app.models_provider(),
                        limits=dict(max_upload_bytes=MAX_UPLOAD,chunk_bytes=CHUNK,max_video_seconds=600,
                                    max_camera_idle_seconds=120),version=VERSION))
                    return
                if path=='/api/logout' and self.command=='POST':
                    self.control_body()
                    app.logout(self.token())
                    self.send_data(200,{'authenticated':False},headers={'Set-Cookie':self.cookie(clear=True)})
                    return
                if path=='/api/jobs' and self.command=='POST':
                    job=app.create_job(session['id'],self.json_body())
                    self.send_data(201,job.snapshot())
                    return
                if path=='/api/jobs' and self.command=='GET':
                    with app.lock:
                        jobs=[job.snapshot() for job in app.jobs.values() if job.owner==session['id']]
                    self.send_data(200,dict(jobs=jobs))
                    return
                parts=path.strip('/').split('/')
                if len(parts)<3 or parts[:2]!=['api','jobs']:
                    raise HTTPError(404,'页面不存在')
                job=app.get_job(session['id'],parts[2])
                tail=parts[3:]
                if not tail and self.command=='GET':
                    self.send_data(200,job.snapshot())
                elif not tail and self.command=='DELETE':
                    self.control_body()
                    with job.lock:
                        job.stop_event.set()
                        job.update(status='cancelled')
                    self.send_data(200,job.snapshot())
                elif tail==['video'] and self.command=='PUT':
                    self.upload(job,parse_qs(parsed.query,strict_parsing=True))
                elif tail==['frame'] and self.command=='POST':
                    self.frame(job,parse_qs(parsed.query,strict_parsing=True))
                elif tail==['preview.jpg'] and self.command=='GET':
                    with job.lock:
                        preview=job.preview
                    if not preview:
                        raise HTTPError(404,'结果尚未就绪')
                    self.send_data(200,preview,'image/jpeg')
                elif tail==['result'] and self.command=='GET':
                    with job.lock:
                        result=dict(job.result)
                    self.send_data(200,result)
                elif len(tail)==2 and tail[0]=='download' and self.command=='GET':
                    files={'json':('predictions.jsonl','application/x-ndjson'),'csv':('trajectory.csv','text/csv; charset=utf-8'),
                           'video':('annotated.mp4','video/mp4')}
                    if tail[1] not in files or not job.snapshot()['downloads'][tail[1]]:
                        raise HTTPError(404,'结果文件尚未就绪')
                    filename,ctype=files[tail[1]]
                    file=job.directory/filename
                    self.send_response(200)
                    self.send_header('Content-Type',ctype)
                    self.send_header('Content-Length',str(file.stat().st_size))
                    self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
                    self.send_header('Cache-Control','no-store')
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.end_headers()
                    with file.open('rb') as source:
                        while block:=source.read(1024*1024):
                            self.wfile.write(block)
                else:
                    raise HTTPError(404,'页面不存在')
            except HTTPError as ex:
                self.close_connection=True
                self.send_data(ex.status,{'error':str(ex)})
                self.wfile.flush()
                self.discard_small_body()
            except (ValueError,TypeError,OverflowError):
                self.close_connection=True
                self.send_data(400,{'error':'请求参数无效'})
                self.wfile.flush()
                self.discard_small_body()
            except (BrokenPipeError,ConnectionResetError,socket.timeout):
                self.close_connection=True
            except Exception:
                self.close_connection=True
                with (app.data_dir/'private_server_error.log').open('a',encoding='utf-8') as log:
                    traceback.print_exc(file=log)
                self.send_data(500,{'error':'服务器无法完成请求，请联系主机管理员'})

        def upload(self,job,query):
            if job.kind!='video' or not job.upload_lock.acquire(blocking=False):
                raise HTTPError(409,'任务不接受此上传或有上传正在进行')
            try:
                if job.snapshot()['status']!='uploading':
                    raise HTTPError(409,'任务已结束上传')
                if set(query)!={'offset','final'} or any(len(v)!=1 for v in query.values()):
                    raise HTTPError(400,'上传参数无效')
                offset=int(query['offset'][0])
                final=query['final'][0]
                values=self.headers.get_all('X-Upload-Length',[])
                if len(values)!=1 or not values[0].isdigit():
                    raise HTTPError(400,'需要完整视频长度')
                total=int(values[0])
                if not 0<total<=MAX_UPLOAD or final not in {'0','1'}:
                    raise HTTPError(413,'上传视频超出512MiB限制或结束标记无效')
                if offset!=job.received or job.upload_total not in {None,total}:
                    raise HTTPError(409,'分块偏移与当前上传不一致')
                data=self.body(CHUNK)
                if not data or job.received+len(data)>total or (final=='1')!=(job.received+len(data)==total):
                    raise HTTPError(400,'分块长度与结束标记不一致')
                # A cancel/logout can occur while this handler waits for bytes.
                # Commit the chunk and queue transition atomically with respect
                # to cancellation; an in-flight request cannot revive a job.
                with app.lock,job.lock:
                    if job.stop_event.is_set() or job.state['status']!='uploading':
                        raise HTTPError(409,'任务已停止')
                    if app.storage_usage()+len(data)>MAX_STORAGE:
                        raise HTTPError(507,'本机存储已满')
                    with (job.directory/'upload.bin').open('ab') as stream:
                        stream.write(data)
                    job.upload_total=total
                    job.received+=len(data)
                    if final=='1':
                        app.enqueue(job)
                self.send_data(200,dict(received=job.received,total=total,status=job.snapshot()['status']))
            finally:
                job.upload_lock.release()

        def frame(self,job,query):
            from web_inference import jpeg_dimensions
            if job.kind!='camera' or job.snapshot()['status']!='running' or not job.frame_lock.acquire(blocking=False):
                raise HTTPError(409,'摄像头模型尚未就绪或上一帧仍在处理')
            try:
                if set(query)!={'sequence','timestamp'} or any(len(v)!=1 for v in query.values()):
                    raise HTTPError(400,'摄像头参数无效')
                seq=int(query['sequence'][0])
                stamp=float(query['timestamp'][0])
                if seq<=job.last_sequence or seq<0 or not math.isfinite(stamp) or stamp<0:
                    raise HTTPError(400,'帧编号和时间须有效递增')
                if self.headers.get('Content-Type','').split(';')[0]!='image/jpeg':
                    raise HTTPError(415,'需要image/jpeg')
                data=self.body(2*1024*1024)
                jpeg_dimensions(data)
                with job.lock:
                    if job.stop_event.is_set() or job.state['status']!='running':
                        raise HTTPError(409,'任务已停止')
                    job.last_sequence=seq
                    future=Future()
                    job.frames.put_nowait((seq,stamp,data,future))
                deadline=time.monotonic()+95
                while not future.done():
                    if job.stop_event.is_set() or job.snapshot()['status'] in TERMINAL:
                        raise HTTPError(409,'任务已停止')
                    if time.monotonic()>deadline:
                        job.stop_event.set()
                        raise HTTPError(504,'推理响应超时')
                    try:
                        future.result(timeout=.2)
                    except FutureTimeout:
                        continue
                self.send_data(200,future.result())
            finally:
                job.frame_lock.release()

        do_GET=dispatch
        do_POST=dispatch
        do_PUT=dispatch
        do_DELETE=dispatch
        do_OPTIONS=dispatch
        do_HEAD=dispatch

    class BoundedServer(ThreadingHTTPServer):
        daemon_threads=True
        request_queue_size=16
        def __init__(self,*args,**kwargs):
            self.slots=threading.BoundedSemaphore(24)
            super().__init__(*args,**kwargs)
        def process_request(self,request,address):
            if not self.slots.acquire(blocking=False):
                request.close()
                return
            try:
                super().process_request(request,address)
            except BaseException:
                self.slots.release()
                raise
        def process_request_thread(self,*args):
            try:
                super().process_request_thread(*args)
            finally:
                self.slots.release()
    return BoundedServer((host,port),Handler)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8780)
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('LOCALAPPDATA',Path.home()))/'MonkeyPoseLive'/'web')
    parser.add_argument('--public-url')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.config:
        os.environ['MONKEYPOSE_CONFIG']=str(args.config.resolve())
    app=WebApplication(args.data_dir,args.public_url)
    server=make_server(app,args.host,args.port)
    url=f'http://127.0.0.1:{server.server_port}'
    descriptor=dict(pid=os.getpid(),url=url,public_url=app.public_url,
                    shutdown_url=url+'/_local/shutdown',shutdown_token=app.shutdown_token,
                    password_file=str(app.password_file),version=VERSION)
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(descriptor,indent=2),encoding='utf-8')
    print('MonkeyPoseLive Web '+VERSION+' listening on '+url,flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__=='__main__':
    main()
