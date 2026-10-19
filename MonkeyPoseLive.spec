# PyInstaller onedir recipe; run through build_windows.py for isolated staging.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH)
data_names = ['worker.py', 'models.py', 'runtime_config.py', 'adaptation_helpers.py']
# External model Python must not see the host's cv2/numpy extensions on sys.path.
datas = [(str(root / name), 'backend') for name in data_names]
datas += [(str(root / 'config.example.json'), '.')]
datas += [(str(root / 'web'), 'web')]
datas += collect_data_files('webview')
datas += copy_metadata('pywebview')

a = Analysis([str(root / 'desktop.py')], pathex=[str(root)], binaries=[], datas=datas,
             hiddenimports=['cv2', 'numpy', 'webview', 'webview.platforms.winforms',
                            'webview.platforms.edgechromium', 'clr', 'pythonnet'],
             excludes=['torch', 'torchvision', 'tensorflow', 'deeplabcut', 'mmpose',
                       'mmcv', 'mmdet', 'ultralytics', 'transformers', 'PyQt5', 'PyQt6',
                       'PySide2', 'PySide6', 'cefpython3', 'matplotlib', 'IPython'],
             hookspath=[], runtime_hooks=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='MonkeyPoseLive',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, uac_admin=False,
          contents_directory='_internal')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MonkeyPoseLive')
