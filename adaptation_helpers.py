"""Minimal original ViTPose++ checkpoint selection; no research-workspace imports.

Selection follows the author tools/model_split.py (Apache-2.0):
https://github.com/ViTAE-Transformer/ViTPose/blob/c050ed29112da7704797cc1a65af0234b525010d/tools/model_split.py
AP10K uses backbone expert3 and associate keypoint head2. The converted COCO-only
checkpoint is unsuitable. Tensor equality is checked before strict model loading.
"""


def select_ap10k_state(original, torch_module):
    selected = {}
    for key, value in original.items():
        if key.startswith('associate_keypoint_heads.') or '.mlp.experts.' in key:
            continue
        if '.mlp.fc2.' in key:
            selected[key] = torch_module.cat([value, original[key.replace('fc2.', 'experts.3.')]], dim=0)
        elif key.startswith('keypoint_head.'):
            selected[key] = original[key.replace('keypoint_head.', 'associate_keypoint_heads.2.')]
        else:
            selected[key] = value
    for key in ('keypoint_head.final_layer.weight', 'keypoint_head.final_layer.bias'):
        if not torch_module.equal(selected[key], original[key.replace('keypoint_head.', 'associate_keypoint_heads.2.')]):
            raise RuntimeError('Selected AP10K head differs from original associate head2')
    return selected
