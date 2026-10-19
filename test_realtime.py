import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
from types import SimpleNamespace

from tracking import LatestSlot,Trajectories
from server import LiveSession,handler_for


class TimelineTests(unittest.TestCase):
    def test_latest_slot_never_queues_old_frames(self):
        slot=LatestSlot()
        slot.publish((1,1.,'old'))
        slot.publish((7,2.,'new'))
        self.assertEqual(slot.after(0,0)[0],7)
        self.assertIsNone(slot.after(7,0))
        slot.finish()
        self.assertIsNone(slot.after(7,0))
    def result(self,t,status='ok',score=.8,**kw):
        return dict(timestamp=t,status=status,points=[[20.,30.,score]],**kw)
    def test_missing_and_time_gaps_break_trajectory(self):
        trail=Trajectories(3)
        for r in [self.result(0),self.result(.1),self.result(.2,'no_target_bbox'),self.result(.3)]:
            trail.add(r,100,100,.2)
        self.assertEqual(len(trail.segments(0)),1)
        trail.add(self.result(1.5),100,100,.2)
        self.assertEqual(len(trail.segments(0)),1)
    def test_low_scores_outside_and_unvalidated_channels_hidden_not_erased(self):
        trail=Trajectories()
        raw=[[20.,20.,.9],[30.,30.,.9],[-1.,20.,.9],[50.,30.,.1]]
        r=dict(timestamp=1,status='ok',points=raw,display_indices=[0,2,3])
        sample=trail.add(r,100,100,.2)
        self.assertEqual(sample['valid'],[True,False,False,False])
        self.assertEqual(sample['keypoints_xy_score'],raw)
    def test_object_score_is_not_a_joint_confidence(self):
        trail=Trajectories()
        s=trail.add(self.result(0,score=.12,score_semantics='object confidence repeated'),100,100,.9)
        self.assertTrue(s['valid'][0])
        s=trail.add(self.result(.1,'below_detection_threshold',.09,score_semantics='object confidence repeated'),100,100,.01)
        self.assertFalse(s['valid'][0])
    def test_roi_change_clears_tracks(self):
        with tempfile.TemporaryDirectory() as folder:
            session=LiveSession(Path(folder))
            session.trails.add(self.result(1),100,100,.2)
            session.configure({'roi':[.2,.1,.8,.9]})
            self.assertEqual(len(session.trails.samples),0)
            snapshot=session.snapshot()
            self.assertEqual(snapshot['configuration']['roi'],[.2,.1,.8,.9])
            self.assertEqual(snapshot['configuration_epoch'],session.tracking_epoch)
    def test_stalled_old_camera_prevents_new_session(self):
        with tempfile.TemporaryDirectory() as folder:
            session=LiveSession(Path(folder))
            session.capture_thread=SimpleNamespace(is_alive=lambda:True)
            with self.assertRaises(ValueError):
                session.start({'model':'mmpose','source_type':'usb','source':'0'})
            self.assertIsNone(session.thread)


class LocalHTTPTests(unittest.TestCase):
    def test_state_and_cross_origin_start_rejected_without_camera(self):
        with tempfile.TemporaryDirectory() as folder:
            session=LiveSession(Path(folder))
            server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(session))
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base+'/api/state') as response:
                    self.assertEqual(json.load(response)['status'],'idle')
                request=Request(base+'/api/start',data=b'{}',headers={'Origin':'https://unrelated.invalid','Content-Type':'application/json'})
                with self.assertRaises(HTTPError) as error:
                    urlopen(request)
                self.assertEqual(error.exception.code,403)
                self.assertIsNone(session.thread)
            finally:
                server.shutdown()
                server.server_close()


if __name__=='__main__':
    unittest.main(verbosity=2)
