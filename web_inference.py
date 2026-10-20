"""Private network-job inference using the existing external model adapters.

No camera is opened on the host. Video jobs process every decoded frame; browser
jobs accept one submitted JPEG at a time. All filesystem inputs are server-owned.
"""
from __future__ import annotations
import base64
import csv
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from tracking import Trajectories

HERE = Path(__file__).resolve().parent


def jpeg_dimensions(data):
    """Inspect JPEG SOF before native decoding, bounding decoded allocation."""
    if not data.startswith(b'\xff\xd8'):
        raise ValueError('需要JPEG图片')
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 255:
            raise ValueError('JPEG头损坏')
        while pos < len(data) and data[pos] == 255:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in (0xd9, 0xda):
            break
        if marker in (0x01, *range(0xd0, 0xd9)):
            continue
        if pos + 2 > len(data):
            break
        size = int.from_bytes(data[pos:pos+2], 'big')
        if size < 2 or pos + size > len(data):
            break
        if marker in (0xc0, 0xc1, 0xc2):
            if size < 8:
                break
            height = int.from_bytes(data[pos+3:pos+5], 'big')
            width = int.from_bytes(data[pos+5:pos+7], 'big')
            if not 2 <= width <= 4096 or not 2 <= height <= 4096 or width*height > 8_000_000:
                raise ValueError('摄像头图片超过4096边长／8MP限制')
            return width, height
        pos += size
    raise ValueError('JPEG头无有效尺寸')


