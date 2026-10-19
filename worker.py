"""Persistent one-model local pose worker. No camera capture, training or trails.

stdin: {image_path, seq, timestamp, roi:[normalized x1,y1,x2,y2], detector:bool}
stdout: RT_READY <JSON> once; RT_RESULT <JSON> for every frame request.
Libraries may print other lines; the parent must parse only these markers.
"""
from __future__ import annotations
import argparse
import contextlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
import traceback

# Avoid occupying sys.modules['models']; X-Pose owns a package with that name.
_spec = importlib.util.spec_from_file_location('realtime_pose_adapters', Path(__file__).with_name('models.py'))
models = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = models
_spec.loader.exec_module(models)


def emit(marker, payload):
    sys.stdout.write(marker + ' ' + json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
    sys.stdout.flush()


def finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def roi_pixels(roi, width, height):
    if roi is None:
        roi = [0, 0, 1, 1]
    if not isinstance(roi, (list, tuple)) or len(roi) != 4:
        raise ValueError('roi must be normalized [x1,y1,x2,y2]')
    if not all(finite_number(x) and 0 <= x <= 1 for x in roi):
        raise ValueError('roi must contain finite normalized values in [0,1]')
    if roi[0] >= roi[2] or roi[1] >= roi[3]:
        raise ValueError('roi must have positive width and height')
    result = [math.floor(roi[0] * width), math.floor(roi[1] * height),
              math.ceil(roi[2] * width), math.ceil(roi[3] * height)]
    if result[2] - result[0] < 2 or result[3] - result[1] < 2:
        raise ValueError('roi is too small at the actual image resolution')
    return result


def padded_crop(box, bounds, padding=1.25):
    if len(box) != 4 or not all(math.isfinite(x) for x in box):
        raise ValueError('Invalid detector box')
    x1, y1, x2, y2 = box
    if x2 <= x1 or y2 <= y1:
        raise ValueError('Detector box has no area')
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    w, h = (x2 - x1) * padding, (y2 - y1) * padding
    return [max(bounds[0], math.floor(cx - w / 2)), max(bounds[1], math.floor(cy - h / 2)),
            min(bounds[2], math.ceil(cx + w / 2)), min(bounds[3], math.ceil(cy + h / 2))]


def fullframe_points(points, origin, expected=17):
    if not isinstance(points, (list, tuple)) or len(points) != expected:
        raise ValueError('Model did not return the native17x3 points')
    result = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 3 or not all(
                finite_number(x) for x in point):
            raise ValueError('Invalid/nonfinite model keypoint; no pose emitted')
        result.append([float(point[0] + origin[0]), float(point[1] + origin[1]), float(point[2])])
    return result


def frame_base(model_id, request, device=None):
    m = models.MODELS[model_id]
    seq, timestamp = request.get('seq'), request.get('timestamp')
    valid_seq = isinstance(seq, int) and not isinstance(seq, bool)
    valid_timestamp = finite_number(timestamp)
    return {'model': model_id, 'seq': seq if valid_seq else None, 'timestamp': timestamp if valid_timestamp else None,
            'points': [], 'keypoints_xy_score': [], 'keypoint_names': m['keypoint_names'], 'edges': m['edges'],
            'display_indices': m['display_indices'], 'score_semantics': m['score_semantics'],
            'status': 'error', 'error': None, 'object_score': None, 'roi_pixels': None,
            'crop_xyxy': None, 'bbox_xyxy': None, 'detector_score': None, 'inference_ms': 0.0,
            'coordinate_system': 'full_frame_pixels', 'device': device,
            'eye_channels_validated': False if model_id == 'macaque3dpose_2d' else None,
            'identity_independently_verified': False}


class Controller:
    def __init__(self, model_id, adapter):
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        self.model_id, self.adapter = model_id, adapter
        self.yolo = None
        self.cv2.setNumThreads(1)

    def detect(self, image, roi):
        load_ms = 0.0
        if self.yolo is None:
            before = time.perf_counter()
            if not Path(models.YOLO_WEIGHTS).is_file():
                raise FileNotFoundError('Verified local detector checkpoint not found')
            from ultralytics import YOLO
            self.yolo = YOLO(models.YOLO_WEIGHTS)
            load_ms = (time.perf_counter() - before) * 1000
        x1, y1, x2, y2 = roi
        prediction = self.yolo.predict(image[y1:y2, x1:x2], conf=.25, iou=.5,
                                       classes=[0], imgsz=640, device=models.DETECTOR_CONFIG.get('device') or 'cpu', verbose=False)[0]
        if prediction.boxes is None or not len(prediction.boxes):
            return None, None, load_ms
        boxes = prediction.boxes.xyxy.detach().cpu().tolist()
        scores = prediction.boxes.conf.detach().cpu().tolist()
        candidates = []
        for box, score in zip(boxes, scores):
            if not all(math.isfinite(v) for v in box + [score]):
                continue
            a, b, c, d = box
            box = [max(x1, a + x1), max(y1, b + y1), min(x2, c + x1), min(y2, d + y1)]
            if box[2] - box[0] >= 2 and box[3] - box[1] >= 2:
                candidates.append((float(score), box))
        if not candidates:
            return None, None, load_ms
        score, box = max(candidates, key=lambda item: item[0])
        return box, score, load_ms

    def process(self, request):
        start = time.perf_counter()
        result = frame_base(self.model_id, request, self.adapter.device)
        try:
            if not isinstance(request.get('seq'), int) or isinstance(request.get('seq'), bool):
                raise ValueError('seq must be an integer')
            timestamp = request.get('timestamp')
            if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool) or not math.isfinite(timestamp):
                raise ValueError('timestamp must be finite')
            detector = request.get('detector', False)
            if not isinstance(detector, bool):
                raise ValueError('detector must be boolean')
            if detector and not models.MODELS[self.model_id]['detector_supported']:
                raise ValueError(models.MODELS[self.model_id]['detector_reason'])
            path = Path(request['image_path'])
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('Encoded frame exceeds32MiB')
            payload = path.read_bytes()
            if len(payload) > 32 * 1024 * 1024:
                raise ValueError('Encoded frame exceeds32MiB')
            image = self.cv2.imdecode(self.np.frombuffer(payload, dtype=self.np.uint8), self.cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('Cannot decode submitted frame')
            height, width = image.shape[:2]
            if width * height > 16_000_000:
                raise ValueError('Frame exceeds16MP; choose a lower capture resolution')
            roi = roi_pixels(request.get('roi'), width, height)
            result.update(width=width, height=height, image_size=[width, height], roi_pixels=roi,
                          detector_enabled=detector, detector_device=(models.DETECTOR_CONFIG.get('device') or 'cpu') if detector else None,
                          bbox_source='highest_confidence_class0_within_user_roi' if detector else 'user_roi',
                          detector_ms=0.0, detector_load_ms=0.0)
            if detector:
                before = time.perf_counter()
                box, score, load_ms = self.detect(image, roi)
                result.update(detector_ms=(time.perf_counter() - before) * 1000, detector_load_ms=load_ms)
                if box is None:
                    result.update(status='no_target_bbox', reason='No local detector class0 box inside user ROI')
                    return result
                crop = padded_crop(box, roi, 1.25)
                result.update(detector_score=score, crop_padding=1.25)
            else:
                crop, box, score = roi[:], [float(v) for v in roi], 1.0
                result['crop_padding'] = 1.0
            x1, y1, x2, y2 = crop
            cut = image[y1:y2, x1:x2]
            if min(cut.shape[:2]) < 2:
                raise ValueError('Selected inference crop is empty/too small')
            local_box = [box[0] - x1, box[1] - y1, box[2] - x1, box[3] - y1]
            if not detector:
                local_box = [0.0, 0.0, float(cut.shape[1] - 1), float(cut.shape[0] - 1)]
            result.update(crop_xyxy=crop, bbox_xyxy=box, pose_bbox_xyxy_crop=local_box)
            self.adapter.sync()
            before = time.perf_counter()
            prediction = self.adapter.infer(cut, local_box, score)
            self.adapter.sync()
            result['inference_ms'] = (time.perf_counter() - before) * 1000
            points = fullframe_points(prediction['points'], [x1, y1])
            obj = prediction.get('object_score')
            if obj is not None and (not isinstance(obj, (int, float)) or not math.isfinite(obj)):
                raise ValueError('Model returned a nonfinite object score')
            result.update(prediction)
            result.update(points=points, keypoints_xy_score=points, error=None,
                          outside_frame_indices=[i for i, p in enumerate(points) if not (0 <= p[0] < width and 0 <= p[1] < height)],
                          outside_crop_indices=[i for i, p in enumerate(points) if not (x1 <= p[0] < x2 and y1 <= p[1] < y2)])
            return result
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            result.update(status='error', error=f'{type(exc).__name__}: {exc}', points=[], keypoints_xy_score=[])
            return result
        finally:
            result['total_ms'] = (time.perf_counter() - start) * 1000


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', choices=list(models.MODELS), required=True)
    p.add_argument('--device', choices=['cuda', 'cpu', 'auto'], default='cuda')
    p.add_argument('--dlc-max-side', type=int, default=0, help='Explicit optional input downscale;0 preserves native ROI')
    args = p.parse_args()
    if args.dlc_max_side < 0:
        p.error('--dlc-max-side cannot be negative')
    loaded = time.perf_counter()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            adapter = models.load_adapter(args.model, args.device, args.dlc_max_side)
            controller = Controller(args.model, adapter)
        record = models.MODELS[args.model]
        emit('RT_READY', {'status': 'ready', 'model': args.model, 'device_requested': args.device,
            'device': adapter.device, 'load_ms': (time.perf_counter() - loaded) * 1000,
            'keypoint_names': record['keypoint_names'], 'edges': record['edges'],
            'display_indices': record['display_indices'], 'score_semantics': record['score_semantics'],
            'load_metadata': adapter.load_metadata, 'detector_loaded': False,
            'detector_supported': record['detector_supported'], 'detector_reason': record['detector_reason'],
            'detector_weights': models.YOLO_WEIGHTS, 'detector_note': 'User-configured local detector; no predefined camera/cage ROI',
            'provenance': record['provenance'], 'coordinate_system': 'full_frame_pixels',
            'training_or_smoothing': False, 'reference_accuracy_validated': False})
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit('RT_READY', {'status': 'error', 'model': args.model, 'error': f'{type(exc).__name__}: {exc}'})
        return 1
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            request = {}
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError('Request must be a JSON object')
                if request.get('cmd') == 'stop':
                    break
                with contextlib.redirect_stdout(sys.stderr):
                    result = controller.process(request)
            except Exception as exc:
                result = frame_base(args.model, request if isinstance(request, dict) else {}, adapter.device)
                result.update(status='error', error=f'{type(exc).__name__}: {exc}')
                # Invalid NaN metadata cannot be echoed in the strict JSON protocol.
                for key in ('seq', 'timestamp'):
                    if isinstance(result[key], float) and not math.isfinite(result[key]):
                        result[key] = None
            emit('RT_RESULT', result)
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            adapter.close()
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
