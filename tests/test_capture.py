from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest
from test_native import FakeWindowAPI

from auto_ready import capture, native
from auto_ready.models import Identity


@dataclass
class FakeFrame:
    frame_buffer: np.ndarray
    timespan: int

    @property
    def width(self):
        return self.frame_buffer.shape[1]

    @property
    def height(self):
        return self.frame_buffer.shape[0]


class FakeControl:
    def __init__(self, *, stop_error=None):
        self.calls = []
        self.stop_error = stop_error

    def stop(self):
        self.calls.append("stop")
        if self.stop_error is not None:
            raise self.stop_error

    def wait(self):
        self.calls.append("wait")


class FakeWindowsCapture:
    def __init__(self, *, frame_on_start=None, start_error=None, **kwargs):
        self.kwargs = kwargs
        self.frame_on_start = frame_on_start
        self.start_error = start_error
        self.handlers = {}
        self.control = FakeControl()

    def event(self, handler):
        self.handlers[handler.__name__] = handler
        return handler

    def start_free_threaded(self):
        if self.start_error is not None:
            raise self.start_error
        if self.frame_on_start is not None:
            self.emit(self.frame_on_start)
        return self.control

    def emit(self, frame):
        self.handlers["on_frame_arrived"](frame, self.control)

    def close_item(self):
        self.handlers["on_closed"]()


class FakeCaptureFactory:
    def __init__(self, *, frame_on_start=None, start_error=None):
        self.frame_on_start = frame_on_start
        self.start_error = start_error
        self.captures = []

    def __call__(self, **kwargs):
        instance = FakeWindowsCapture(
            frame_on_start=self.frame_on_start,
            start_error=self.start_error,
            **kwargs,
        )
        self.captures.append(instance)
        return instance


def patterned_bgra(width=44, height=40):
    y, x = np.indices((height, width))
    image = np.empty((height, width, 4), dtype=np.uint8)
    image[:, :, 0] = x
    image[:, :, 1] = y
    image[:, :, 2] = (x + y) % 255
    image[:, :, 3] = 255
    return image


def current_qpc_100ns():
    return time.perf_counter_ns() // 100


def test_capture_adapter_owns_bgr_copy_and_uses_source_sequence_not_poll_sequence():
    factory = FakeCaptureFactory()
    adapter = capture.WindowsCaptureAdapter(
        123,
        4,
        "session",
        capture_factory=factory,
        monotonic=time.monotonic,
    )
    adapter.start()
    native_frame = FakeFrame(patterned_bgra(), current_qpc_100ns())
    factory.captures[0].emit(native_frame)
    first = adapter.poll(0)
    assert first is not None
    assert first.source_sequence == 1
    assert first.source_time_100ns == native_frame.timespan
    assert first.image.flags.c_contiguous
    assert first.image.dtype == np.uint8
    assert first.image[0, 0].tolist() == [0, 0, 0]

    # Same pixels are still a new frame when WGC supplies a later source time.
    next_frame = FakeFrame(patterned_bgra(), native_frame.timespan + 1)
    factory.captures[0].emit(next_frame)
    second = adapter.poll(0)
    assert second is not None
    assert second.source_sequence == 2
    assert np.array_equal(first.image, second.image)

    # Repeated/nonincreasing source timestamps never get a new sequence.
    factory.captures[0].emit(FakeFrame(patterned_bgra(), next_frame.timespan))
    assert adapter.poll(0) is None
    adapter.close()
    assert factory.captures[0].control.calls == ["stop"]


def test_capture_adapter_rejects_non_bgra8_and_drops_callbacks_after_generation_change():
    current = {"active": True}
    factory = FakeCaptureFactory()
    adapter = capture.WindowsCaptureAdapter(
        123,
        8,
        "old-session",
        capture_factory=factory,
        is_current=lambda _session, _generation: current["active"],
    )
    adapter.start()
    bad = FakeFrame(np.zeros((40, 44, 4), dtype=np.float16), current_qpc_100ns())
    factory.captures[0].emit(bad)
    with pytest.raises(capture.CaptureAdapterError, match="HDR or non-8-bit"):
        adapter.poll(0)

    current["active"] = False
    factory.captures[0].emit(FakeFrame(patterned_bgra(), current_qpc_100ns() + 1))
    with pytest.raises(capture.CaptureAdapterError, match="generation"):
        adapter.poll(0)
    adapter.close()


def test_capture_adapter_close_is_idempotent_and_rejects_late_callback():
    factory = FakeCaptureFactory()
    adapter = capture.WindowsCaptureAdapter(123, 2, "s", capture_factory=factory)
    adapter.start()
    old_capture = factory.captures[0]
    adapter.close()
    adapter.close()
    old_capture.emit(FakeFrame(patterned_bgra(), current_qpc_100ns()))
    assert old_capture.control.calls == ["stop"]
    assert adapter.latest_source_time_100ns == -1