class ModelProcess:
    def __init__(self, job):
        from models import MODELS
        from runtime_config import config_path
        spec = MODELS[job.config['model']]
        self.job = job
        self.messages = queue.Queue(maxsize=8)
        self.log = (job.directory/'worker.stderr.log').open('wb')
        env = dict(os.environ, MONKEYPOSE_CONFIG=str(config_path()), PYTHONIOENCODING='utf-8',
                   PYTHONUNBUFFERED='1', OMP_NUM_THREADS='4', MKL_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4',
                   TORCH_CUDNN_V8_API_LRU_CACHE_LIMIT='128', DNNL_PRIMITIVE_CACHE_CAPACITY='64')
        env.pop('PYTHONHOME', None)
        env.pop('PYTHONPATH', None)
        try:
            self.proc = subprocess.Popen([str(spec['python']), '-u', str(HERE/'worker.py'),
                '--model', job.config['model'], '--device', 'cpu' if job.config['model']=='dlc' else 'cuda'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True,
                encoding='utf-8', errors='replace', bufsize=1, env=env, cwd=str(HERE),
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except BaseException:
            self.log.close()
            raise
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        try:
            for line in self.proc.stdout:
                if line.startswith(('RT_READY ', 'RT_RESULT ')):
                    marker, payload = line.split(' ', 1)
                    self.messages.put((marker, json.loads(payload)), timeout=3)
                # Do not persist per-frame model coordinates in diagnostic logs.
        except Exception:
            pass
        finally:
            try:
                self.messages.put(('EOF', {}), timeout=1)
            except queue.Full:
                pass

    def receive(self, marker, timeout):
        deadline = time.monotonic()+timeout
        while not self.job.stop_event.is_set():
            if time.monotonic() > deadline:
                raise RuntimeError('模型响应超时')
            try:
                tag, data = self.messages.get(timeout=.2)
            except queue.Empty:
                continue
            if tag == 'EOF' or data.get('error') or data.get('status') == 'error':
                raise RuntimeError('模型进程出错；请由主机管理员检查本机日志')
            if tag == marker:
                return data
        raise InterruptedError('已停止')

    def predict(self, jpeg, sequence, timestamp):
        path = self.job.directory/'ipc_frame.jpg'
        path.write_bytes(jpeg)
        self.proc.stdin.write(json.dumps(dict(image_path=str(path), seq=sequence,
            timestamp=timestamp, roi=self.job.config['roi'], detector=self.job.config['detector']))+'\n')
        self.proc.stdin.flush()
        return self.receive('RT_RESULT', 90)

    def close(self):
        try:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write('{"cmd":"stop"}\n')
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=3)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    if self.proc.poll() is None:
                        if os.name == 'nt':
                            subprocess.run(['taskkill','/PID',str(self.proc.pid),'/T','/F'],
                                capture_output=True, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        else:
                            self.proc.kill()
                        self.proc.wait(timeout=5)
        finally:
            self.log.close()
            (self.job.directory/'ipc_frame.jpg').unlink(missing_ok=True)


class InferenceEngine:
    def run(self, job):
        import cv2
        import numpy as np
        cv2.setNumThreads(1)
        worker = None
        cap = None
        video_writer = None
        prediction_file = None
        csv_file = None
        trails = Trajectories()
        previous_box = None
        processed = 0
        submission = None
        started = time.monotonic()
        try:
            fps = 15.0
            total = 0
            if job.kind == 'video':
                cap = cv2.VideoCapture(str(job.directory/'upload.bin'))
                if not cap.isOpened():
                    raise ValueError('无法解码上传的视频')
                fps = float(cap.get(cv2.CAP_PROP_FPS))
                if not math.isfinite(fps) or not 0 < fps <= 240:
                    fps = 30.0
                total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                if total <= 0 or total > 18000 or total/fps > 600:
                    raise ValueError('视频须在10分钟／18000帧以内')
                vw, vh = cap.get(cv2.CAP_PROP_FRAME_WIDTH),cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
                if not (2<=vw<=4096 and 2<=vh<=4096 and vw*vh<=8_000_000):
                    raise ValueError('视频尺寸超过4096边长／8MP限制')
            job.update(status='loading_model')
            worker = ModelProcess(job)
            readiness = worker.receive('RT_READY', 300)
            # Keep upstream versions/checkpoint verification on the host. This
            # metadata can contain private interpreter/checkpoint paths and is
            # deliberately excluded from the network download allowlist.
            (job.directory/'model_provenance.json').write_text(
                json.dumps(readiness,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
            if job.config['record']:
                prediction_file = (job.directory/'predictions.jsonl').open('x', encoding='utf-8')
                csv_file = (job.directory/'trajectory.csv').open('x', encoding='utf-8-sig', newline='')
                csv_writer = csv.writer(csv_file)
                csv_writer.writerow(['sequence','timestamp_seconds','joint_index','joint_name','x_pixels','y_pixels',
                                     'raw_score','display_valid','status','coordinate_system'])
            job.update(status='running', total_frames=total)
            last_camera = time.monotonic()
            last_timestamp = -1.0
            while not job.stop_event.is_set():
                submission = None
                if time.monotonic()-started > 3600:
                    raise ValueError('单次任务超过1小时限制')
                if job.kind == 'video':
                    ok, image = cap.read()
                    if not ok:
                        if processed < total:
                            raise ValueError('视频提前结束或无法完整解码')
                        break
                    if processed>=18000 or processed/fps>=600:
                        raise ValueError('实际视频超过10分钟／18000帧限制')
                    seq, stamp = processed+1, processed/fps
                else:
                    try:
                        submission = job.frames.get(timeout=.3)
                    except queue.Empty:
                        if time.monotonic()-last_camera > 120:
                            job.update(note='摄像头超过120秒未发送画面，已释放模型')
                            break
                        continue
                    last_camera = time.monotonic()
                    seq, stamp, payload, future = submission
                    if stamp <= last_timestamp:
                        future.set_exception(ValueError('摄像头时间戳须递增'))
                        continue
                    last_timestamp = stamp
                    image = cv2.imdecode(np.frombuffer(payload,dtype=np.uint8),cv2.IMREAD_COLOR)
                    if image is None:
                        future.set_exception(ValueError('无法解码JPEG'))
                        continue
                h0, w0 = image.shape[:2]
                scale = min(1., 1280/max(w0,h0))
                if scale < 1:
                    image = cv2.resize(image, (max(2,round(w0*scale)),max(2,round(h0*scale))))
                h, w = image.shape[:2]
                ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY,95])
                if not ok:
                    raise RuntimeError('输入图片编码失败')
                result = worker.predict(encoded.tobytes(), seq, stamp)
                result.update(source_width=w0,source_height=h0,coordinate_width=w,coordinate_height=h,
                              sequence=seq,coordinate_system='resized_frame_pixels',training=False,
                              reference_accuracy_validated=False)
                # Avoid joining trajectories across disjoint target-box jumps.
                box = result.get('bbox_xyxy')
                if previous_box and box:
                    ix=max(0,min(previous_box[2],box[2])-max(previous_box[0],box[0]))
                    iy=max(0,min(previous_box[3],box[3])-max(previous_box[1],box[1]))
                    distance=math.hypot((box[0]+box[2]-previous_box[0]-previous_box[2])/2,
                                        (box[1]+box[3]-previous_box[1]-previous_box[3])/2)
                    if ix*iy==0 and distance>.25*max(w,h):
                        trails.clear()
                        result['trajectory_reset_reason']='large_disjoint_target_box_jump; identity unverified'
                previous_box = box
                sample = trails.add(result,w,h,job.config['threshold'])
                result['display_valid'] = sample['valid']
                points = result.get('points',[])
                for j,p in enumerate(points):
                    for a,b in trails.segments(j):
                        cv2.line(image,tuple(map(round,a)),tuple(map(round,b)),(255,180,70),1,cv2.LINE_AA)
                for a,b in result.get('edges',[]):
                    if a<len(sample['valid']) and b<len(sample['valid']) and sample['valid'][a] and sample['valid'][b]:
                        cv2.line(image,tuple(map(round,points[a][:2])),tuple(map(round,points[b][:2])),(50,220,250),2)
                for i,p in enumerate(points):
                    if sample['valid'][i]:
                        cv2.circle(image,(round(p[0]),round(p[1])),3,(70,235,100),-1)
                cv2.putText(image,f'{job.config["model"]} | frame {seq} | {result.get("status")}',
                            (12,24),cv2.FONT_HERSHEY_SIMPLEX,.5,(245,245,245),1)
                ok, preview = cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,85])
                if not ok:
                    raise RuntimeError('结果图片编码失败')
                if job.config['record']:
                    prediction_file.write(json.dumps(result,ensure_ascii=False,allow_nan=False)+'\n')
                    prediction_file.flush()
                    names=result.get('keypoint_names',[])
                    for i,p in enumerate(points):
                        csv_writer.writerow([seq,stamp,i,names[i] if i<len(names) else str(i),*p,sample['valid'][i],
                                             result.get('status'),'resized_frame_pixels'])
                    csv_file.flush()
                    if job.kind=='video':
                        if video_writer is None:
                            video_writer=cv2.VideoWriter(str(job.directory/'annotated.mp4'),
                                cv2.VideoWriter_fourcc(*'mp4v'),fps,(w,h))
                            if not video_writer.isOpened():
                                raise RuntimeError('无法写入结果视频')
                        video_writer.write(image)
                processed += 1
                safe_result={k:result.get(k) for k in ('sequence','timestamp','points','display_valid','edges',
                    'keypoint_names','score_semantics','object_score','inference_ms','status','coordinate_system',
                    'coordinate_width','coordinate_height','source_width','source_height')}
                safe_result.update(width=w,height=h,joint_names=result.get('keypoint_names',[]),
                                   trajectories=list(trails.samples))
                with job.lock:
                    job.preview=preview.tobytes()
                    job.result=safe_result
                job.update(processed=processed,sequence=seq,progress=processed/total if total else None,
                           width=w,height=h,inference_fps=processed/max(.001,time.monotonic()-started))
                if submission and not future.done():
                    response=dict(safe_result,jpeg=base64.b64encode(preview).decode('ascii'))
                    future.set_result(response)
                if job.storage_usage() > job.storage_limit:
                    raise ValueError('任务达到本机存储配额，请管理员归档后再试')
        finally:
            if submission:
                current_future=submission[-1]
                if not current_future.done():
                    current_future.set_exception(InterruptedError('推理已停止，请检查任务状态'))
            if worker:
                worker.close()
            if cap:
                cap.release()
            if video_writer:
                video_writer.release()
            if prediction_file:
                prediction_file.close()
            if csv_file:
                csv_file.close()
            while not job.frames.empty():
                try:
                    *_, future=job.frames.get_nowait()
                    if not future.done():
                        future.set_exception(InterruptedError('已停止'))
                except queue.Empty:
                    break
