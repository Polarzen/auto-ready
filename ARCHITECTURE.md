# Implementation contract

Python 3.11+, PySide6, OpenCV-headless, numpy; Win32 via ctypes only. No global modifications.
Data classes and coordinate conversions are in `models.py`; images are uint8 BGR contiguous
physical-pixel client screenshots. Frame times use `time.monotonic()`. Identity equality excludes
title but includes HWND, PID, executable path and process creation time. No persisted HWND binding.

`native.py` public contract:
- `enable_dpi_awareness()` before QApplication.
- `WindowsBackend.list_windows() -> list[Identity]`, `inspect(identity) -> Geometry` validates
  identity, visible, not minimized, client entirely inside virtual desktop; throws `SafetyError`.
- `foreground(identity) -> bool`, `moving(identity) -> bool`, `unobscured(identity, rect: Rect,
  geometry: Geometry) -> bool` rect in client pixels, conservative above-target window intersection.
- `capture(identity, session_id) -> Frame`: inspect before/after GDI screen BitBlt of client only;
  discard changes, always monotonic unique sequence, release GDI resources in finally.
- `WindowsInput.click(point: tuple[int,int], cancelled: threading.Event) -> None`: real SendInput
  absolute virtual desktop move + one down/up; release cleanup for partial/exception; never retry
  down and never restore cursor; abort if cancelled or physical mouse/modifier currently down.
- `GlobalHotkey(spec, callback)`: `start() -> bool`, `.error: str`, `.available: bool`, `close()`;
  dedicated bounded message loop RegisterHotKey, callback directly signals engine cancellation.
- `SingleInstance.acquire() -> bool`, `close()` named per-session mutex.
- Imports on non-Windows permitted; constructors fail with actionable error when unsupported.

`config.py`: `ConfigStore(root: Path | None = None)`, `.root`, `save(settings, region, button,
success)`, `load() -> tuple[Settings, Region, Template | None, Template | None]`.
User-writable LOCALAPPDATA/auto-ready default; only explicit save persists template PNGs + JSON,
atomic config replacement; no mode/session/identity persistence. Validate corrupt files and
path traversal; versioned schema. `load_template(path, geometry) -> Template` imports image with
current geometry reference and validates. `setup_logging(root) -> None` rotating logs no screenshots.
`sound.py`: `notify()` asynchronous system sound and no imported Windows dependency on Linux.

`engine.py` public contract (Root owns): `Engine(backend, input_adapter, notifier, clock=time.monotonic)`.
`configure(identity, button, success=None, region=Region(), settings=Settings(), mode=Mode.REMIND)`
cancels round; `set_hotkey_available(bool)`; `start() -> str` creates manually authorized round;
`pause()`, `resume()`, `stop(reason='已停止')`, `close()`; `tick() -> Snapshot`; `snapshot() -> Snapshot`;
`preview() -> Snapshot` captures/recognizes with no input, cancels current round first.
All configure/start/control calls thread-safe. One round consumes eligibility before real input;
old sessions/callbacks rejected, final fresh recapture + all checks in centralized private executor.
Single long-lived worker calls tick synchronously (no work queue). UI can poll latest snapshot.
Manual pause never auto resumes; move/foreground temporary block resets confirmation but resumes;
resize/DPI blocks manual recapture. terminal states never restart. Success needs configured positive
template absent before click and present on two new frames; disappearance never counts.

`recognition.py` Root owns: `validate_template(image)` raises ValueError;
`match(frame, template, region, settings) -> Detection` invalid/blank/multiple candidates reject.

`ui.py`: `MainWindow(backend=None, input_adapter=None, store=None, hotkey_factory=None)` defaults
native; owns Engine, worker, hotkey, controls and capture preview. Native target must be selected
each launch. Mode default remind, disarmed. Selection static preview via optional 3s capture delay
so user can foreground target; no focus manipulation. Rectangle widget `ImagePreview` mapping
logical points through models helper, signals selection Rect; template/search/success selection.
Target, settings, region, template, mode or hotkey changes always cancel round. Show candidates,
raw scores, geometry/DPI, rejection reason. Capture/preview/monitor IO outside GUI thread via one
bounded worker; latest result only. start/pause/resume/stop/audio/config/import actions.
Do not auto save screenshots. Shutdown immediately cancels then joins worker/releases hotkey.

`simulator.py`: separate Qt window supports delay, distractor/duplicate, success/no-success/expiry,
move/resize normal OS window. CLI `python -m auto_ready --simulator`. Never automates real input
by default. An opt-in real input script can only target this app's simulator.

All agents leaf-only. Each owns their assigned files only; no Git writes, commits, pushes, global
configuration, actual game operation or real mouse input during implementation. Tests fake input.
