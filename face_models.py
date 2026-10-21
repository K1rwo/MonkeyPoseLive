"""Optional face backends; imports remain stdlib-only until a worker loads one.

PrimateFace supplies face detection / 68 landmarks. Facial display categories
come from the separate monkey-identity-expression author's frozen CNN, executed
in its own TensorFlow interpreter. No model code or weights are redistributed.
"""
from __future__ import annotations
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import types

import runtime_config

FACE_PATH_FIELDS = ('python', 'repo', 'detector_config', 'detector_checkpoint',
    'landmark_config', 'landmark_checkpoint', 'expression_python',
    'expression_repo', 'expression_architecture', 'expression_checkpoint',
    'class_mapping_audit')
EXPRESSION_CLASSES = ['neutral', 'lipsmack', 'threat', 'fearful']
MEAN_BGR = (0.3310429315873352, 0.2596515072870283, 0.21203281118510672)
FACE68_NAMES = [f'face-{i}' for i in range(68)]
# Author analysis/constants.py uses the dlib68 regions. Join only within each
# anatomical region, including the four closed eye/mouth contours.
FACE68_EDGES = [[i, i + 1] for start, end in ((0,16),(17,21),(22,26),(27,30),
    (31,35),(36,41),(42,47),(48,59),(60,67)) for i in range(start,end)]
FACE68_EDGES += [[41,36],[47,42],[59,48],[67,60]]
PF_PROVENANCE = dict(upstream='https://github.com/KordingLab/PrimateFace',
    commit='184fa8c31f9ed824c3226e46e70b87b3efe97416', license='MIT',
    model_repository='fparodi/primateface-models',
    model_revision='4fade59531e3c11b77eacf26d741ef6746e09598')
EXPRESSION_PROVENANCE = dict(
    upstream='https://github.com/monkey-id-exp/monkey-identity-expression',
    commit='41be445d425ffe57fbc6714442150aad226c39b3',
    license='No explicit repository license; author code and weights remain external')
EXPECTED = {
    'detector_config': 'bb922eeb026c5e7a06524c71f7a61cb6eb227ba0a9f92de70bbbbda016f32885',
    'detector_checkpoint': '215e347181bfd504e5eeb6a62b425b5980fbae499ef102197eb380cf4f52d06b',
    'landmark_config': '1c2814b5e73666a979c42a7817c4119a1507c97d3e7067ad682510d6ea63976d',
    'landmark_checkpoint': '4a639cbc07973a1c6cd3792f5068fd53e187cf89b1b932dfb05b149a365c1d97',
    'expression_checkpoint': '70381000deaccc262c3e57e2134309b09a6a3c503c7b3727fca513146170bd1a',
    'expression_architecture': '7e6dd0e61fdcc6d2ce57d9a4dc48ce2924b732d6688e8a152059968152290fff',
}
FACE_MODELS = {}


def refresh_registry(config=None):
    config = config if config is not None else runtime_config.load_config(resolve_paths=True)
    records = {}
    for key, label in (
        ('primateface_landmarks', 'PrimateFace HRNetV2-W18 / facial landmarks (68)'),
        ('primateface_expression', 'Monkey facial-display CNN + PrimateFace detector (4 classes)')):
        expression = key == 'primateface_expression'
        record = deepcopy(config.get('face_models', {}).get(key, {}))
        for field in FACE_PATH_FIELDS:
            record.setdefault(field, '')
        required = ['python', 'repo', 'detector_config', 'detector_checkpoint']
        required += (['expression_python', 'expression_repo', 'expression_architecture',
                      'expression_checkpoint', 'class_mapping_audit'] if expression else
                     ['landmark_config', 'landmark_checkpoint'])
        missing = []
        for field in required:
            value = record[field]
            exists = Path(value).is_dir() if field in ('repo','expression_repo') else Path(value).is_file()
            if not value or not exists:
                missing.append(field + ': ' + ('path not found' if value else 'not configured'))
        errors = config.get('_errors', [])
        record.update(id=key, label=label, category='facial_expression' if expression else 'face_landmarks',
            task='facial_expression' if expression else 'face_landmarks',
            keypoint_names=[] if expression else FACE68_NAMES[:],
            display_indices=[] if expression else list(range(68)),
            edges=[] if expression else deepcopy(FACE68_EDGES),
            expression_classes=EXPRESSION_CLASSES[:] if expression else [],
            score_semantics=('Uncalibrated facial-display class softmax; not internal emotion probability' if expression else
                             'Raw maximum landmark heatmap score; not calibrated accuracy'),
            detector_supported=False, detector_ready=False,
            detector_reason='Uses its own PrimateFace Cascade R-CNN face detector within the selected ROI',
            device_supported=['cuda','cpu'], native_face_detector=True,
            provenance=deepcopy(EXPRESSION_PROVENANCE if expression else PF_PROVENANCE),
            face_detector_provenance=deepcopy(PF_PROVENANCE),
            configuration_error='; '.join(errors) or None, missing=missing, missing_config=missing[:],
            ready=not missing and not errors, available=not missing and not errors,
            caveat='Facial-display predictions are exploratory; no internal affect or anatomical accuracy validation.')
        records[key] = record
    FACE_MODELS.clear()
    FACE_MODELS.update(records)
    return FACE_MODELS


