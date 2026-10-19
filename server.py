"""Loopback USB/video live pose viewer. Exactly one persistent model worker."""
from __future__ import annotations
import argparse
from collections import deque
import csv
from datetime import datetime,timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlparse

from tracking import LatestSlot,Trajectories

HERE=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
from runtime_config import default_output_directory
DEFAULT_OUTPUT=default_output_directory()
CREATE_NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)
APP_ID='monkey_pose_live'
APP_VERSION='0.1.0'


def model_summary():
    from models import MODELS
    fields=('id','label','score_semantics','ready','missing','detector_supported',
            'detector_reason','device_supported','upstream','license','caveat')
    return {key:{name:value.get(name) for name in fields} for key,value in MODELS.items()}


def setup_summary():
    from runtime_config import config_path,load_config
    from models import YOLO_WEIGHTS
    error=None
    try:
        loaded=load_config()
        error=loaded.get('config_error')
        configuration={k:v for k,v in loaded.items() if not k.startswith('_') and k!='config_error'}
    except (ValueError,OSError) as ex:
        configuration={'schema_version':1,'models':{},'detector':{}}
        error=str(ex)
    return dict(config_path=str(config_path()),output_directory=str(default_output_directory()),
                configuration=configuration,configuration_error=error,models=model_summary(),
                detector_ready=bool(YOLO_WEIGHTS and Path(YOLO_WEIGHTS).is_file()))


