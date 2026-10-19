# Windows desktop build

Use a dedicated 64-bit Python 3.12 virtual environment. Model frameworks and
weights remain in separately configured Python environments; they are not
dependencies of the desktop build.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --index-url https://pypi.org/simple -r requirements-desktop.txt
.\.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py" -v
.\.venv\Scripts\python.exe build_windows.py --output-dir dist
```

The output is an onedir `dist/MonkeyPoseLive/MonkeyPoseLive.exe`, a portable ZIP,
and its SHA-256. Extract the whole folder before double-clicking the executable.
The native window uses the installed Microsoft Edge WebView2 Runtime. It does
not open a camera or load a pose model until the user starts a session. Optional
`Install-CurrentUser.ps1` copies a fresh version to the current user's Programs
folder and creates a Start Menu shortcut without administrator access.

The build copies an explicit public-source allowlist to an isolated staging
directory. For neutral build paths, set `--staging-root` to a writable,
ASCII-only build directory outside the source checkout.
It refuses existing output directories, and preserves build logs in staging.
Installed third-party license texts, the Python license, package versions and
file hashes are included in the portable app. Read `THIRD_PARTY_NOTICES.txt` and
`third-party-licenses/`, including the PyInstaller bootloader exception.

Headless packaged-executable validation never opens a camera or runs a model:

```powershell
.\dist\MonkeyPoseLive\MonkeyPoseLive.exe --self-test --report self-test.json
.\dist\MonkeyPoseLive\MonkeyPoseLive.exe --gui-smoke-test --report native-window-test.json
```

The second command loads and automatically closes the native WebView2 window.
The windowed EXE has no console, so use `--report` and inspect the process exit
code for automated checks.

For integration tests, `--serve --port 0 --report service.json` starts its own
random-port loopback service without opening a browser, camera or model. The
report contains its URL and a per-process control token. To close only that
instance, POST `{}` to the report's `shutdown_url`, with the header named by
`shutdown_header` and value `shutdown_token`. `--browser` is an explicit test
mode. These modes do not attach to or stop another localhost service.

`--config PATH` selects an external private configuration before importing the
model registry. Normally the current-user configuration is used. No machine
configuration, model weights, captured media or research results belong in a
release ZIP.

The frozen host's capture libraries are built for its bundled Python version.
External model workers are stored in `_internal/backend/` so their script import
path does not expose those host extension modules to a different model Python.

After an executable has been accepted, public Markdown documentation and license
texts can be refreshed without changing its binary. `refresh_release.py` checks
all frozen Python/web/example-config hashes, backs up the previous public text,
and requires a new ZIP filename. A source-code change always requires a new build.

```powershell
python refresh_release.py --app-dir dist/MonkeyPoseLive --archive release/MonkeyPoseLive-0.1.0-windows-x64-final.zip
```
