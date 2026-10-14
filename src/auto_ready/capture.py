"""Narrow, lazily loaded adapter for the pinned Windows Graphics Capture wrapper.

The third-party frame exposes a view into a mapped native surface.  This module
copies each accepted BGRA8 frame before its callback returns and keeps only one
latest-frame slot, so neither native storage nor an unbounded queue escapes the
capture thread.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

_MAX_CAPTURE_DIMENSION = 16_384
_MAX_CAPTURE_PIXELS = 33_177_600


class CaptureAdapterError(RuntimeError):
    """The optional Windows capture wrapper could not provide a safe frame."""


def probe_wgc_support(*, force_unavailable: bool = False) -> tuple[bool, str]:
    """Check wrapper/OS capability without creating a capture item or touching focus.

    A positive result proves only that the pinned wrapper can be loaded on a
    supported Windows x64 release.  It says nothing about a particular HWND.
    """
    if force_unavailable:
        return False, "WGC support was intentionally reported unavailable"
    if sys.platform != "win32":
        return False, "Windows Graphics Capture is available only on Windows"
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        return False, "The installed windows-capture wheel supports Windows x64 only"
    version = getattr(sys, "getwindowsversion", lambda: None)()
    if version is None or int(getattr(version, "build", 0)) < 18_362:
        return False, "HWND capture requires Windows 10 version 1903 or newer"
    try:
        from windows_capture import WindowsCapture
    except Exception as exc:
        return False, f"The pinned windows-capture wrapper is unavailable: {exc}"
    if not callable(getattr(WindowsCapture, "start_free_threaded", None)):
        return False, "The loaded windows-capture wrapper lacks free-threaded capture support"
    return (
        True,
        "Windows x64 and the windows-capture wrapper are available; target support is unverified",
    )


@dataclass(frozen=True)
class CapturedSourceFrame:
    image: np.ndarray
    width: int
    height: int
    source_time_100ns: int
    source_sequence: int
    received_at: float


class WindowsCaptureAdapter:
    """One HWND-bound free-threaded capture session using windows-capture 2.0.1."""

    def __init__(
        self,
        hwnd: int,
        generation: int,
        session_id: str,
        *,
        capture_factory: Callable[..., object] | None = None,
        is_current: Callable[[str, int], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hwnd = int(hwnd)
        self.generation = int(generation)
        self.session_id = session_id
        self._capture_factory = capture_factory
        self._is_current = is_current or (lambda _session, _generation: True)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._changed = threading.Event()
        self._latest: CapturedSourceFrame | None = None
        self._source_sequence = 0
        self._last_source_time_100ns = -1
        self._last_delivered_sequence = 0
        self._last_error = ""
        self._closed = False
        self._active = True
        self._capture = None
        self._control = None

    @property
    def started(self) -> bool:
        return self._control is not None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def latest_source_time_100ns(self) -> int:
        with self._lock:
            return self._last_source_time_100ns

    def start(self) -> None:
        if not self._active:
            raise CaptureAdapterError("The capture session was already closed")
        factory = self._capture_factory
        if factory is None:
            # Import only at the point an HWND capture is requested.  This keeps
            # importing auto_ready.native safe when the optional wheel is absent.
            from windows_capture import WindowsCapture

            factory = WindowsCapture
        version = getattr(sys, "getwindowsversion", lambda: None)()
        build = int(getattr(version, "build", 0)) if version is not None else 0
        capture = factory(
            cursor_capture=False if build >= 19_041 else None,
            window_hwnd=self.hwnd,
            minimum_update_interval=100 if build >= 26_100 else None,
        )

        def on_frame_arrived(frame, _capture_control):
            self._on_frame_arrived(frame)

        def on_closed():
            self._on_closed()

        capture.event(on_frame_arrived)
        capture.event(on_closed)
        self._capture = capture
        self._control = capture.start_free_threaded()

    def _on_frame_arrived(self, frame) -> None:
        if not self._active or not self._is_current(self.session_id, self.generation):
            return
        try:
            width, height = int(frame.width), int(frame.height)
            raw_time = int(frame.timespan)
            if width <= 0 or height <= 0 or raw_time <= 0:
                raise CaptureAdapterError("WGC returned invalid frame dimensions or source time")
            if (
                width > _MAX_CAPTURE_DIMENSION
                or height > _MAX_CAPTURE_DIMENSION
                or width * height > _MAX_CAPTURE_PIXELS
            ):
                raise CaptureAdapterError("WGC frame dimensions exceed safe capture limits")
            source = np.asarray(frame.frame_buffer)
            if source.dtype != np.uint8:
                raise CaptureAdapterError(
                    "WGC frame is not BGRA8; HDR or non-8-bit color capture is unsupported"
                )
            if source.shape != (height, width, 4):
                raise CaptureAdapterError("WGC frame is not a width/height-matched BGRA8 image")
            # The wheel's Graphics Capture Frame contract is BGRA8.  Own BGR
            # bytes while the callback still holds the native mapped frame.
            image = np.array(source[:, :, :3], dtype=np.uint8, order="C", copy=True)
            if not image.flags.c_contiguous:
                raise CaptureAdapterError("WGC could not provide a contiguous frame copy")
            received_at = self._monotonic()
            with self._lock:
                if not self._active or not self._is_current(self.session_id, self.generation):
                    return
                if raw_time <= self._last_source_time_100ns:
                    return
                self._last_source_time_100ns = raw_time
                self._source_sequence += 1
                self._latest = CapturedSourceFrame(
                    image=image,
                    width=width,
                    height=height,
                    source_time_100ns=raw_time,
                    source_sequence=self._source_sequence,
                    received_at=received_at,
                )
                self._last_error = ""
                self._changed.set()
        except Exception as exc:
            with self._lock:
                if self._active and self._is_current(self.session_id, self.generation):
                    self._last_error = str(exc) or "WGC returned an invalid frame"
                    self._changed.set()

    def _on_closed(self) -> None:
        with self._lock:
            if self._active and self._is_current(self.session_id, self.generation):
                self._closed = True
                self._last_error = "The target window's WGC capture item closed"
                self._changed.set()

    def poll(self, timeout: float) -> CapturedSourceFrame | None:
        """Wait at most timeout seconds and consume one genuinely new source frame."""
        deadline = self._monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                if not self._active or not self._is_current(self.session_id, self.generation):
                    raise CaptureAdapterError("The capture generation is no longer active")
                latest = self._latest
                if latest is not None and latest.source_sequence > self._last_delivered_sequence:
                    self._latest = None
                    self._last_delivered_sequence = latest.source_sequence
                    self._changed.clear()
                    return latest
                if self._closed:
                    raise CaptureAdapterError(self._last_error)
                error = self._last_error
                self._changed.clear()
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                if error:
                    raise CaptureAdapterError(error)
                return None
            self._changed.wait(remaining)

    def close(self) -> None:
        """Invalidate callbacks and clear pixels before stopping outside the callback lock."""
        with self._lock:
            if not self._active:
                return
            self._active = False
            self._latest = None
            self._changed.set()
            control, self._control = self._control, None
            self._capture = None
        if control is None:
            return
        try:
            control.stop()
        except BaseException as exc:
            # The native control is consumed even when stop fails.  Do not retry
            # it or claim that the capture thread was joined.
            raise CaptureAdapterError(f"WGC teardown did not complete cleanly: {exc}") from exc