class LiveSession:
    def __init__(self,output):
        self.output=output
        self.lock=threading.RLock()
        self.image_changed=threading.Condition(self.lock)
        self.state={'status':'idle','error':None,'frames_captured':0,'frames_processed':0,'frames_dropped':0,
                    'capture_fps':0,'inference_fps':0,'latency_ms':None,'keypoint_names':[]}
        self.jpeg=None
        self.image_version=0
        self.thread=None
        self.capture_thread=None
        self.proc=None
        self.stop_event=threading.Event()
        self.trails=Trajectories()
        self.config={}
        self.tracking_epoch=0
    def snapshot(self):
        with self.lock:
            return dict(self.state,app_id=APP_ID,app_version=APP_VERSION,
                        configuration=dict(self.config),configuration_epoch=self.tracking_epoch)
    def trajectory(self):
        with self.lock:
            return dict(keypoint_names=self.state.get('keypoint_names',[]),samples=list(self.trails.samples),
                        max_gap_seconds=self.trails.max_gap,epoch=self.tracking_epoch)
    def configure(self,c):
        with self.lock:
            if 'roi' in c:
                roi=list(map(float,c['roi']))
                if len(roi)!=4 or not(0<=roi[0]<roi[2]<=1 and 0<=roi[1]<roi[3]<=1):
                    raise ValueError('ROI必须为0–1范围内的左上、右下坐标')
                if roi!=self.config.get('roi'):
                    self.trails.clear()
                    self.tracking_epoch+=1
                self.config['roi']=roi
            for key,lo,hi in [('threshold',0,1),('trail_seconds',.1,30)]:
                if key in c:
                    value=float(c[key])
                    if not math.isfinite(value):
                        raise ValueError('显示参数必须为有限数值')
                    value=max(lo,min(hi,value))
                    if key=='threshold' and value!=self.config.get(key):
                        self.trails.clear()
                        self.tracking_epoch+=1
                    self.config[key]=value
            if 'detector' in c:
                if bool(c['detector'])!=self.config.get('detector'):
                    self.trails.clear()
                    self.tracking_epoch+=1
                self.config['detector']=bool(c['detector'])
            self.trails.seconds=self.config.get('trail_seconds',3)
    def start(self,c):
        from models import MODELS
        with self.lock:
            if (self.thread and self.thread.is_alive()) or (self.capture_thread and self.capture_thread.is_alive()):
                raise ValueError('请先停止当前运行，再切换输入或模型')
            model=c.get('model','mmpose')
            if model not in MODELS:
                raise ValueError('未知模型')
            if not MODELS[model].get('ready',False):
                raise ValueError('模型尚未配置完成，请打开“模型与路径设置”：'+
                                 '；'.join(MODELS[model].get('missing',[])))
            source_type=c.get('source_type','usb')
            source=str(c.get('source','0'))
            if source_type=='usb':
                if not source.isdigit() or int(source)>20:
                    raise ValueError('USB摄像头编号应为0–20')
            elif source_type=='file':
                path=Path(source).resolve()
                if not path.is_file() or path.suffix.lower() not in {'.mp4','.avi','.mov','.mkv','.m4v'}:
                    raise ValueError('本地视频路径不存在或格式不受支持')
                source=str(path)
            else:
                raise ValueError('此版本支持USB摄像头和本地视频')
            device=c.get('device','cuda')
            if device not in {'cuda','cpu','auto'}:
                raise ValueError('设备仅支持cuda、cpu、auto')
            backend=c.get('backend','auto')
            if backend not in {'auto','dshow','msmf'}:
                raise ValueError('未知摄像头后端')
            if c.get('detector',True) and not MODELS[model].get('detector_supported',True):
                raise ValueError('旧DLC使用CPU与手动ROI；请关闭YOLO自动定位')
            if c.get('detector',True):
                from models import YOLO_WEIGHTS
                if not YOLO_WEIGHTS or not Path(YOLO_WEIGHTS).is_file():
                    raise ValueError('YOLO权重尚未配置，请设置detector.checkpoint，或关闭YOLO使用手动ROI')
            if model=='dlc' and device!='cpu':
                raise ValueError('旧DLC后端仅支持CPU，请选择CPU')
            self.config={'model':model,'source_type':source_type,'source':source,'device':device,'backend':backend,
                         'detector':bool(c.get('detector',True)),'roi':[0.,0.,1.,1.],
                         'threshold':.2,'trail_seconds':3.,'record':bool(c.get('record',False))}
            self.trails=Trajectories()
            self.tracking_epoch+=1
            self.configure(c)
            self.stop_event=threading.Event()
            self.jpeg=None
            self.state=dict(status='loading_model',error=None,model=model,source_type=source_type,
                frames_captured=0,frames_processed=0,frames_dropped=0,capture_fps=0,inference_fps=0,
                latency_ms=None,keypoint_names=[],frame_width=0,frame_height=0,
                latency_definition='本机读取完成至推理结果，不含摄像头曝光／驱动缓冲延迟',record=self.config['record'])
            self.thread=threading.Thread(target=self.run_guarded,daemon=True)
            self.thread.start()
            return dict(self.state)
    def stop(self):
        self.stop_event.set()
        proc=self.proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(json.dumps({'cmd':'stop'})+'\n')
                proc.stdin.flush()
            except (OSError,ValueError):
                pass
        thread=self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        if proc and proc.poll() is None:
            # Only the process tree spawned for this session; never user apps.
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,creationflags=CREATE_NO_WINDOW)
            else:
                proc.kill()
        if thread and thread.is_alive():
            thread.join(timeout=3)
        with self.lock:
            if self.capture_thread and self.capture_thread.is_alive():
                self.state['status']='stopping_capture'
            elif not thread or not thread.is_alive():
                self.state['status']='stopped'
            self.image_changed.notify_all()
        return self.snapshot()
    def run_guarded(self):
        try:
            self.run()
        except Exception as ex:
            with self.lock:
                self.state.update(status='error',error=f'{type(ex).__name__}: {ex}')
    def run(self):
        import cv2
        import numpy as np
        from models import MODELS
        cv2.setNumThreads(1)
        stop_event=self.stop_event
        spec=MODELS[self.config['model']]
        session_dir=self.output/(datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+str(time.time_ns())[-7:])
        session_dir.mkdir(parents=True,exist_ok=False)
        cfg=dict(self.config)
        (session_dir/'session.json').write_text(json.dumps(dict(config=cfg,model=spec,started_utc=datetime.now(timezone.utc).isoformat(),
            training=False,raw_video_recording=False,raw_scores_not_accuracy=True),ensure_ascii=False,indent=2),encoding='utf-8')
        with self.lock:
            self.state['run_directory']=str(session_dir)
        slot=LatestSlot()
        ready=threading.Event()
        result_ready=threading.Condition()
        pending={}
        failure=[]
        self.proc=None
        prediction_file=None
        csv_file=None
        cap_thread=None
        try:
            env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1',OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',
                     OPENBLAS_NUM_THREADS='4',TORCH_CUDNN_V8_API_LRU_CACHE_LIMIT='128',DNNL_PRIMITIVE_CACHE_CAPACITY='64',
                     ONEDNN_PRIMITIVE_CACHE_CAPACITY='64',LRU_CACHE_CAPACITY='64')
            from runtime_config import config_path
            env['MONKEYPOSE_CONFIG']=str(config_path())
            # External ML interpreters must load their own native libraries.
            if getattr(sys,'frozen',False):
                env.pop('PYTHONHOME',None)
                env.pop('PYTHONPATH',None)
                env['PYINSTALLER_RESET_ENVIRONMENT']='1'
                # cv2 may append bundle DLL directories to PATH at import.
                bundle_root=os.path.normcase(os.path.abspath(HERE))
                env['PATH']=os.pathsep.join(entry for entry in env.get('PATH','').split(os.pathsep)
                    if not os.path.normcase(os.path.abspath(entry)).startswith(bundle_root+os.sep)
                    and os.path.normcase(os.path.abspath(entry))!=bundle_root)
            stderr=(session_dir/'worker.stderr.log').open('wb')
            worker_directory=HERE/'backend' if getattr(sys,'frozen',False) else HERE
            cmd=[str(spec['python']),'-u',str(worker_directory/'worker.py'),'--model',cfg['model'],'--device',cfg['device']]
            (session_dir/'worker_command.json').write_text(json.dumps(cmd,ensure_ascii=False),encoding='utf-8')
            frozen_windows=os.name=='nt' and getattr(sys,'frozen',False)
            if frozen_windows:
                import ctypes
                ctypes.windll.kernel32.SetDllDirectoryW(None)
            try:
                self.proc=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,text=True,
                    encoding='utf-8',errors='replace',bufsize=1,env=env,creationflags=CREATE_NO_WINDOW,cwd=str(worker_directory))
            finally:
                if frozen_windows:
                    ctypes.windll.kernel32.SetDllDirectoryW(str(HERE))
            proc=self.proc
            def listen():
                with (session_dir/'worker.stdout.log').open('w',encoding='utf-8') as log:
                    for line in proc.stdout:
                        # Per-frame coordinates are persisted only when the
                        # user enables recording, via predictions.jsonl below.
                        if not line.startswith('RT_RESULT '):
                            log.write(line)
                            log.flush()
                        try:
                            if line.startswith('RT_READY '):
                                info=json.loads(line[9:])
                                if info.get('status')=='error' or info.get('error'):
                                    failure.append(info.get('error','模型初始化失败'))
                                with self.lock:
                                    if not stop_event.is_set():
                                        self.state.update({k:v for k,v in info.items() if k not in {'status'}})
                                ready.set()
                            elif line.startswith('RT_RESULT '):
                                result=json.loads(line[10:])
                                with result_ready:
                                    pending[result['seq']]=result
                                    result_ready.notify_all()
                        except (ValueError,KeyError,TypeError) as ex:
                            failure.append('模型协议错误：'+str(ex))
                    ready.set()
                    with result_ready:
                        result_ready.notify_all()
            threading.Thread(target=listen,daemon=True).start()
            deadline=time.monotonic()+240
            while not ready.wait(.2):
                if stop_event.is_set():
                    return
                if time.monotonic()>deadline:
                    raise RuntimeError('模型加载超时，请查看worker.stderr.log与内存／显存状态')
            if failure or proc.poll() is not None:
                raise RuntimeError('; '.join(failure) or '模型进程退出，请查看运行目录日志')
            if stop_event.is_set():
                return
            def capture():
                cap=None
                try:
                    backend=cv2.CAP_DSHOW if cfg['backend'] in {'auto','dshow'} and os.name=='nt' else cv2.CAP_MSMF if cfg['backend']=='msmf' else cv2.CAP_ANY
                    cap=cv2.VideoCapture(int(cfg['source']),backend) if cfg['source_type']=='usb' else cv2.VideoCapture(cfg['source'])
                    if not cap.isOpened():
                        raise RuntimeError('无法打开USB摄像头，请检查编号／占用和驱动' if cfg['source_type']=='usb' else '无法读取视频')
                    if cfg['source_type']=='usb':
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH,640)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT,480)
                        cap.set(cv2.CAP_PROP_FPS,30)
                        cap.set(cv2.CAP_PROP_BUFFERSIZE,1)
                    fps=float(cap.get(cv2.CAP_PROP_FPS))
                    if not math.isfinite(fps) or not 0<fps<=240:
                        fps=30.
                    start=time.monotonic()
                    times=deque(maxlen=90)
                    seq=0
                    while not stop_event.is_set():
                        if cfg['source_type']=='file':
                            stop_event.wait(max(0,start+seq/fps-time.monotonic()))
                        if stop_event.is_set():
                            break
                        ok,img=cap.read()
                        if not ok:
                            if cfg['source_type']=='usb':
                                raise RuntimeError('摄像头读取中断')
                            break
                        original_size=(img.shape[1],img.shape[0])
                        if img.shape[1]>1280:
                            img=cv2.resize(img,(1280,round(img.shape[0]*1280/img.shape[1])))
                        now=time.monotonic()
                        times.append(now)
                        seq+=1
                        slot.publish((seq,now,img,original_size))
                        with self.lock:
                            self.state.update(frames_captured=seq,frame_width=img.shape[1],frame_height=img.shape[0],
                                source_frame_width=original_size[0],source_frame_height=original_size[1],
                                capture_fps=(len(times)-1)/(times[-1]-times[0]) if len(times)>1 and times[-1]>times[0] else 0)
                except Exception as ex:
                    failure.append(str(ex))
                finally:
                    if cap is not None:
                        cap.release()
                    slot.finish()
                    with self.lock:
                        if self.state.get('status')=='stopping_capture' and stop_event.is_set():
                            self.state['status']='stopped'
                            if self.state.get('error')=='等待摄像头驱动释放；释放前不能启动新会话':
                                self.state['error']=None
            cap_thread=threading.Thread(target=capture,daemon=True)
            self.capture_thread=cap_thread
            cap_thread.start()
            with self.lock:
                self.state['status']='running'
            if cfg['record']:
                prediction_file=(session_dir/'predictions.jsonl').open('x',encoding='utf-8')
                csv_file=(session_dir/'trajectory.csv').open('x',encoding='utf-8-sig',newline='')
                writer=csv.writer(csv_file)
                writer.writerow(['capture_seq','capture_monotonic_seconds','joint_index','joint_name','x_pixels','y_pixels','raw_score','display_valid','status'])
            last_seq=0
            previous_box=None
            processed_times=deque(maxlen=90)
            def persist(result,names,valid):
                if prediction_file:
                    prediction_file.write(json.dumps(result,ensure_ascii=False,allow_nan=False)+'\n')
                    prediction_file.flush()
                    for j,p in enumerate(result.get('points',[])):
                        writer.writerow([result['capture_seq'],result['timestamp'],j,names[j] if j<len(names) else str(j),*p,valid[j],result.get('status')])
                    csv_file.flush()
            while not stop_event.is_set():
                value=slot.after(last_seq)
                if value is None:
                    if slot.ended:
                        break
                    continue
                seq,stamp,img,original_size=value
                frame_path=session_dir/'latest_input.jpg'
                temp_path=session_dir/'latest_input.partial.jpg'
                ok,encoded=cv2.imencode('.jpg',img,[cv2.IMWRITE_JPEG_QUALITY,95])
                if not ok:
                    raise RuntimeError('无法编码最新输入帧')
                temp_path.write_bytes(encoded.tobytes())
                os.replace(temp_path,frame_path)
                with self.lock:
                    settings=dict(self.config)
                    request_epoch=self.tracking_epoch
                request=dict(image_path=str(frame_path),seq=seq,timestamp=stamp,roi=settings['roi'],detector=settings['detector'])
                proc.stdin.write(json.dumps(request)+'\n')
                proc.stdin.flush()
                limit=time.monotonic()+90
                with result_ready:
                    while seq not in pending and proc.poll() is None and not stop_event.is_set() and time.monotonic()<limit:
                        result_ready.wait(.2)
                    result=pending.pop(seq,None)
                if stop_event.is_set():
                    break
                if result is None:
                    raise RuntimeError('模型无响应，请查看日志；采集队列没有累积')
                result['timestamp']=stamp
                result['capture_seq']=seq
                result['result_utc']=datetime.now(timezone.utc).isoformat()
                result['request_configuration']=settings
                result['configuration_epoch']=request_epoch
                now=time.monotonic()
                latency=(now-stamp)*1000
                processed_times.append(now)
                h,w=img.shape[:2]
                result['source_image_size']=list(original_size)
                result['processing_image_size']=[w,h]
                result['processing_to_source_scale_xy']=[original_size[0]/w,original_size[1]/h]
                with self.lock:
                    if request_epoch!=self.tracking_epoch:
                        result['discarded_for_display']='configuration_changed_during_inference'
                        result['display_valid']=[False]*len(result.get('points',[]))
                        self.state['stale_results_discarded']=self.state.get('stale_results_discarded',0)+1
                        self.state['frames_processed']+=1
                        self.state['frames_dropped']+=max(0,seq-last_seq-1)
                        persist(result,result.get('keypoint_names',[]),result['display_valid'])
                        last_seq=seq
                        previous_box=None
                        continue
                    current_box=result.get('bbox_xyxy') if settings['detector'] and result.get('status')=='ok' else None
                    if previous_box and current_box:
                        pc=((previous_box[0]+previous_box[2])/2,(previous_box[1]+previous_box[3])/2)
                        cc=((current_box[0]+current_box[2])/2,(current_box[1]+current_box[3])/2)
                        ix=max(0,min(previous_box[2],current_box[2])-max(previous_box[0],current_box[0]))
                        iy=max(0,min(previous_box[3],current_box[3])-max(previous_box[1],current_box[1]))
                        if ix*iy==0 and math.hypot(cc[0]-pc[0],cc[1]-pc[1])>.25*max(w,h):
                            self.trails.clear()
                            self.tracking_epoch+=1
                            result['trajectory_reset_reason']='large_disjoint_target_box_jump; identity unverified'
                    previous_box=current_box
                    sample=self.trails.add(result,w,h,settings['threshold'])
                    names=result.get('keypoint_names',self.state.get('keypoint_names',[]))
                    for j in range(len(sample['keypoints_xy_score'])):
                        for a,b in self.trails.segments(j):
                            cv2.line(img,tuple(map(round,a)),tuple(map(round,b)),(255,180,70),1,cv2.LINE_AA)
                    pts=sample['keypoints_xy_score']
                    for a,b in result.get('edges',[]):
                        if a<len(sample['valid']) and b<len(sample['valid']) and sample['valid'][a] and sample['valid'][b]:
                            cv2.line(img,tuple(map(round,pts[a][:2])),tuple(map(round,pts[b][:2])),(50,220,250),2,cv2.LINE_AA)
                    for j,p in enumerate(pts):
                        if sample['valid'][j]:
                            cv2.circle(img,(round(p[0]),round(p[1])),3,(70,235,100),-1,cv2.LINE_AA)
                    roi=result.get('roi_pixels')
                    if roi:
                        cv2.rectangle(img,tuple(map(round,roi[:2])),tuple(map(round,roi[2:])),(255,220,70),1)
                    cv2.putText(img,f'{cfg["model"]} | {result.get("status")} | read-to-result {latency:.0f}ms',(12,24),cv2.FONT_HERSHEY_SIMPLEX,.55,(245,245,245),2,cv2.LINE_AA)
                    ok,out=cv2.imencode('.jpg',img,[cv2.IMWRITE_JPEG_QUALITY,88])
                    if not ok:
                        raise RuntimeError('无法编码显示画面')
                    self.jpeg=out.tobytes()
                    self.image_version+=1
                    self.state.update(status='running',last_result_status=result.get('status'),keypoint_names=names,
                        frames_processed=self.state['frames_processed']+1,
                        frames_dropped=self.state['frames_dropped']+max(0,seq-last_seq-1),
                        inference_fps=(len(processed_times)-1)/(processed_times[-1]-processed_times[0]) if len(processed_times)>1 and processed_times[-1]>processed_times[0] else 0,
                        latency_ms=latency,last_result_time=now,score_semantics=result.get('score_semantics'),
                        object_score=result.get('object_score'),last_error=result.get('error'),points=pts,
                        display_valid=sample['valid'],inference_ms=result.get('inference_ms'),
                        capture_to_display_note='轨迹只连接有效模型输出；缺失或超过1秒间隔断开，不插值补点')
                    self.image_changed.notify_all()
                result['display_valid']=sample['valid']
                result['read_to_result_ms']=latency
                persist(result,names,sample['valid'])
                last_seq=seq
            if failure:
                raise RuntimeError('; '.join(failure))
            with self.lock:
                self.state['status']='stopped' if stop_event.is_set() else 'source_ended'
        except Exception as ex:
            with self.lock:
                self.state.update(status='error',error=f'{type(ex).__name__}: {ex}')
        finally:
            with self.lock:
                if stop_event.is_set() and self.state.get('status') in {'loading_model','running'}:
                    self.state['status']='stopped'
            stop_event.set()
            slot.finish()
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.write(json.dumps({'cmd':'stop'})+'\n')
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=5)
                except (OSError,subprocess.TimeoutExpired):
                    if os.name=='nt' and self.proc.poll() is None:
                        subprocess.run(['taskkill','/PID',str(self.proc.pid),'/T','/F'],capture_output=True,creationflags=CREATE_NO_WINDOW)
                    elif self.proc.poll() is None:
                        self.proc.kill()
            if cap_thread:
                cap_thread.join(timeout=2)
                if cap_thread.is_alive():
                    with self.lock:
                        self.state.update(status='stopping_capture',error='等待摄像头驱动释放；释放前不能启动新会话')
            if prediction_file:
                prediction_file.close()
                csv_file.close()
            if 'stderr' in locals():
                stderr.close()
            with self.lock:
                snapshot=dict(self.state,configuration=dict(self.config),configuration_epoch=self.tracking_epoch)
                self.image_changed.notify_all()
            if not cfg['record']:
                for field in ['points','display_valid']:
                    snapshot.pop(field,None)
            # These are session-owned IPC buffers, never source research media.
            for name in ['latest_input.jpg','latest_input.partial.jpg']:
                try:
                    (session_dir/name).unlink(missing_ok=True)
                except OSError as ex:
                    snapshot['temporary_frame_cleanup_error']=str(ex)
            (session_dir/'session_end.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')


