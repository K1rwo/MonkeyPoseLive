"""Persistent optional face worker; RT_READY/RT_RESULT match the body protocol.

Only this private host process imports model libraries. The nested expression
mode runs in the configured TensorFlow interpreter and accepts encoded face
pixels on stdin. It is never an HTTP endpoint or a user-selectable executable.
"""
from __future__ import annotations
import argparse
import base64
import contextlib
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

import face_models


def emit(marker,payload):
    print(marker+' '+json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')),flush=True)


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def roi_pixels(roi,width,height):
    if roi is None:
        roi = [0,0,1,1]
    if (not isinstance(roi,(list,tuple)) or len(roi)!=4 or
            not all(finite(v) and 0<=v<=1 for v in roi) or
            roi[0]>=roi[2] or roi[1]>=roi[3]):
        raise ValueError('roi must be normalized [x1,y1,x2,y2] with positive area')
    box = [math.floor(roi[0]*width),math.floor(roi[1]*height),
           math.ceil(roi[2]*width),math.ceil(roi[3]*height)]
    if box[2]-box[0]<2 or box[3]-box[1]<2:
        raise ValueError('roi is too small at the actual frame resolution')
    return box


def frame_base(model_id,request,device):
    record = face_models.FACE_MODELS[model_id]
    return dict(model=model_id,task=record['category'],seq=request.get('seq') if
        isinstance(request.get('seq'),int) and not isinstance(request.get('seq'),bool) else None,
        timestamp=request.get('timestamp') if finite(request.get('timestamp')) else None,
        points=[],keypoints_xy_score=[],keypoint_names=record['keypoint_names'],edges=record['edges'],
        display_indices=record['display_indices'],score_semantics=record['score_semantics'],
        status='error',error=None,object_score=None,roi_pixels=None,crop_xyxy=None,
        bbox_xyxy=None,face_bbox_xyxy=None,detector_score=None,inference_ms=0.,coordinate_system='full_frame_pixels',
        device=device,identity_independently_verified=False,internal_affect_validated=False)


class Controller:
    def __init__(self,model_id,adapter):
        import cv2
        import numpy as np
        self.cv2,self.np,self.model_id,self.adapter = cv2,np,model_id,adapter
        cv2.setNumThreads(1)

    def process(self,request):
        start = time.perf_counter()
        result = frame_base(self.model_id,request,self.adapter.device)
        try:
            if not isinstance(request.get('seq'),int) or isinstance(request.get('seq'),bool):
                raise ValueError('seq must be an integer')
            if not finite(request.get('timestamp')):
                raise ValueError('timestamp must be finite')
            if request.get('detector',False) is not False:
                raise ValueError('Face backends use their own face detector; body detector must be false')
            path = Path(request['image_path'])
            if path.stat().st_size > 32*1024*1024:
                raise ValueError('Encoded frame exceeds32MiB')
            payload = path.read_bytes()
            if len(payload)>32*1024*1024:
                raise ValueError('Encoded frame exceeds32MiB')
            image = self.cv2.imdecode(self.np.frombuffer(payload,dtype=self.np.uint8),self.cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('Cannot decode submitted frame')
            h,w = image.shape[:2]
            if w*h>16_000_000:
                raise ValueError('Frame exceeds16MP')
            roi = roi_pixels(request.get('roi'),w,h)
            x1,y1,x2,y2 = roi
            predicted = self.adapter.predict(image[y1:y2,x1:x2])
            result.update(width=w,height=h,image_size=[w,h],roi_pixels=roi,crop_xyxy=roi,
                detector_enabled=True,detector_device=self.adapter.device,
                bbox_source='highest_confidence_primateface_face_within_user_roi',
                detector_score=predicted.get('detector_score'),face_count=predicted.get('face_count',0),
                status=predicted['status'])
            if predicted['status'] not in ('ok','no_face'):
                raise ValueError('Invalid face backend status')
            if predicted['status']=='no_face':
                result.update(reason='No PrimateFace face above0.5 inside user ROI')
            box = predicted.get('bbox_xyxy')
            if box is not None:
                if len(box)!=4 or not all(finite(v) for v in box) or box[2]<=box[0] or box[3]<=box[1]:
                    raise ValueError('Face backend returned invalid face box')
                result['bbox_xyxy'] = [float(box[0]+x1),float(box[1]+y1),float(box[2]+x1),float(box[3]+y1)]
                result['face_bbox_xyxy'] = result['bbox_xyxy'][:]
            points = predicted.get('points',[])
            if self.model_id=='primateface_landmarks' and predicted['status']=='ok':
                if len(points)!=68 or any(len(p)!=3 or not all(finite(v) for v in p) for p in points):
                    raise ValueError('Face backend returned invalid68-point landmarks')
                result['keypoint_names'] = getattr(self.adapter,'names',result['keypoint_names'])
                result['points'] = [[float(p[0]+x1),float(p[1]+y1),float(p[2])] for p in points]
                result['keypoints_xy_score'] = result['points']
            if self.model_id=='primateface_expression':
                expression = predicted.get('expression',{})
                if predicted['status']=='ok':
                    if expression.get('available') is not True:
                        raise ValueError('Missing facial-display prediction for detected face')
                    if [p.get('label') for p in expression.get('probabilities',[])] != face_models.EXPRESSION_CLASSES:
                        raise ValueError('Invalid facial-display class order')
                    expression = face_models.expression_payload([p['score'] for p in expression['probabilities']])
                else:
                    expression = dict(available=False,label=None,score=None,probabilities=[],reason='no_face',
                                      internal_affect_validated=False)
                result['expression'] = expression
                crop = predicted.get('face_crop_xyxy')
                if crop:
                    result['face_crop_xyxy'] = [crop[0]+x1,crop[1]+y1,crop[2]+x1,crop[3]+y1]
        except Exception as ex:
            result.update(status='error',error=str(ex),points=[],keypoints_xy_score=[])
            result.pop('expression',None)
            traceback.print_exc(file=sys.stderr)
        finally:
            result['inference_ms'] = (time.perf_counter()-start)*1000
        return result


def expression_child(model_id):
    record = face_models.FACE_MODELS[model_id]
    names = face_models.verify_mapping(record['class_mapping_audit'],record['expression_repo'])
    checkpoint_hash = face_models.verify_file(record,'expression_checkpoint')
    architecture_hash = face_models.verify_file(record,'expression_architecture')
    os.environ['CUDA_VISIBLE_DEVICES']='-1'
    os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL','2')
    import cv2
    import numpy as np
    import tensorflow as tf
    cv2.setNumThreads(1)
    tf.config.set_visible_devices([],'GPU')
    tf.config.threading.set_intra_op_parallelism_threads(4)
    tf.config.threading.set_inter_op_parallelism_threads(2)
    with contextlib.redirect_stdout(sys.stderr):
        model = tf.keras.models.model_from_json(Path(record['expression_architecture']).read_text(encoding='utf-8'))
        model.load_weights(record['expression_checkpoint'])
        model = tf.keras.Model(model.input,model.get_layer('emo_fc8').output)
    emit('RT_READY',dict(status='ready',model=model_id,device='cpu',class_names=names,
        upstream=face_models.EXPRESSION_PROVENANCE,checkpoint_sha256=checkpoint_hash,
        architecture_sha256=architecture_hash,mapping_audit_sha256=face_models.sha256(record['class_mapping_audit']),
        tensorflow=tf.__version__,training=False,
        preprocessing='Author BGR resize224x224 float32/255 minus original BGR means'))
    for line in sys.stdin:
        try:
            if len(line)>48*1024*1024:
                raise ValueError('Face payload too large')
            request = json.loads(line)
            if request.get('cmd')=='stop':
                break
            payload = base64.b64decode(request['face_png'],validate=True)
            if len(payload)>32*1024*1024:
                raise ValueError('Face image too large')
            face = cv2.imdecode(np.frombuffer(payload,dtype=np.uint8),cv2.IMREAD_COLOR)
            if face is None or min(face.shape[:2])<2 or face.shape[0]*face.shape[1]>16_000_000:
                raise ValueError('Invalid face image')
            x = cv2.resize(face,(224,224)).astype(np.float32)/255.
            x -= np.asarray(face_models.MEAN_BGR,dtype=np.float32)
            with contextlib.redirect_stdout(sys.stderr):
                scores = model(x[None],training=False).numpy()[0].tolist()
            face_models.expression_payload(scores)
            emit('RT_RESULT',dict(status='ok',probabilities=scores))
        except Exception as ex:
            traceback.print_exc(file=sys.stderr)
            emit('RT_RESULT',dict(status='error',error=str(ex)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',choices=tuple(face_models.FACE_MODELS),required=True)
    parser.add_argument('--device',choices=('cuda','cpu','auto'),default='cuda')
    parser.add_argument('--expression-child',action='store_true')
    args = parser.parse_args()
    adapter = None
    try:
        if args.expression_child:
            expression_child(args.model)
            return
        with contextlib.redirect_stdout(sys.stderr):
            adapter = face_models.create_adapter(args.model,args.device)
        controller = Controller(args.model,adapter)
        emit('RT_READY',dict(status='ready',model=args.model,task=face_models.FACE_MODELS[args.model]['category'],
                            device=adapter.device,provenance=adapter.provenance))
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request.get('cmd')=='stop':
                    break
                with contextlib.redirect_stdout(sys.stderr):
                    result = controller.process(request)
                emit('RT_RESULT',result)
            except Exception as ex:
                emit('RT_RESULT',dict(status='error',error=str(ex)))
    except Exception as ex:
        traceback.print_exc(file=sys.stderr)
        emit('RT_READY',dict(status='error',error=str(ex),model=args.model))
        raise SystemExit(1)
    finally:
        if adapter is not None:
            adapter.close()


if __name__=='__main__':
    main()
