"""Output regressions for facial tasks and removal of image motion trails."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import runtime_config
from web_inference import CSV_FIELDS,prediction_csv_rows,render_frame_overlay


class DrawingProbe:
    FONT_HERSHEY_SIMPLEX=0
    def __init__(self):
        self.lines=[]
        self.circles=[]
        self.boxes=[]
        self.text=[]
    def line(self,image,a,b,color,*args): self.lines.append((a,b,color))
    def circle(self,image,point,*args): self.circles.append(point)
    def rectangle(self,image,a,b,*args): self.boxes.append((a,b))
    def putText(self,image,text,*args): self.text.append(text)


class WebOverlayTests(unittest.TestCase):
    def test_two_moving_frames_never_draw_a_history_connector(self):
        probe=DrawingProbe()
        for x in (10,100):
            render_frame_overlay(probe,None,dict(points=[[x,x,.9],[x+10,x+10,.8]],edges=[[0,1]]),[True,True])
        self.assertEqual(probe.lines,[((10,10),(20,20),(50,220,250)),((100,100),(110,110),(50,220,250))])
        self.assertNotIn(((20,20),(100,100),(255,180,70)),probe.lines)

    def test_no_face_does_not_reuse_previous_box_or_expression(self):
        probe=DrawingProbe()
        render_frame_overlay(probe,None,dict(points=[],edges=[],expression={'available':False}),[])
        self.assertEqual((probe.lines,probe.circles,probe.boxes,probe.text),([],[],[],[]))

    def test_face_native_points_and_unavailable_points_keep_visibility_mask(self):
        probe=DrawingProbe()
        points=[[i+10,i+10,.9] for i in range(68)]
        valid=[True]*68
        valid[3]=False
        render_frame_overlay(probe,None,dict(points=points,edges=[[2,3],[3,4],[66,67]],face_bbox_xyxy=[5,5,100,100]),valid)
        self.assertEqual(probe.lines,[((76,76),(77,77),(50,220,250))])
        self.assertEqual(len(probe.circles),67)
        self.assertEqual(probe.boxes,[((5,5),(100,100))])


class FacialCSVTests(unittest.TestCase):
    def test_expression_csv_keeps_all_four_scores_without_fake_keypoints(self):
        expression=dict(available=True,label='lipsmack',score=.4,
                        probabilities=[dict(label=k,score=v) for k,v in zip(
                            ['neutral','lipsmack','threat','fearful'],[.3,.4,.2,.1])])
        rows=prediction_csv_rows(dict(sequence=7,timestamp=.4,status='ok',task='facial_expression',points=[],expression=expression),[])
        records=[dict(zip(CSV_FIELDS,row)) for row in rows]
        self.assertEqual([r['class_label'] for r in records],['neutral','lipsmack','threat','fearful'])
        self.assertEqual([r['class_score'] for r in records],[.3,.4,.2,.1])
        self.assertTrue(all(r['joint_index']=='' and r['x_pixels']=='' and r['expression_label']=='lipsmack' for r in records))

    def test_no_face_exports_an_explicit_gap_without_category_scores(self):
        rows=prediction_csv_rows(dict(sequence=8,timestamp=.5,status='no_face',task='facial_expression',
                                     expression=dict(available=False,label=None,score=None,probabilities=[])),[])
        self.assertEqual(len(rows),1)
        record=dict(zip(CSV_FIELDS,rows[0]))
        self.assertFalse(record['expression_available'])
        self.assertEqual(record['class_score'],'')
        self.assertEqual(record['status'],'no_face')

    def test_original_body_native_coordinates_remain_unmodified(self):
        rows=prediction_csv_rows(dict(sequence=1,timestamp=0,status='ok',points=[[10,20,.15],[30,40,.9]],
                                     keypoint_names=['nose','wrist']),[False,True])
        self.assertEqual(rows[0][:10],[1,0,0,'nose',10,20,.15,False,'ok','resized_frame_pixels'])
        self.assertEqual(rows[1][:10],[1,0,1,'wrist',30,40,.9,True,'ok','resized_frame_pixels'])


class OptionalFaceConfigurationTests(unittest.TestCase):
    def test_relative_face_paths_resolve_and_body_configuration_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'private/config.json'
            value=dict(schema_version=1,models={'mmpose':{'checkpoint':'body.pth'}},
                       face_models={'primateface_landmarks':{'python':'env/python.exe','repo':'../upstream',
                                   'landmark_checkpoint':'weights/face.pth','landmark_checkpoint_sha256':'a'*64}})
            runtime_config.save_config(value,path)
            resolved=runtime_config.load_config(path,resolve_paths=True)
            self.assertFalse(resolved['config_error'])
            self.assertEqual(resolved['face_models']['primateface_landmarks']['python'],str((path.parent/'env/python.exe').resolve()))
            self.assertEqual(resolved['models']['mmpose']['checkpoint'],str((path.parent/'body.pth').resolve()))
            saved=runtime_config.save_config(runtime_config.load_config(path),path)
            self.assertTrue(Path(saved['_backup_path']).exists())
            self.assertEqual(saved['face_models']['primateface_landmarks']['landmark_checkpoint_sha256'],'a'*64)

    def test_invalid_face_configuration_cannot_replace_existing_private_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'config.json'
            runtime_config.save_config({'schema_version':1},path)
            before=path.read_bytes()
            for bad in ({'unexpected':{}},{'primateface_landmarks':{'python':[]}},
                        {'primateface_expression':{'expression_checkpoint_sha256':'not-a-sha'}}):
                with self.assertRaises(ValueError):
                    runtime_config.save_config(dict(schema_version=1,face_models=bad),path)
                self.assertEqual(path.read_bytes(),before)

    def test_remote_registry_includes_all_tasks_and_excludes_private_paths(self):
        import models
        import face_models
        import remote_server
        body={'mmpose':dict(id='mmpose',label='Body',ready=True,python='PRIVATE-INTERPRETER',checkpoint='PRIVATE-WEIGHT')}
        face={k:dict(id=k,label='Face',task=task,ready=True,python='PRIVATE-FACE',expression_checkpoint='PRIVATE-EXPRESSION')
              for k,task in [('primateface_landmarks','face_landmarks'),('primateface_expression','facial_expression')]}
        with patch.object(models,'MODELS',body),patch.object(face_models,'FACE_MODELS',face):
            records=remote_server.safe_models()
        self.assertEqual([r['task'] for r in records],['body_pose','face_landmarks','facial_expression'])
        self.assertNotIn('PRIVATE',json.dumps(records))


if __name__=='__main__':
    unittest.main()
