"""Local frozen-model registry and lazy adapters. Importing this file is stdlib-only."""
from __future__ import annotations
import hashlib
import importlib.util
import math
import os
from pathlib import Path
import sys
from copy import deepcopy

APP_DIRECTORY = Path(__file__).resolve().parent
if str(APP_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(APP_DIRECTORY))
import runtime_config
NAMES = ['nose', 'left_eye', 'right_eye', 'left_ear', 'right_ear', 'left_shoulder',
         'right_shoulder', 'left_elbow', 'right_elbow', 'left_wrist', 'right_wrist',
         'left_hip', 'right_hip', 'left_knee', 'right_knee', 'left_ankle', 'right_ankle']
AP10K = ['L_Eye', 'R_Eye', 'Nose', 'Neck', 'Root of tail', 'L_Shoulder', 'L_Elbow',
         'L_F_Paw', 'R_Shoulder', 'R_Elbow', 'R_F_Paw', 'L_Hip', 'L_Knee', 'L_B_Paw',
         'R_Hip', 'R_Knee', 'R_B_Paw']
COCO_EDGES = [(0, 1), (0, 2), (1, 3), (2, 4), (5, 6), (5, 7), (7, 9), (6, 8),
              (8, 10), (5, 11), (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16)]
AP10K_EDGES = [(0, 1), (0, 2), (1, 2), (2, 3), (3, 4), (3, 5), (5, 6), (6, 7),
               (3, 8), (8, 9), (9, 10), (4, 11), (11, 12), (12, 13), (4, 14), (14, 15), (15, 16)]
