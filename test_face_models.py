"""Face backend contracts without downloading weights or importing ML runtimes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import face_models
import face_worker


class FaceMetadataTests(unittest.TestCase):
    def tearDown(self):
        face_models.refresh_registry()

    def test_face_registry_disabled_without_private_configuration(self):
        records = face_models.refresh_registry({'face_models':{},'_errors':[]})
        self.assertEqual(set(records),{'primateface_landmarks','primateface_expression'})
        for record in records.values():
            self.assertFalse(record['ready'])
            self.assertFalse(record['detector_supported'])
            self.assertTrue(record['native_face_detector'])
        landmarks = records['primateface_landmarks']
        self.assertEqual(len(landmarks['keypoint_names']),68)
        self.assertNotIn([16,17],landmarks['edges'])
        self.assertNotIn([35,36],landmarks['edges'])
        self.assertIn([41,36],landmarks['edges'])
        self.assertEqual(records['primateface_expression']['keypoint_names'],[])

    def test_registry_requires_the_correct_external_interpreters_and_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'file').write_text('external placeholder')
            record = {field:str(path if field in ('repo','expression_repo') else path/'file')
                      for field in face_models.FACE_PATH_FIELDS}
            config = {'face_models':{'primateface_expression':record},'_errors':[]}
            self.assertTrue(face_models.refresh_registry(config)['primateface_expression']['ready'])
            config['face_models']['primateface_expression']['expression_python']=''
            result = face_models.refresh_registry(config)['primateface_expression']
            self.assertFalse(result['ready'])
            self.assertIn('expression_python: not configured',result['missing'])

    def test_mapping_audit_must_match_current_author_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in ('train_id_exp.xls','exponehot.npy'):
                (path/name).write_bytes(b'author mapping fixture '+name.encode())
            audit = dict(mapping_verified=True,matched_rows=12412,xls_rows=12412,
                mapping={str(i):name for i,name in enumerate(face_models.EXPRESSION_CLASSES)},
                sha256={name:face_models.sha256(path/name) for name in ('train_id_exp.xls','exponehot.npy')})
            (path/'audit.json').write_text(json.dumps(audit))
            self.assertEqual(face_models.verify_mapping(path/'audit.json',path),face_models.EXPRESSION_CLASSES)
            (path/'exponehot.npy').write_bytes(b'changed evidence')
            with self.assertRaisesRegex(ValueError,'evidence changed'):
                face_models.verify_mapping(path/'audit.json',path)
            audit['mapping']['1']='submissive'
            (path/'audit.json').write_text(json.dumps(audit))
            with self.assertRaisesRegex(ValueError,'class mapping differs'):
                face_models.verify_mapping(path/'audit.json',path)

    def test_expression_scores_preserve_author_order_and_reject_invalid_softmax(self):
        result = face_models.expression_payload([.1,.2,.6,.1])
        self.assertEqual(result['label'],'threat')
        self.assertEqual([p['label'] for p in result['probabilities']],face_models.EXPRESSION_CLASSES)
        self.assertFalse(result['internal_affect_validated'])
        for values in ([.1,.2,.3], [0,0,0,0], [float('nan'),0,0,1],[-.1,0,0,1.1], [True,0,0,0]):
            with self.subTest(values=values),self.assertRaises(ValueError):
                face_models.expression_payload(values)

    def test_hash_verification_fails_before_loading_an_unexpected_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'checkpoint'
            path.write_bytes(b'fixture')
            record = dict(expression_checkpoint=str(path),expression_checkpoint_sha256='0'*64)
            with self.assertRaisesRegex(ValueError,'hash mismatch'):
                face_models.verify_file(record,'expression_checkpoint')


class FakeAdapter:
    device='cpu'
    names=face_models.FACE68_NAMES[:]
    def __init__(self,result):
        self.result=result
        self.calls=[]
    def predict(self,image):
        self.calls.append(image.shape)
        return deepcopy(self.result)


class FaceFrameTests(unittest.TestCase):
    def setUp(self):
        import cv2
        import numpy as np
        self.temporary=tempfile.TemporaryDirectory()
        self.path=Path(self.temporary.name)/'frame.jpg'
        ok,data=cv2.imencode('.jpg',np.zeros((80,100,3),np.uint8))
        self.assertTrue(ok)
        self.path.write_bytes(data.tobytes())
        self.request=dict(image_path=str(self.path),seq=7,timestamp=1.5,roi=[.2,.25,.8,.75],detector=False)

    def tearDown(self):
        self.temporary.cleanup()

    def test_landmarks_are_full_frame_native68_and_boxes_follow_roi_origin(self):
        adapter=FakeAdapter(dict(status='ok',points=[[i*.1,i*.2,.9] for i in range(68)],
            bbox_xyxy=[1,2,31,32],detector_score=.95,face_count=1))
        result=face_worker.Controller('primateface_landmarks',adapter).process(self.request)
        self.assertEqual(adapter.calls,[(40,60,3)])
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['task'],'face_landmarks')
        self.assertEqual(result['points'][0],[20.,20.,.9])
        self.assertEqual(result['points'][-1],[26.7,33.4,.9])
        self.assertEqual(result['bbox_xyxy'],[21.,22.,51.,52.])
        self.assertEqual(result['face_bbox_xyxy'],result['bbox_xyxy'])
        self.assertEqual(len(result['points']),68)
        self.assertEqual(len(result['display_indices']),68)

    def test_no_face_retains_a_gap_without_display_category(self):
        adapter=FakeAdapter(dict(status='no_face',points=[],bbox_xyxy=None,detector_score=None))
        result=face_worker.Controller('primateface_expression',adapter).process(self.request)
        self.assertEqual(result['status'],'no_face')
        self.assertEqual(result['points'],[])
        self.assertFalse(result['expression']['available'])
        self.assertIsNone(result['expression']['label'])
        self.assertEqual(result['expression']['probabilities'],[])

    def test_expression_mode_emits_real_class_scores_without_fake_landmarks(self):
        adapter=FakeAdapter(dict(status='ok',points=[],bbox_xyxy=[1,2,31,32],detector_score=.95,
            expression=face_models.expression_payload([.1,.2,.6,.1]),face_crop_xyxy=[0,0,35,36]))
        result=face_worker.Controller('primateface_expression',adapter).process(self.request)
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['task'],'facial_expression')
        self.assertEqual(result['expression']['label'],'threat')
        self.assertEqual(result['points'],[])
        self.assertEqual(result['face_crop_xyxy'],[20,20,55,56])

    def test_bad_roi_or_body_detector_never_reaches_face_inference(self):
        adapter=FakeAdapter({})
        controller=face_worker.Controller('primateface_landmarks',adapter)
        for change in (dict(roi=[0,0,float('nan'),1]),dict(roi=[.8,0,.2,1]),dict(detector=True)):
            with self.subTest(change=change),patch('face_worker.traceback.print_exc'):
                result=controller.process(dict(self.request,**change))
                self.assertEqual(result['status'],'error')
                self.assertEqual(result['points'],[])
        self.assertEqual(adapter.calls,[])

    def test_nonfinite_face_point_is_not_exposed(self):
        points=[[0.,0.,.9] for _ in range(68)]
        points[50][0]=float('nan')
        adapter=FakeAdapter(dict(status='ok',points=points,bbox_xyxy=[1,2,31,32],detector_score=.95))
        with patch('face_worker.traceback.print_exc'):
            result=face_worker.Controller('primateface_landmarks',adapter).process(self.request)
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['points'],[])
        json.dumps(result,allow_nan=False)


if __name__=='__main__':
    unittest.main()