@pytest.mark.parametrize(
    ("build", "cursor_capture", "minimum_update_interval"),
    [(18_000, None, None), (19_041, False, None), (26_100, False, 100)],
)
def test_capture_adapter_only_passes_version_supported_options(
    monkeypatch, build, cursor_capture, minimum_update_interval
):
    monkeypatch.setattr(
        capture.sys, "getwindowsversion", lambda: SimpleNamespace(build=build), raising=False
    )
    factory = FakeCaptureFactory()
    adapter = capture.WindowsCaptureAdapter(123, 3, "s", capture_factory=factory)
    adapter.start()

    kwargs = factory.captures[0].kwargs
    assert kwargs["cursor_capture"] is cursor_capture
    assert kwargs["minimum_update_interval"] == minimum_update_interval
    adapter.close()


def test_backend_wgc_crops_client_from_physical_dwm_bounds_and_reports_background_frame():
    api = FakeWindowAPI()
    api.foreground_hwnd = 2
    source = FakeFrame(patterned_bgra(), current_qpc_100ns())
    factory = FakeCaptureFactory(frame_on_start=source)
    backend = native.WindowsBackend(_api=api, capture_factory=factory)
    backend.set_capture_method("wgc")
    identity = Identity(1, 44, api.creation_time, api.process_path)

    frame = backend.capture(identity, "round-1")

    assert frame.backend == "wgc"
    assert frame.capture_generation == backend.capture_status().generation
    assert frame.source_sequence == 1
    assert frame.source_time_100ns == source.timespan
    assert frame.received_at is not None
    assert frame.geometry.left < 0
    assert frame.image.shape == (30, 40, 3)
    assert frame.image.flags.c_contiguous
    assert frame.image[0, 0].tolist() == [2, 3, 5]
    status = backend.capture_status()
    assert status.backend == "wgc"
    assert status.valid_frame and status.fresh
    assert status.background_supported
    assert status.foreground is False
    assert status.obscured is False
    assert status.minimized is False
    assert status.source_sequence == 1
    assert factory.captures[0].kwargs["window_hwnd"] == identity.hwnd
    assert "dirty_region" not in factory.captures[0].kwargs
    assert "secondary_window" not in factory.captures[0].kwargs

    backend.close()
    assert factory.captures[0].control.calls == ["stop"]


def test_backend_wgc_stall_is_not_reissued_or_silently_replaced_by_gdi():
    api = FakeWindowAPI()
    factory = FakeCaptureFactory()
    backend = native.WindowsBackend(_api=api, capture_factory=factory)
    backend.set_capture_method("wgc")
    identity = Identity(1, 44, api.creation_time, api.process_path)

    with pytest.raises(native.CapturePendingError, match="新的源帧"):
        backend.capture(identity, "round-1")
    status = backend.capture_status()
    assert status.backend == "wgc"
    assert not status.valid_frame and not status.fresh
    assert "gdi" not in status.reason.casefold()
    assert len(factory.captures) == 1
    backend.close()


def test_backend_rejects_replaced_hwnd_and_tears_down_session():
    api = FakeWindowAPI()
    source = capture.CapturedSourceFrame(
        patterned_bgra()[:, :, :3],
        44,
        40,
        current_qpc_100ns(),
        1,
        time.monotonic(),
    )

    class LateMutatingAdapter:
        def __init__(self):
            self.closed = False
            self.close_calls = 0

        @property
        def latest_source_time_100ns(self):
            return source.source_time_100ns

        def start(self):
            pass

        def poll(self, _timeout):
            api.creation_time += 1
            return source

        def close(self):
            self.close_calls += 1
            self.closed = True

    adapters = []

    def make_adapter(*_args, **_kwargs):
        adapter = LateMutatingAdapter()
        adapters.append(adapter)
        return adapter

    backend = native.WindowsBackend(_api=api, capture_adapter_factory=make_adapter)
    backend.set_capture_method("wgc")
    identity = Identity(1, 44, api.creation_time, api.process_path)

    # The HWND stays numerically equal while the process identity changes.
    with pytest.raises(native.SafetyError, match="identity changed"):
        backend.capture(identity, "round-1")

    assert backend.capture_status().valid_frame is False
    assert adapters[0].close_calls == 1
    with pytest.raises(native.SafetyError, match="identity changed"):
        backend.capture(identity, "round-1")


