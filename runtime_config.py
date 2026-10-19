"""Private, portable runtime configuration. This module uses the standard library only."""
from __future__ import annotations
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

MODEL_IDS = ('mmpose', 'macaque3dpose_2d', 'vitpose_animal', 'xpose', 'dlc')
MODEL_PATH_FIELDS = ('python', 'config', 'checkpoint', 'repo', 'clip_checkpoint', 'pose_config')
DEFAULT_CONFIG = {'schema_version': 1, 'base_python': '', 'output_directory': '',
                  'detector': {'checkpoint': '', 'device': 'cpu'}, 'models': {k: {} for k in MODEL_IDS}}


def _expanded(value):
    value = os.path.expanduser(os.path.expandvars(str(value)))
    return re.sub(r'%([^%]+)%', lambda m: os.environ.get(m.group(1), m.group(0)), value)


def local_app_directory():
    local = os.environ.get('LOCALAPPDATA')
    return (Path(_expanded(local)) if local else Path.home() / 'AppData/Local') / 'MonkeyPoseLive'


def default_config_path():
    return local_app_directory() / 'config.json'


def config_path(path=None):
    value = path if path is not None else os.environ.get('MONKEYPOSE_CONFIG')
    return Path(_expanded(value)).resolve() if value else default_config_path().resolve()


def default_output_directory():
    # Read the editable (unresolved) config to avoid recursive resolution.
    value=load_config().get('output_directory','')
    if not value:
        return local_app_directory() / 'sessions'
    target=Path(_expanded(value))
    return target.resolve() if target.is_absolute() else (config_path().parent/target).resolve()


def _validate(value):
    if not isinstance(value, dict):
        raise ValueError('Configuration must be a JSON object')
    if value.get('schema_version', 1) != 1 or isinstance(value.get('schema_version'), bool):
        raise ValueError('Unsupported schema_version; expected1')
    for key in ('base_python', 'output_directory'):
        if key in value and not isinstance(value[key], str):
            raise ValueError(key + ' must be a string path')
    detector = value.get('detector', {})
    if not isinstance(detector, dict):
        raise ValueError('detector must be an object')
    for key in ('checkpoint', 'device'):
        if key in detector and not isinstance(detector[key], str):
            raise ValueError('detector.' + key + ' must be a string')
    model_values = value.get('models', {})
    if not isinstance(model_values, dict):
        raise ValueError('models must be an object')
    for model_id, record in model_values.items():
        if model_id not in MODEL_IDS or not isinstance(record, dict):
            raise ValueError('Unknown model ID or invalid model object: ' + str(model_id))
        for key in MODEL_PATH_FIELDS:
            if key in record and not isinstance(record[key], str):
                raise ValueError('models.' + model_id + '.' + key + ' must be a string path')
        if 'detector_supported' in record and not isinstance(record['detector_supported'], bool):
            raise ValueError('models.' + model_id + '.detector_supported must be boolean')
        for key in ('checkpoint_sha256', 'clip_checkpoint_sha256'):
            if key in record and (not isinstance(record[key], str) or record[key] and not re.fullmatch(r'[0-9a-fA-F]{64}', record[key])):
                raise ValueError('models.' + model_id + '.' + key + ' must be empty or contain64 hexadecimal characters')
        hashes = record.get('checkpoint_files_sha256', {})
        if not isinstance(hashes, dict) or any(not isinstance(k, str) or not k.startswith('.') or '/' in k or '\\' in k or
                                             not isinstance(v, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', v) for k, v in hashes.items()):
            raise ValueError('models.' + model_id + '.checkpoint_files_sha256 must map file suffixes to SHA256 strings')
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('Configuration must contain finite JSON values') from exc
    return value


def _merged(value):
    result = deepcopy(DEFAULT_CONFIG)
    result.update({k: deepcopy(v) for k, v in value.items() if not k.startswith('_') and k not in ('detector', 'models')})
    result['detector'].update(deepcopy(value.get('detector', {})))
    for key, item in value.get('models', {}).items():
        result['models'][key].update(deepcopy(item))
    return result


def load_config(path=None, resolve_paths=False):
    """Return raw editable configuration by default; errors are metadata, not import failures.

    Internal keys start with _. Model loaders request resolve_paths=True. Relative
    paths resolve against the private JSON directory, independent of working dir.
    """
    target = config_path(path)
    errors = []
    value = {}
    if target.exists():
        try:
            value = _validate(json.loads(target.read_text(encoding='utf-8-sig')))
        except (OSError, ValueError, TypeError) as exc:
            errors.append('Cannot load configuration ' + str(target) + ': ' + str(exc))
            value = {}
    result = _merged(value)
    if resolve_paths:
        def resolve(value):
            if not value:
                return ''
            p = Path(_expanded(value))
            return str(p.resolve() if p.is_absolute() else (target.parent / p).resolve())
        result['base_python'] = resolve(result['base_python'])
        result['output_directory'] = resolve(result['output_directory']) or str((local_app_directory()/'sessions').resolve())
        result['detector']['checkpoint'] = resolve(result['detector']['checkpoint'])
        for model in result['models'].values():
            for key in MODEL_PATH_FIELDS:
                if key in model:
                    model[key] = resolve(model[key])
    result.update(_config_path=str(target), _config_exists=target.exists(), _errors=errors, config_error='; '.join(errors) or None)
    return result


def save_config(value, path=None):
    """Validate, back up an existing private config, and atomically replace it.

    Returns raw editable config, with _backup_path when a backup was created.
    The source/data directory is never used as an implicit save location.
    """
    value = _merged(_validate({k: v for k, v in value.items() if not k.startswith('_') and k != 'config_error'} if isinstance(value, dict) else value))
    target = config_path(path)
    package = Path(__file__).resolve().parent
    if target == package or package in target.parents:
        raise ValueError('Save private configuration outside the application/source directory')
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        backup = target.with_name(target.stem + '.backup-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns()) + target.suffix)
        shutil.copy2(target, backup)
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=target.parent, prefix=target.stem + '.', suffix='.tmp', delete=False) as f:
            temp = Path(f.name)
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, target)
    except BaseException:
        if temp is not None and temp.exists():
            temp.unlink()
        raise
    result = load_config(target)
    result['_backup_path'] = str(backup) if backup else None
    return result