PAPER15 = [0, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
_MODEL_METADATA = {
    'mmpose': {'label': 'MacaquePose HRNet-W48 (17)',
        'checkpoint_sha256': '9b34b02a4ef7eddcde7f2b350ceb919e42e570449ede42165599273e7488630b',
        'upstream': 'https://github.com/open-mmlab/mmpose', 'version': '0.29.0', 'license': 'Apache-2.0'},
    'macaque3dpose_2d': {'label': 'macaque3Dpose HRNet-W32 (display15/raw17)',
        'checkpoint_sha256': '4ccd168b879de432c1cc90e08a62088c28ea5f60fb82347ff5edb8d961a27593',
        'upstream': 'https://github.com/PrimatoModelling/macaque3Dpose', 'commit': 'f1da814cf369987d3e2b538e41a9f604c73c7784', 'license': 'MIT',
        'caveat': 'Only 2D; released17 includes two unvalidated eye channels. Display excludes them.'},
    'vitpose_animal': {'label': 'ViTPose++-S AP10K animal (17)',
        'checkpoint_sha256': '75df941cd7ae632a7b0abb136d6c11c9f967643f9ea4a1dd52cf6b642b1eb8f3',
        'upstream': 'https://github.com/ViTAE-Transformer/ViTPose', 'commit': 'c050ed29112da7704797cc1a65af0234b525010d', 'license': 'Apache-2.0',
        'caveat': 'Original AP10K expert3 + associate head2; original animal anatomy, not renamed COCO.'},
    'xpose': {'label': 'X-Pose Swin-T / macaque prompt (17)',
        'checkpoint_sha256': '5326ce945d068fec454ad759b35b42c1128934179424bfd1dfd7ab024599e4c0',
        'clip_checkpoint_sha256': '40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af',
        'upstream': 'https://github.com/IDEA-Research/X-Pose', 'commit': 'e8dae4b5cbe6c8dc185e4905582aa6c904f817f1',
        'license': 'Non-commercial scientific research', 'caveat': 'Object score only; target identity unverified; FP32 original resize800/max1333.'},
    'dlc': {'label': 'DeepLabCut ResNet50 (17, CPU)',
        'upstream': 'https://github.com/DeepLabCut/DeepLabCut', 'version': '3.0.0 / TensorFlow2.15.1', 'license': 'LGPL-3.0',
        'caveat': 'User-supplied17-point TensorFlow checkpoint; training exclusion must be established separately.'},
}
MODELS, RUNTIME_CONFIG, DETECTOR_CONFIG = {}, {}, {}
YOLO_WEIGHTS = ''


def refresh_registry():
    """Reload private settings and mutate MODELS in place for imported references."""
    global YOLO_WEIGHTS
    config = runtime_config.load_config(resolve_paths=True)
    RUNTIME_CONFIG.clear()
    RUNTIME_CONFIG.update(config)
    DETECTOR_CONFIG.clear()
    DETECTOR_CONFIG.update(config['detector'])
    YOLO_WEIGHTS = config['detector'].get('checkpoint', '')
    records = {}
    for key, metadata in _MODEL_METADATA.items():
        record = deepcopy(metadata)
        configured = config['models'].get(key, {})
        record.update(deepcopy(configured))
        for field in runtime_config.MODEL_PATH_FIELDS:
            record.setdefault(field, '')
        record.setdefault('checkpoint_files_sha256', {})
        record.update(id=key, keypoint_names=AP10K[:] if key == 'vitpose_animal' else NAMES[:],
                      edges=[list(x) for x in (AP10K_EDGES if key == 'vitpose_animal' else COCO_EDGES)],
                      display_indices=PAPER15[:] if key == 'macaque3dpose_2d' else list(range(17)))
        record.setdefault('provenance', {k: metadata[k] for k in ('upstream', 'commit', 'version', 'license') if k in metadata})
        if key == 'macaque3dpose_2d':
            record['edges'] += [[0, 3], [0, 4]]
        record['score_semantics'] = ('Object category score repeated at joints; no learned per-joint confidence' if key == 'xpose' else
            'DLC keypoint likelihood; not calibrated target accuracy' if key == 'dlc' else 'Raw maximum heatmap score; not calibrated probability')
        record.setdefault('detector_supported', key != 'dlc')
        record['detector_reason'] = ('Uses the configured local detector within user ROI' if record['detector_supported'] else
            'Optional detector disabled for this backend configuration; use manual ROI')
        record['device_supported'] = ['cpu'] if key == 'dlc' else ['cuda', 'cpu']
        required = ['python', 'config', 'checkpoint']
        if key in ('vitpose_animal', 'xpose'):
            required.append('repo')
        if key == 'xpose':
            required.append('clip_checkpoint')
        if key == 'dlc':
            required.append('pose_config')
        missing = []
        for field in required:
            value = record.get(field, '')
            if not value:
                missing.append(field + ': not configured')
            elif field == 'checkpoint' and key == 'dlc':
                prefix = Path(value)
                if not Path(value + '.index').is_file():
                    missing.append('checkpoint.index: file not found: ' + value + '.index')
                if not any(prefix.parent.glob(prefix.name + '.data-*')):
                    missing.append('checkpoint.data: no checkpoint data parts at prefix: ' + value)
            elif not (Path(value).is_dir() if field == 'repo' else Path(value).is_file()):
                missing.append(field + ': path not found: ' + value)
        if key == 'vitpose_animal' and record['repo'] and not (Path(record['repo']) / 'mmpose/models/backbones/vit.py').is_file():
            missing.append('repo: author ViT backbone not found')
        record['configuration_error'] = '; '.join(config['_errors']) or None
        record['missing'] = missing
        record['ready'] = not missing and not record['configuration_error']
        record['available'], record['missing_config'] = record['ready'], missing[:]
        record['detector_ready'] = bool(record['detector_supported'] and YOLO_WEIGHTS and Path(YOLO_WEIGHTS).is_file())
        records[key] = record
    MODELS.clear()
    MODELS.update(records)
    return MODELS


reload_registry = refresh_registry
refresh_registry()


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _verify(path, expected):
    actual = _sha(path)
    if expected and actual.lower() != expected.lower():
        raise RuntimeError('Local checkpoint hash mismatch: ' + str(path))
    return actual


def _module(path, alias):
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


class HeatmapAdapter:
    def __init__(self, model_id, device):
        import numpy as np
        import torch
        import mmcv
        from mmpose.apis import init_pose_model, inference_top_down_pose_model
        from mmpose.datasets import DatasetInfo
        self.np, self.torch, self.inference = np, torch, inference_top_down_pose_model
        self.record = MODELS[model_id]
        self.device = 'cuda:0' if device in ('cuda', 'auto') and torch.cuda.is_available() else device
        if self.device in ('cuda', 'auto'):
            self.device = 'cpu'
        torch.set_num_threads(2)
        actual_sha = _verify(self.record['checkpoint'], self.record.get('checkpoint_sha256'))
        vit_selected = False
        if model_id == 'vitpose_animal':
            source = Path(self.record['repo']) / 'mmpose/models/backbones/vit.py'
            mod = _module(source, 'mmpose.models.backbones.vit')
            base_init = mod.BaseBackbone.init_weights
            def compatible_init(instance, pretrained=None, patch_padding='pad', **kwargs):
                if pretrained is not None:
                    raise RuntimeError('Realtime ViT requires the complete published checkpoint')
                return base_init(instance, pretrained)
            mod.BaseBackbone.init_weights = compatible_init
            vit_selected = True
        cfg = mmcv.Config.fromfile(self.record['config'])
        self.model = init_pose_model(cfg, checkpoint=None, device='cpu')
        checkpoint = torch.load(self.record['checkpoint'], map_location='cpu', weights_only=False)
        state = checkpoint.get('state_dict', checkpoint)
        state = {k.removeprefix('module.'): v for k, v in state.items()}
        source_head_verified = None
        if vit_selected:
            from adaptation_helpers import select_ap10k_state
            original = state
            state = select_ap10k_state(original, torch)
            source_head_verified = all(torch.equal(v, original[k.replace('keypoint_head.', 'associate_keypoint_heads.2.')])
                                       for k, v in state.items() if k.startswith('keypoint_head.'))
            if not source_head_verified:
                raise RuntimeError('AP10K selected decoder differs from original associate head2')
        loaded = self.model.load_state_dict(state, strict=True)
        head = state['keypoint_head.final_layer.weight']
        if head.shape[0] != 17:
            raise RuntimeError('Expected strictly loaded17-channel anatomical head')
        self.load_metadata = {'strict_state_dict_load': True, 'missing_keys': list(loaded.missing_keys),
            'unexpected_keys': list(loaded.unexpected_keys), 'head_weight_shape': list(head.shape),
            'checkpoint_sha256': actual_sha, 'expected_checkpoint_sha256': self.record.get('checkpoint_sha256') or None}
        if vit_selected:
            self.load_metadata.update(selected_expert=3, selected_associate_head=2,
                                       all_selected_head_tensors_equal_original=source_head_verified)
        del checkpoint, state, head
        if vit_selected:
            del original
        self.model = self.model.eval().requires_grad_(False).to(self.device)
        self.info = DatasetInfo(self.model.cfg.data.test.dataset_info)
        names = [self.info.keypoint_info[i]['name'] for i in range(self.info.keypoint_num)]
        if names != self.record['keypoint_names']:
            raise RuntimeError('Configured native anatomical name order changed')
        self.dataset = 'AnimalAP10KDataset' if model_id == 'vitpose_animal' else 'AnimalMacaqueDataset'

    def sync(self):
        if self.device.startswith('cuda'):
            self.torch.cuda.synchronize()

    def infer(self, image, bbox, bbox_score):
        with self.torch.inference_mode():
            poses, _ = self.inference(self.model, image,
                [{'bbox': self.np.asarray(bbox, dtype=self.np.float32), 'bbox_score': bbox_score}],
                format='xyxy', dataset=self.dataset, dataset_info=self.info)
        if len(poses) != 1:
            raise RuntimeError('Expected one frozen heatmap pose')
        return {'points': self.np.asarray(poses[0]['keypoints'], dtype=float).tolist(), 'object_score': None, 'status': 'ok'}

    def close(self):
        pass


class XposeAdapter:
    def __init__(self, model_id, device):
        record = MODELS[model_id]
        actual_sha = _verify(record['checkpoint'], record.get('checkpoint_sha256'))
        clip_sha = _verify(record['clip_checkpoint'], record.get('clip_checkpoint_sha256'))
        # Our registry is loaded under a unique module alias by worker.py. This
        # name belongs to the author repository in the dedicated X-Pose process.
        sys.path.insert(0, record['repo'])
        import numpy as np
        import torch
        import clip
        import transforms as T
        from PIL import Image
        from models import build_model
        from util.config import Config
        from util.utils import clean_state_dict
        self.torch, self.np, self.Image = torch, np, Image
        torch.set_num_threads(2)
        self.device = 'cuda:0' if device in ('cuda', 'auto') and torch.cuda.is_available() else device
        if self.device in ('cuda', 'auto'):
            self.device = 'cpu'
        cfg = Config.fromfile(record['config'])
        cfg.device, cfg.use_checkpoint, cfg.use_transformer_ckpt = self.device, False, False
        checkpoint = torch.load(record['checkpoint'], map_location='cpu', weights_only=False)
        state = clean_state_dict(checkpoint['model'])
        original_load = clip.load
        def local_clip(name, device='cpu', **kwargs):
            if name != 'ViT-B/32':
                raise RuntimeError('Unexpected text-encoder request')
            # Pass the verified actual local file, never a network download.
            return original_load(record['clip_checkpoint'], device='cpu', **kwargs)
        clip.load = local_clip
        try:
            self.model = build_model(cfg)
        finally:
            clip.load = original_load
        loaded = self.model.load_state_dict(state, strict=False)
        expected = {k for k in self.model.state_dict() if k.startswith('clip_model.') and not k.startswith('clip_model.visual.')}
        if set(loaded.missing_keys) != expected or loaded.unexpected_keys:
            raise RuntimeError('X-Pose checkpoint mismatch beyond separately verified CLIP text encoder')
        self.load_metadata = {'checkpoint_sha256': actual_sha, 'clip_checkpoint_sha256': clip_sha,
            'expected_checkpoint_sha256': record.get('checkpoint_sha256') or None, 'all_pose_parameters_loaded': True,
            'missing_keys': list(loaded.missing_keys), 'unexpected_keys': list(loaded.unexpected_keys),
            'missing_policy': 'Exact CLIP text-only set; loaded from verified original local ViT-B/32 weights',
            'instance_prompt': 'macaque', 'short_side': 800, 'max_side': 1333, 'precision': 'float32'}
        del checkpoint, state
        self.model = self.model.eval().requires_grad_(False).to(self.device)
        self.transform = T.Compose([T.RandomResize([800], max_size=1333), T.ToTensor(),
                                    T.Normalize([.485, .456, .406], [.229, .224, .225])])
        prompts = [name.replace('_', ' ') for name in NAMES]
        with torch.inference_mode():
            obj = self.model.clip_model.encode_text(clip.tokenize(['a photo of macaque']).to(self.device)).float()
            kpts = self.model.clip_model.encode_text(clip.tokenize(['a photo of ' + name for name in prompts]).to(self.device)).float()
        self.target = dict(instance_text_prompt=['macaque'], keypoint_text_prompt=prompts, object_embeddings_text=obj,
            kpts_embeddings_text=torch.cat([kpts, torch.zeros(100 - 17, 512, device=self.device)]),
            kpt_vis_text=torch.cat([torch.ones(17, device=self.device), torch.zeros(100 - 17, device=self.device)]))

    def sync(self):
        if self.device.startswith('cuda'):
            self.torch.cuda.synchronize()

    def infer(self, image, bbox, bbox_score):
        h, w = image.shape[:2]
        tensor, _ = self.transform(self.Image.fromarray(image[:, :, ::-1].copy()), None)
        with self.torch.inference_mode():
            outputs = self.model(tensor.unsqueeze(0).to(self.device), [self.target])
            scores = outputs['pred_logits'][0, :, 0].sigmoid().float().cpu().numpy()
            q = int(scores.argmax())
            raw = outputs['pred_keypoints'][0, q].float().cpu().numpy()
            xy = raw[:34].reshape(17, 2) * self.np.array([w, h], self.np.float32)
        score = float(scores[q])
        return {'points': [[float(x), float(y), score] for x, y in xy], 'object_score': score,
                'status': 'ok' if score >= .1 else 'below_detection_threshold', 'selected_query': q,
                'instance_prompt': 'macaque', 'object_threshold': .1, 'query_object_scores': scores.tolist()}

    def close(self):
        pass


class DLCAdapter:
    def __init__(self, model_id, device, max_side=0):
        record = MODELS[model_id]
        prefix = Path(record['checkpoint'])
        expected_hashes = record.get('checkpoint_files_sha256', {})
        for suffix in expected_hashes:
            if not Path(str(prefix) + suffix).is_file():
                raise FileNotFoundError('Configured checkpoint part missing: ' + str(prefix) + suffix)
        parts = [p for p in prefix.parent.glob(prefix.name + '.*') if p.suffix in ('.index', '.meta') or '.data-' in p.name]
        actual_hashes = {p.name[len(prefix.name):]: _verify(p, expected_hashes.get(p.name[len(prefix.name):])) for p in parts}
        # This adapter intentionally uses the compatible CPU TensorFlow path.
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
        os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
        os.environ.setdefault('DLClight', 'True')
        os.environ.setdefault('MPLBACKEND', 'Agg')
        os.environ.setdefault('TF_NUM_INTRAOP_THREADS', '4')
        os.environ.setdefault('TF_NUM_INTEROP_THREADS', '1')
        import cv2
        import numpy as np
        import yaml
        import tensorflow as tf
        tf.compat.v1.disable_eager_execution()
        tf.config.threading.set_intra_op_parallelism_threads(4)
        tf.config.threading.set_inter_op_parallelism_threads(1)
        from deeplabcut.pose_estimation_tensorflow.config import load_config
        from deeplabcut.pose_estimation_tensorflow.core import predict
        raw = yaml.safe_load(Path(record['pose_config']).read_text(encoding='utf-8-sig'))
        project = yaml.safe_load(Path(record['config']).read_text(encoding='utf-8-sig'))
        if raw['all_joints_names'] != NAMES or project['bodyparts'] != NAMES:
            raise RuntimeError('Existing DLC native name order differs')
        cfg = load_config(record['pose_config'])
        cfg['init_weights'], cfg['batch_size'], cfg['num_outputs'] = record['checkpoint'], 1, 1
        self.session, self.inputs, self.outputs = predict.setup_pose_prediction(cfg, allow_growth=True)
        checkpoint_vars = dict(tf.train.list_variables(record['checkpoint']))
        graph_vars = self.session.graph.get_collection(tf.compat.v1.GraphKeys.GLOBAL_VARIABLES)
        mismatch = [v.name for v in graph_vars if v.name.split(':')[0] not in checkpoint_vars or
                    list(v.shape.as_list()) != checkpoint_vars[v.name.split(':')[0]]]
        heads = [v for v in graph_vars if '/part_pred/' in v.name or '/locref_pred/' in v.name]
        if mismatch or not heads or self.outputs[0].shape.as_list()[-1] != 17:
            self.session.close()
            raise RuntimeError('DLC graph/head checkpoint verification failed: ' + str(mismatch))
        reader = tf.train.load_checkpoint(record['checkpoint'])
        head_equal = all(np.array_equal(self.session.run(v), reader.get_tensor(v.name.split(':')[0])) for v in heads)
        if not head_equal:
            self.session.close()
            raise RuntimeError('DLC restored head tensors differ from the published local checkpoint')
        self.cv2, self.np, self.cfg, self.predict = cv2, np, cfg, predict
        self.device, self.max_side = 'cpu', max_side
        self.load_metadata = {'all_graph_variables_present_with_matching_shapes': True,
            'restore_api': 'Original tf.compat.v1.train.Saver.restore via predict.setup_pose_prediction',
            'head_variable_names': [v.name for v in heads], 'restored_head_tensors_equal_checkpoint': True,
            'checkpoint_files_sha256': actual_hashes, 'expected_checkpoint_parts_verified': list(expected_hashes),
            'effective_device': 'cpu', 'device_note': 'This legacy TensorFlow adapter is configured CPU-only',
            'input_max_side': max_side, 'training_global_scale_applied': False}

    def sync(self):
        pass

    def infer(self, image, bbox, bbox_score):
        h, w = image.shape[:2]
        network_image = image
        if self.max_side and max(h, w) > self.max_side:
            factor = self.max_side / max(h, w)
            network_image = self.cv2.resize(image, (max(1, round(w * factor)), max(1, round(h * factor))), interpolation=self.cv2.INTER_AREA)
        pose = self.np.asarray(self.predict.getpose(self.cv2.cvtColor(network_image, self.cv2.COLOR_BGR2RGB),
                               self.cfg, self.session, self.inputs, self.outputs), dtype=float)
        pose[:, 0] *= w / network_image.shape[1]
        pose[:, 1] *= h / network_image.shape[0]
        return {'points': pose.tolist(), 'object_score': None, 'status': 'ok'}

    def close(self):
        self.session.close()


def load_adapter(model_id, device='cuda', dlc_max_side=0):
    if model_id not in MODELS or device not in ('cuda', 'cpu', 'auto'):
        raise ValueError('Unsupported model/device')
    record = MODELS[model_id]
    if not record['ready']:
        details = ([record['configuration_error']] if record['configuration_error'] else []) + record['missing']
        raise ValueError('Model configuration is not ready: ' + '; '.join(details))
    if model_id in ('mmpose', 'macaque3dpose_2d', 'vitpose_animal'):
        return HeatmapAdapter(model_id, device)
    if model_id == 'xpose':
        return XposeAdapter(model_id, device)
    return DLCAdapter(model_id, device, dlc_max_side)
