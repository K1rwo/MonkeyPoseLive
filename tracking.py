"""Bounded live trajectory display; raw model coordinates stay unchanged."""
from collections import deque
import math
import threading
import time


class LatestSlot:
    def __init__(self):
        self.condition=threading.Condition()
        self.value=None
        self.ended=False
    def publish(self,value):
        with self.condition:
            self.value=value
            self.condition.notify_all()
    def finish(self):
        with self.condition:
            self.ended=True
            self.condition.notify_all()
    def after(self,sequence,timeout=.5):
        with self.condition:
            self.condition.wait_for(lambda:self.ended or (self.value and self.value[0]>sequence),timeout)
            return self.value if self.value and self.value[0]>sequence else None


class Trajectories:
    def __init__(self,seconds=3,max_gap=1.0):
        self.seconds=seconds
        self.max_gap=max_gap
        self.samples=deque(maxlen=1800)
    def clear(self):
        self.samples.clear()
    def add(self,result,width,height,threshold):
        pts=result.get('points',result.get('keypoints_xy_score',[]))
        t=float(result['timestamp'])
        indices=set(result.get('display_indices',range(len(pts))))
        box=result.get('crop_xyxy') or [0,0,width,height]
        accepted=result.get('status')=='ok'
        object_only='object' in result.get('score_semantics','').lower()
        valid=[]
        for i,p in enumerate(pts):
            finite=len(p)==3 and all(math.isfinite(float(v)) for v in p)
            inside=finite and 0<=p[0]<width and 0<=p[1]<height and box[0]<=p[0]<box[2] and box[1]<=p[1]<box[3]
            valid.append(bool(accepted and i in indices and inside and (object_only or p[2]>=threshold)))
        sample=dict(time=t,keypoints_xy_score=pts,valid=valid,status='ok' if accepted else 'gap',
                    frame_width=width,frame_height=height,score_filter_basis='object_only' if object_only else 'per_keypoint_raw_score')
        self.samples.append(sample)
        while self.samples and t-self.samples[0]['time']>max(15,self.seconds):
            self.samples.popleft()
        return sample
    def segments(self,joint):
        segments=[]
        previous=None
        for s in self.samples:
            if self.samples[-1]['time']-s['time']>self.seconds:
                continue
            valid=joint<len(s['valid']) and s['valid'][joint]
            if valid and previous is not None and s['time']-previous['time']<=self.max_gap:
                segments.append((previous['keypoints_xy_score'][joint][:2],s['keypoints_xy_score'][joint][:2]))
            previous=s if valid else None
        return segments
