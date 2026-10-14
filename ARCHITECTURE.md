# Implementation contract — 0.4.0

## Data and capability boundaries

Python 3.11 x64, PySide6, OpenCV, NumPy and pinned MIT windows-capture==2.0.1.
Dependencies are project-local. No global settings, process injection or background input messages.
Images are owned contiguous uint8 BGR physical client pixels. Identity includes HWND, PID,
process creation time and executable; title is display information. Qt logical points use
explicit letterbox mapping. No live binding, session or input permission is persisted.

Frame preserves its positional API and adds capture_generation, source_sequence,
source_time_100ns, received_at and backend. captured_at is mapped to Python monotonic time
using QPC calibration. Receipt time is not source render time. Polling never refreshes
cached source timestamps/sequence. Identical pixels on new source frames are valid; new
source frames do not establish that a game is progressing.

CaptureStatus separates backend/interface support from actual current-target valid frame,
foreground, occlusion, minimization, freshness and reason. API support is not a permanent
per-game compatibility promise and is not an input authorization.

## Native capture and input

WindowsBackend retains list_windows/inspect/foreground/moving/unobscured/capture APIs.
set_capture_method, release_capture and close run on the worker; capture_status returns
a nonblocking diagnostic snapshot. inspect validates full identity, visibility/minimization,
client physical geometry/DPI and virtual-desktop containment.

WGC binds the exact HWND through windows-capture. Each generation owns one target and
request session. Callback work makes an owned BGR copy and replaces one bounded latest slot;
no GUI, recognition, activation or input runs in callbacks. Old generations cannot populate
replacement state. Both source sequence and timestamp must advance. Bounded waiting without
new source frames reports stale/unavailable; it never reissues cached images as fresh.

Client crop offsets come from ClientToScreen/GetClientRect and DWM extended frame bounds,
not guessed borders. Texture dimensions, offsets, crop containment and geometry before/after
must agree. Source geometry races, size/DPI changes, black or inconsistent frames are rejected.
The own-window corner-marker probe checks usable BGR alignment, not pixel-exact cropping
for every framed window or monitor arrangement.
HDR/color conversion, protected content and exclusive fullscreen are not broadly certified.

Auto mode may visibly select foreground GDI if WGC initialization is unavailable. GDI checks
foreground and unobscured client before/after screen BitBlt. A running WGC failure/stall cannot
silently become successful background detection with GDI. Minimization/closure/identity change
invalidate frames; the app does not restore or adjust a real game to mask incompatibility.

Sampling follows configured engine polling. Minimum WGC update interval is requested only on
supported Windows builds. Native readback cost on older builds is not bounded by Python polling;
measure it. Idle/manually paused worker releases capture sessions; temporary capture pauses
retain the current session. Queues and latest frame storage are bounded.

request_foreground(identity, cancelled) makes one normal SetForegroundWindow request after
identity, cancellation and physical-input checks. It does not synthesize Alt, use AttachThreadInput,
force topmost, raise privileges, change registry or restore previous focus/cursor. The engine
independently rechecks actual foreground; the API bool is only a request result.

WindowsInput.click remains the real input adapter: check cancellation/held input, one absolute
virtual-desktop move/down/up batch, release-only cleanup on partial insertion. Never retry down.
The final OS scene-check/input race remains. OS input acceptance is not target success.

## Round state and permission

Engine.configure cancels current authorization. Startup/load/preview/wizard completion are
unarmed and reminder-default; only explicit start creates a round. start(allow_activation=False)
binds optional activation to that round and target. This permission is not in Settings or JSON.
Pause/stop/changes cancel pending actions.

Read-only capture and final input eligibility are separate. Valid WGC frames can be recognized
while nonforeground/obscured; GDI remains foreground-only. Reminder mode requests one notification
without focus/input, even when the hotkey is unavailable. It never claims the user saw/heard it.

AUTO with a background candidate waits for manual return unless this round allows activation.
One activation attempt is consumed before the request. Refusal/mismatched actual foreground waits
without retries. Held keys/dragging prevent action. Return to foreground clears background counts
and coordinates, sets a source-time barrier and waits for stable geometry and new foreground frames.

_execute is the sole engine click path. It freshly recaptures and checks target, frame age/session/
generation, unique candidate, geometry, foreground, occlusion, hotkey, conflict, cancellation and
timeout. Atomic one-shot consumption occurs before native input. Failures never restore the budget.
Positive success markers must be absent before input and present on consecutive new frames
whose source times are after input completion.
Disappearance and API return values are not success. Missing marker means result unconfirmed.
Manual pause never auto-resumes; its time counts toward the original round timeout. Terminal elapsed
time is fixed; only a successful new manual start resets it.

## UI, persistence and shutdown

Dashboard contains daily controls; editor/advanced/diagnostics contain screenshots and technical
controls. The four-step wizard owns a temporary draft until an explicit successful save. Back/cancel/
failed capture preserve the previous active templates/path. Production matching supplies no-click
checks; unconfirmed setup may be saved with an honest label and without a success marker.

Profile names are readable. Unsaved edits require save/discard/cancel before switching. I/O uses
immutable request snapshots. Revision/request-ID/authorization epochs reject stale load/save/start
results. One durable profile completion slot prevents diagnostic noise dropping completion. Disk,
capture and resource waits stay off the GUI thread. Capture callbacks never dispatch GUI-dependent work.

ConfigStore retains schema 1 and v0.3 JSON plus templates compatibility. Optional capture_method=auto
and sound_enabled=true migrate only in memory until explicit save. JSON replacement remains atomic
after unique template files; different profiles do not overwrite each other's images. Path/size checks
remain; no runtime identity or permission is written.

Tray hide is explicit. Normal close stops and exits. Notifications do not activate/show the main
window automatically; clicking a notification never authorizes a round or input. Stop is reachable
on the dashboard and tray. Shutdown revokes queued actions, stops monitoring/capture, joins worker,
releases hotkey/tray, then single-instance lock and logs. GUI events continue during deferred close.
A vanished window is not proof of stopped threads. Native WGC stop releases the GIL and must run
outside callback locks; on stop exception a consumed control's is_finished is not proof of clean join.

## Evidence and delivery

Automated tests use fake input/activation. --capture-check targets only its own no-activate marker
windows and has no input path. Native capture, real activation, real input and game compatibility
are separate claims. The bundle includes the native extension/licenses and source snapshot;
build metadata records true dirty status, HEAD and snapshot hash. Publication requires explicit
user authorization; the current release was requested after the local implementation task.

Sources:
- https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture
- https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.direct3d11captureframe.systemrelativetime
- https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow
- https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
- https://docs.rs/windows-capture/2.0.1/windows_capture/