refresh_registry()


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def verify_file(record, field):
    actual = sha256(record[field])
    expected = record.get(field + '_sha256') or EXPECTED.get(field)
    if expected and actual.lower() != expected.lower():
        raise ValueError('Configured face model hash mismatch: ' + field)
    return actual


def verify_mapping(path, source):
    """Require the prior audit and verify the author's original mapping evidence."""
    audit = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if (audit.get('mapping_verified') is not True or
            not isinstance(audit.get('matched_rows'), int) or
            isinstance(audit.get('matched_rows'), bool) or audit['matched_rows'] <= 0 or
            audit['matched_rows'] != audit.get('xls_rows')):
        raise ValueError('Facial-display class mapping is not verified')
    names = [audit.get('mapping', {}).get(str(i)) for i in range(4)]
    if names != EXPRESSION_CLASSES:
        raise ValueError('Facial-display class mapping differs from the verified author order')
    for name in ('train_id_exp.xls', 'exponehot.npy'):
        expected = audit.get('sha256', {}).get(name)
        if not expected or sha256(Path(source)/name) != expected:
            raise ValueError('Facial-display mapping evidence changed: ' + name)
    return names


def expression_payload(values):
    """Validate softmax values before exposing any predicted display category."""
    if (not isinstance(values, (list,tuple)) or len(values) != 4 or
            any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v)
                or v < 0 or v > 1 for v in values) or abs(sum(values)-1) >= 1e-4):
        raise ValueError('Invalid four-class facial-display scores')
    k = max(range(4), key=lambda i: values[i])
    return dict(available=True, label=EXPRESSION_CLASSES[k], score=float(values[k]),
        probabilities=[dict(label=n,score=float(p)) for n,p in zip(EXPRESSION_CLASSES,values)],
        score_semantics='Uncalibrated facial-display class softmax; not internal emotion probability',
        internal_affect_validated=False)