def handler_for(session):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def allowed(self):
            host=self.headers.get('Host','').split(':')[0]
            origin=self.headers.get('Origin')
            return host in {'127.0.0.1','localhost'} and (not origin or urlparse(origin).netloc==self.headers.get('Host'))
        def response(self,value,status=200):
            data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if not self.allowed():
                self.response({'error':'仅允许本机同源访问'},403)
                return
            route=urlparse(self.path).path
            if route=='/api/state':
                self.response(session.snapshot())
            elif route=='/api/trajectory':
                self.response(session.trajectory())
            elif route=='/api/models':
                self.response(model_summary())
            elif route=='/api/setup':
                self.response(setup_summary())
            elif route=='/stream.mjpg':
                self.send_response(200)
                self.send_header('Content-Type','multipart/x-mixed-replace; boundary=frame')
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                last=-1
                try:
                    while True:
                        with session.image_changed:
                            session.image_changed.wait_for(lambda:session.image_version!=last,timeout=2)
                            data=session.jpeg
                            last=session.image_version
                        if data:
                            self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data+b'\r\n')
                            self.wfile.flush()
                        time.sleep(.02)
                except (OSError,ConnectionError):
                    pass
            elif route=='/' or route=='/index.html':
                data=(HERE/'web/index.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.response({'error':'not found'},404)
        def do_POST(self):
            if not self.allowed():
                self.response({'error':'仅允许本机同源访问'},403)
                return
            try:
                n=int(self.headers.get('Content-Length',0))
                route=urlparse(self.path).path
                if n<0 or n>(131072 if route=='/api/setup' else 16384):
                    raise ValueError('请求过大')
                body=json.loads(self.rfile.read(n) or b'{}')
                if route=='/api/start':
                    result=session.start(body)
                elif route=='/api/stop':
                    result=session.stop()
                elif route=='/api/config':
                    session.configure(body)
                    result=session.snapshot()
                elif route=='/api/setup':
                    from runtime_config import save_config
                    from models import refresh_registry
                    with session.lock:
                        if ((session.thread and session.thread.is_alive()) or
                            (session.capture_thread and session.capture_thread.is_alive())):
                            self.response({'error':'请先停止当前运行并等待模型和输入释放，再更改后端设置'},409)
                            return
                        save_config(body)
                        refresh_registry()
                        session.output=default_output_directory()
                        result=setup_summary()
                else:
                    self.response({'error':'not found'},404)
                    return
                self.response(result)
            except (ValueError,TypeError,KeyError,OSError) as ex:
                self.response({'error':str(ex)},400)
    return Handler


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--port',type=int,default=8765)
    p.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    a=p.parse_args()
    session=LiveSession(a.output)
    server=ThreadingHTTPServer(('127.0.0.1',a.port),handler_for(session))
    server.daemon_threads=True
    print(f'USB monkey pose: http://127.0.0.1:{a.port}',flush=True)
    try:
        server.serve_forever()
    finally:
        session.stop()
        server.server_close()


if __name__=='__main__':
    main()