def test_auto_only_falls_back_when_wgc_setup_is_unavailable_and_explicit_wgc_does_not():
    class FailingAdapter:
        def __init__(self, *_args, **_kwargs):
            self.closed = False

        def start(self):
            raise ImportError("simulated missing WGC extension")

        def close(self):
            self.closed = True

    api = FakeWindowAPI()
    identity = Identity(1, 44, api.creation_time, api.process_path)
    auto = native.WindowsBackend(_api=api, capture_adapter_factory=FailingAdapter)
    frame = auto.capture(identity, "auto")
    assert frame.backend == "gdi"
    assert "WGC 初始化不可用" in auto.capture_status().reason
    assert "simulated missing WGC extension" in auto.capture_status().reason
    auto.close()

    explicit = native.WindowsBackend(_api=FakeWindowAPI(), capture_adapter_factory=FailingAdapter)
    explicit.set_capture_method("wgc")
    with pytest.raises(native.SafetyError, match="simulated missing WGC extension"):
        explicit.capture(identity, "explicit")
    assert explicit.capture_status().backend == "wgc"
    assert not explicit.capture_status().valid_frame
    explicit.close()


def test_support_probe_can_report_unavailable_without_capture_or_focus():
    available, detail = capture.probe_wgc_support(force_unavailable=True)
    assert available is False
    assert "intentionally" in detail

    code = """
import builtins, sys
sys.path.insert(0, 'src')
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'windows_capture':
        raise ModuleNotFoundError('simulated missing wheel')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
import auto_ready.native
import auto_ready.capture
print('safe-import')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert "safe-import" in completed.stdout


def test_minimized_wgc_target_pauses_without_releasing_session_and_resumes_new_frames():
    api = FakeWindowAPI()
    first_source = FakeFrame(patterned_bgra(), current_qpc_100ns())
    factory = FakeCaptureFactory(frame_on_start=first_source)
    backend = native.WindowsBackend(_api=api, capture_factory=factory)
    backend.set_capture_method("wgc")
    identity = Identity(1, 44, api.creation_time, api.process_path)

    first = backend.capture(identity, "round-1")
    generation = first.capture_generation
    api.minimized = True
    with pytest.raises(native.CapturePendingError, match="minimized"):
        backend.capture(identity, "round-1")
    status = backend.capture_status()
    assert status.minimized is True
    assert not status.valid_frame and not status.fresh
    assert status.generation == generation
    assert factory.captures[0].control.calls == []

    api.minimized = False
    factory.captures[0].emit(FakeFrame(patterned_bgra(), current_qpc_100ns()))
    resumed = backend.capture(identity, "round-1")
    assert resumed.capture_generation == generation
    assert resumed.source_sequence == 2
    assert resumed.backend == "wgc"
    backend.close()
    assert factory.captures[0].control.calls == ["stop"]


def test_minimize_racing_wgc_start_keeps_adapter_for_restore():
    api = FakeWindowAPI()
    source = capture.CapturedSourceFrame(
        patterned_bgra()[:, :, :3],
        44,
        40,
        current_qpc_100ns(),
        1,
        time.monotonic(),
    )

    class MinimizeOnStartAdapter:
        closed = False

        def start(self):
            api.minimized = True

        def poll(self, _timeout):
            return source

        def close(self):
            self.closed = True

    adapters = []

    def make_adapter(*_args, **_kwargs):
        adapter = MinimizeOnStartAdapter()
        adapters.append(adapter)
        return adapter

    backend = native.WindowsBackend(_api=api, capture_adapter_factory=make_adapter)
    backend.set_capture_method("wgc")
    identity = Identity(1, 44, api.creation_time, api.process_path)
    with pytest.raises(native.CapturePendingError, match="minimized"):
        backend.capture(identity, "round-1")
    generation = backend.capture_status().generation
    assert backend.capture_status().minimized is True
    assert adapters[0].closed is False

    api.minimized = False
    frame = backend.capture(identity, "round-1")
    assert frame.capture_generation == generation
    assert frame.source_sequence == 1
    backend.close()
    assert adapters[0].closed is True


def test_capture_callback_rejects_oversized_dimensions_before_reading_native_surface():
    class OversizedFrame:
        width = 16_385
        height = 1
        timespan = current_qpc_100ns()

        @property
        def frame_buffer(self):
            raise AssertionError("oversized native surface must not be read")

    factory = FakeCaptureFactory()
    adapter = capture.WindowsCaptureAdapter(123, 1, "s", capture_factory=factory)
    adapter.start()
    factory.captures[0].emit(OversizedFrame())
    with pytest.raises(capture.CaptureAdapterError, match="safe capture limits"):
        adapter.poll(0)
    adapter.close()


def test_stop_failure_is_reported_once_without_followup_wait_or_stop():
    factory = FakeCaptureFactory()
    adapter = capture.WindowsCaptureAdapter(123, 1, "s", capture_factory=factory)
    adapter.start()
    control = factory.captures[0].control
    control.stop_error = RuntimeError("native stop failed")
    with pytest.raises(capture.CaptureAdapterError, match="native stop failed"):
        adapter.close()
    adapter.close()
    assert control.calls == ["stop"]