def install_inference_ops_bridge():
    """Windows mmcv-lite bridge, matching the already verified local pipeline."""
    import torch
    import torchvision.ops as tvops
    class Ext(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith('__'):
                raise AttributeError(name)
            def unsupported(*args, **kwargs):
                raise NotImplementedError('Unsupported MMCV compiled op: ' + name)
            return unsupported
    ext = Ext('mmcv._ext')
    ext.__file__ = __file__
    ext.__spec__ = importlib.util.spec_from_loader('mmcv._ext', loader=None)
    def nms(boxes, scores, iou_threshold, offset=0):
        if offset:
            boxes = boxes.clone()
            boxes[:, 2:4] += offset
        return tvops.nms(boxes,scores,iou_threshold)
    def roi_align_forward(inp,rois,out,arg_y,arg_x,aligned_height,aligned_width,
                          spatial_scale,sampling_ratio,pool_mode,aligned):
        if pool_mode != 1:
            raise NotImplementedError('Only average RoIAlign is supported')
        out.copy_(tvops.roi_align(inp,rois,(aligned_height,aligned_width),
                                  spatial_scale,sampling_ratio,aligned))
    ext.nms, ext.roi_align_forward = nms, roi_align_forward
    sys.modules['mmcv._ext'] = ext
    original = torch.load
    def trusted_load(*args,**kwargs):
        kwargs.setdefault('weights_only',False)
        return original(*args,**kwargs)
    torch.load = trusted_load


class ExpressionProcess:
    """One owned TF process. Pixel payloads use stdin; no temporary face files."""
    def __init__(self, model_id, record):
        self.messages = queue.Queue(maxsize=4)
        env = dict(os.environ, MONKEYPOSE_CONFIG=str(runtime_config.config_path()),
            PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1',CUDA_VISIBLE_DEVICES='-1',
            TF_CPP_MIN_LOG_LEVEL='2',OMP_NUM_THREADS='4',TF_NUM_INTRAOP_THREADS='4',TF_NUM_INTEROP_THREADS='2')
        env.pop('PYTHONHOME',None)
        env.pop('PYTHONPATH',None)
        self.proc = subprocess.Popen([record['expression_python'],'-u',
            str(Path(__file__).with_name('face_worker.py')),'--expression-child','--model',model_id],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=sys.stderr,text=True,
            encoding='utf-8',errors='replace',bufsize=1,env=env,cwd=str(Path(__file__).parent),
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        threading.Thread(target=self._listen,daemon=True).start()
        try:
            self.provenance = self.receive('RT_READY',180)
        except BaseException:
            self.close()
            raise

    def _listen(self):
        try:
            for line in self.proc.stdout:
                if line.startswith(('RT_READY ','RT_RESULT ')):
                    marker,body = line.split(' ',1)
                    self.messages.put((marker,json.loads(body)),timeout=2)
        except Exception:
            pass
        finally:
            try:
                self.messages.put(('EOF',{}),timeout=1)
            except queue.Full:
                pass

    def receive(self, marker, timeout):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            try:
                tag,data = self.messages.get(timeout=min(1,max(.01,deadline-time.monotonic())))
            except queue.Empty:
                continue
            if tag == 'EOF' or data.get('error') or data.get('status') == 'error':
                raise RuntimeError('Facial-display worker failed; inspect private host diagnostics')
            if tag == marker:
                return data
        raise TimeoutError('Facial-display worker response timed out')

    def predict(self, face, cv2):
        ok,png = cv2.imencode('.png',face)
        if not ok:
            raise ValueError('Cannot encode the detected face')
        self.proc.stdin.write(json.dumps({'face_png':base64.b64encode(png.tobytes()).decode('ascii')})+'\n')
        self.proc.stdin.flush()
        return expression_payload(self.receive('RT_RESULT',60)['probabilities'])

    def close(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write('{"cmd":"stop"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=3)
            except (OSError,ValueError,subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait(timeout=5)


class FaceAdapter:
    def __init__(self, model_id, device):
        import numpy as np
        import torch
        self.record = FACE_MODELS[model_id]
        if not self.record['ready']:
            raise ValueError('Face model is not configured')
        self.model_id,self.np,self.torch = model_id,np,torch
        self.expression = None
        self.device = 'cuda:0' if device in ('cuda','auto') and torch.cuda.is_available() else 'cpu'
        torch.set_num_threads(2)
        fields = ['detector_config','detector_checkpoint']
        if model_id == 'primateface_landmarks':
            fields += ['landmark_config','landmark_checkpoint']
        hashes = {f:verify_file(self.record,f) for f in fields}
        install_inference_ops_bridge()
        from mmdet.apis import init_detector,inference_detector
        from mmpose.utils import adapt_mmdet_pipeline
        self.detector = init_detector(self.record['detector_config'],
            self.record['detector_checkpoint'],device=self.device)
        self.detector.cfg = adapt_mmdet_pipeline(self.detector.cfg)
        self.detect = inference_detector
        self.pose = None
        if model_id == 'primateface_landmarks':
            from mmpose.apis import init_model,inference_topdown
            self.pose = init_model(self.record['landmark_config'],
                self.record['landmark_checkpoint'],device=self.device)
            self.landmarks = inference_topdown
            self.names = [self.pose.dataset_meta['keypoint_id2name'][i] for i in range(68)]
        else:
            self.names = []
            self.expression = ExpressionProcess(model_id,self.record)
        self.provenance = dict(model=model_id,device=self.device,upstream=PF_PROVENANCE,
            verified_model_sha256=hashes,training=False,target_labels_used=False,
            face_selection='highest confidence class0 face within user ROI',face_threshold=.5,
            inference_ops_bridge='torchvision compiled NMS and average RoIAlign, same semantics',
            landmark_count=68 if self.pose is not None else 0,
            expression=self.expression.provenance if self.expression else None)

    def predict(self, image):
        np,torch = self.np,self.torch
        with torch.inference_mode():
            sample = self.detect(self.detector,image)
            detection = sample.pred_instances.to('cpu').numpy()
            valid = np.where((detection.scores >= .5) & (detection.labels == 0))[0]
            candidates = []
            h,w = image.shape[:2]
            for index in valid:
                box = detection.bboxes[index].astype(float).tolist()
                score = float(detection.scores[index])
                if not all(math.isfinite(v) for v in box+[score]):
                    continue
                box = [max(0.,box[0]),max(0.,box[1]),min(float(w),box[2]),min(float(h),box[3])]
                if box[2]-box[0] >= 2 and box[3]-box[1] >= 2:
                    candidates.append((score,box))
            if not candidates:
                return dict(status='no_face',points=[],bbox_xyxy=None,detector_score=None,
                    expression=dict(available=False,label=None,probabilities=[],reason='no_face'))
            score,box = max(candidates,key=lambda v:v[0])
            result = dict(status='ok',bbox_xyxy=box,detector_score=score,points=[],face_count=len(candidates))
            if self.pose is not None:
                pose = self.landmarks(self.pose,image,np.asarray([box],dtype=np.float32),bbox_format='xyxy')[0]
                data = pose.pred_instances.to('cpu').numpy()
                points = np.concatenate([data.keypoints[0],data.keypoint_scores[0,:,None]],axis=1)
                if points.shape != (68,3) or not np.isfinite(points).all():
                    raise ValueError('Face model returned invalid 68-point coordinates')
                result['points'] = points.tolist()
            else:
                import cv2
                bw,bh = box[2]-box[0],box[3]-box[1]
                x1,y1 = max(0,math.floor(box[0]-.1*bw)),max(0,math.floor(box[1]-.1*bh))
                x2,y2 = min(w,math.ceil(box[2]+.1*bw)),min(h,math.ceil(box[3]+.1*bh))
                result['expression'] = self.expression.predict(image[y1:y2,x1:x2],cv2)
                result['face_crop_xyxy'] = [x1,y1,x2,y2]
            return result

    def close(self):
        if self.expression:
            self.expression.close()


def create_adapter(model_id, device):
    return FaceAdapter(model_id,device)
