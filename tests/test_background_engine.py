import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import numpy as np
import pytest

from auto_ready.engine import Engine
from auto_ready.models import (
    CaptureStatus,
    Frame,
    Geometry,
    Identity,
    Mode,
    Settings,
    State,
    Template,
)


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, amount=0.2):
        self.now += amount


def pattern(seed=11):
    return np.random.default_rng(seed).integers(0, 256, (24, 60, 3), dtype=np.uint8)


class BackgroundBackend:
    def __init__(self, clock, backend="wgc"):
        self.clock = clock
        self.identity = Identity(101, 202, 303, "mock-game.exe", "Mock game")
        self.geometry = Geometry(-320, 40, 320, 240, 96)
        self.capture_backend = backend
        self.foreground_value = False
        self.clear = False
        self.moving_value = False
        self.minimized = False
        self.conflict = False
        self.generation = 0
        self.sequence = 0
        self.image = np.full((240, 320, 3), 35, dtype=np.uint8)
        self.image[80:104, 120:180] = pattern()
        self.frame_transform = lambda frame: frame
        self.status_generation = None
        self.status_supported = True
        self.status_valid_frame = True
        self.status_fresh = True
        self.status_reason = "fake WGC frame status"
        self.capture_error = None
        self.on_capture_error = None
        self.capture_count = 0
        self.on_capture = None
        self.request_count = 0
        self.api_result = True
        self.foreground_on_request = False
        self.request_hook = None
        self.activation_sequence = None

    def inspect(self, identity):
        if identity != self.identity:
            raise RuntimeError("target identity changed")
        return self.geometry

    def foreground(self, identity):
        if identity != self.identity:
            return False
        return self.foreground_value

    def moving(self, identity):
        return self.moving_value

    def unobscured(self, identity, rect, geometry):
        return self.clear

    def input_conflict(self):
        return self.conflict

    def capture_status(self):
        return CaptureStatus(
            backend=self.capture_backend,
            supported=self.status_supported,
            valid_frame=self.status_valid_frame,
            background_supported=self.capture_backend == "wgc",
            foreground=self.foreground_value,
            obscured=not self.clear,
            minimized=self.minimized,
            fresh=self.status_fresh,
            generation=(
                self.generation if self.status_generation is None else self.status_generation
            ),
            source_sequence=self.sequence,
            received_at=self.clock(),
            reason=self.status_reason,
        )

    def set_capture_method(self, method):
        self.applied_method = method

    def capture(self, identity, session_id):
        self.capture_count += 1
        if self.capture_error is not None:
            if self.on_capture_error is not None:
                self.on_capture_error(self)
            raise self.capture_error
        self.sequence += 1
        self.clock.advance(0.001)
        captured_at = self.clock()
        frame = Frame(
            self.image.copy(),
            self.geometry,
            self.identity,
            captured_at,
            session_id,
            self.sequence,
            capture_generation=self.generation,
            source_sequence=self.sequence if self.capture_backend == "wgc" else None,
            source_time_100ns=(
                round(captured_at * 10_000_000) if self.capture_backend == "wgc" else None
            ),
            received_at=captured_at if self.capture_backend == "wgc" else None,
            backend=self.capture_backend,
        )
        if self.on_capture is not None:
            self.on_capture(self, frame)
        return self.frame_transform(frame)

    def request_foreground(self, identity, cancelled):
        self.request_count += 1
        self.activation_sequence = self.sequence
        if self.request_hook is not None:
            self.request_hook(self, cancelled)
        if self.foreground_on_request:
            self.foreground_value = True
            self.clear = True
        return self.api_result


class FakeInput:
    def __init__(self):
        self.points = []
        self.error = False

    def click(self, point, cancelled):
        if cancelled.is_set():
            return
        self.points.append(point)
        if self.error:
            raise OSError("fake click failed")


def make_rig(*, mode=Mode.AUTO, backend="wgc", settings=None, hotkey=True):
    clock = Clock()
    native = BackgroundBackend(clock, backend)
    adapter = FakeInput()
    notifications = []
    engine = Engine(native, adapter, lambda: notifications.append("notified"), clock)
    engine.configure(
        native.identity,
        Template(pattern(), (320, 240), 96),
        settings=settings or Settings(),
        mode=mode,
    )
    engine.set_hotkey_available(hotkey)
    return engine, native, adapter, notifications, clock


def tick_many(engine, clock, count=15, step=0.2):
    snapshot = engine.snapshot()
    for _ in range(count):
        clock.advance(step)
        snapshot = engine.tick()
        if snapshot.state in {
            State.DISARMED,
            State.REMINDED,
            State.SUCCESS,
            State.UNCONFIRMED,
            State.ERROR,
        }:
            break
    return snapshot


def test_reminder_recognizes_obscured_nonforeground_wgc_without_input_or_activation():
    engine, backend, adapter, notifications, clock = make_rig(
        mode=Mode.REMIND, hotkey=False
    )
    engine.start(allow_activation=True)

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.REMINDED
    assert snapshot.foreground is False
    assert snapshot.frame.backend == "wgc"
    assert notifications == ["notified"]
    assert adapter.points == []
    assert backend.request_count == 0
    elapsed = snapshot.elapsed
    clock.advance(4)
    assert engine.snapshot().state == State.REMINDED
    assert engine.snapshot().elapsed == elapsed


def test_auto_without_activation_notifies_then_manual_return_requires_new_frames():
    engine, backend, adapter, notifications, clock = make_rig()
    engine.start()

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.WAITING_FOREGROUND
    assert snapshot.activation_allowed is False
    assert snapshot.activation_attempted is False
    assert snapshot.count == 0
    assert snapshot.detection is None
    assert notifications == ["notified"]
    assert backend.request_count == 0
    assert adapter.points == []

    old_sequence = backend.sequence
    backend.foreground_value = True
    backend.clear = True
    clock.advance()
    assert engine.tick().state == State.MONITORING
    assert adapter.points == []

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.UNCONFIRMED
    assert len(adapter.points) == 1
    assert notifications == ["notified"]
    assert backend.sequence > old_sequence


@pytest.mark.parametrize("api_result", [False, True])
def test_activation_api_return_never_substitutes_for_foreground_evidence(api_result):
    engine, backend, adapter, notifications, clock = make_rig()
    backend.api_result = api_result
    backend.foreground_on_request = False
    engine.start(allow_activation=True)

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.WAITING_FOREGROUND
    assert snapshot.activation_allowed is True
    assert snapshot.activation_attempted is True
    assert backend.request_count == 1
    assert adapter.points == []
    assert notifications == ["notified"]
    tick_many(engine, clock, 20)
    assert backend.request_count == 1
    assert adapter.points == []


def test_activation_success_requires_fresh_foreground_reconfirmation():
    engine, backend, adapter, _notifications, clock = make_rig()
    events = []
    engine.set_observer(lambda event, **fields: events.append((event, fields)))
    backend.api_result = False  # The OS return value is deliberately not evidence.
    backend.foreground_on_request = True
    engine.start(allow_activation=True)

    snapshot = tick_many(engine, clock)

    attempt = next(fields for event, fields in events if event == "input_attempt")
    assert backend.request_count == 1
    assert snapshot.state == State.UNCONFIRMED
    assert snapshot.activation_attempted is True
    assert len(adapter.points) == 1
    assert attempt["frame_sequence"] > backend.activation_sequence


@pytest.mark.parametrize("action", ["stop", "pause", "configure", "timeout"])
def test_control_during_blocked_activation_prevents_followup_input(action):
    settings = Settings(round_timeout=5) if action == "timeout" else Settings()
    engine, backend, adapter, notifications, clock = make_rig(settings=settings)
    entered, release = threading.Event(), threading.Event()

    def block_request(_backend, _cancelled):
        entered.set()
        assert release.wait(2)

    backend.request_hook = block_request
    engine.start(allow_activation=True)

    def run_until_activation():
        for _ in range(30):
            clock.advance()
            engine.tick()
            if entered.is_set():
                return

    worker = threading.Thread(target=run_until_activation)
    worker.start()
    assert entered.wait(2)

    control = None
    if action != "timeout":
        def apply_control():
            if action == "configure":
                engine.configure(backend.identity, engine.button, mode=Mode.AUTO)
            else:
                getattr(engine, action)()

        control = threading.Thread(target=apply_control)
        control.start()
        event = engine._pause_requested if action == "pause" else engine._cancel
        assert event.wait(2)
    else:
        clock.advance(6)

    release.set()
    worker.join(2)
    if control is not None:
        control.join(2)

    assert not worker.is_alive()
    assert control is None or not control.is_alive()
    assert backend.request_count == 1
    assert adapter.points == []
    assert notifications == ["notified"]
    if action == "stop" or action == "configure":
        assert engine.snapshot().state == State.DISARMED
    elif action == "pause":
        assert engine.snapshot().state == State.PAUSED
        assert engine.snapshot().activation_allowed is False
    else:
        assert engine.snapshot().state == State.UNCONFIRMED


def test_pause_is_visible_to_native_activation_last_gate():
    engine, backend, adapter, _notifications, clock = make_rig()
    entered, release = threading.Event(), threading.Event()
    cancellation_state = []

    def native_last_gate(_backend, cancelled):
        entered.set()
        assert release.wait(2)
        cancellation_state.append(cancelled.is_set())

    backend.request_hook = native_last_gate
    engine.start(allow_activation=True)

    worker = threading.Thread(target=lambda: tick_many(engine, clock, 30))
    worker.start()
    assert entered.wait(2)
    pauser = threading.Thread(target=engine.pause)
    pauser.start()
    assert engine._pause_requested.wait(2)
    release.set()
    worker.join(2)
    pauser.join(2)

    assert not worker.is_alive()
    assert not pauser.is_alive()
    assert cancellation_state == [True]
    assert engine.snapshot().state == State.PAUSED
    assert engine.snapshot().activation_attempted is True
    assert adapter.points == []


def test_missing_wgc_source_pauses_and_recovers_without_resetting_session():
    engine, backend, _adapter, _notifications, clock = make_rig()
    engine.start()
    backend.status_valid_frame = False
    backend.status_fresh = False
    backend.status_reason = "尚未收到新的源帧"
    backend.capture_error = RuntimeError("no fresh WGC source frame")

    clock.advance(1)
    paused = engine.tick()

    assert paused.state == State.PAUSED
    assert paused.manual_paused is False
    assert paused.capture_status.reason == "尚未收到新的源帧"
    assert backend.capture_count == 1
    assert engine._capture_generation is None

    backend.capture_error = None
    backend.status_valid_frame = True
    backend.status_fresh = True
    clock.advance(0.2)
    resumed = engine.tick()

    assert resumed.state == State.BACKGROUND
    assert resumed.frame is not None
    assert resumed.manual_paused is False
    assert backend.capture_count == 2


def test_missing_wgc_source_with_lost_identity_is_terminal():
    engine, backend, _adapter, _notifications, clock = make_rig()
    engine.start()
    backend.status_valid_frame = False
    backend.status_fresh = False
    backend.status_reason = "尚未收到新的源帧"
    backend.capture_error = RuntimeError("no fresh WGC source frame")
    backend.identity = replace(backend.identity, pid=999)

    clock.advance(1)
    snapshot = engine.tick()

    assert snapshot.state == State.ERROR
    assert "目标窗口或身份不可用" in snapshot.reason
    assert snapshot.manual_paused is False


def test_missing_wgc_source_with_changed_capture_generation_is_terminal():
    engine, backend, _adapter, _notifications, clock = make_rig()
    engine.start()
    clock.advance(1)
    assert engine.tick().frame is not None
    backend.status_valid_frame = False
    backend.status_fresh = False
    backend.status_reason = "尚未收到新的源帧"
    backend.status_generation = backend.generation + 1
    backend.capture_error = RuntimeError("no fresh WGC source frame")

    clock.advance(0.2)
    snapshot = engine.tick()

    assert snapshot.state == State.ERROR
    assert "捕获会话已重建" in snapshot.reason


def test_auto_wgc_fallback_to_gdi_waits_for_manual_foreground_and_fresh_frame():
    engine, backend, adapter, _notifications, clock = make_rig()
    def fallback_to_gdi(native):
        native.capture_backend = "gdi"
        native.status_supported = False
        native.status_valid_frame = False
        native.status_fresh = False
        native.status_reason = (
            "WGC 初始化不可用；GDI 前台兼容捕获要求目标窗口位于前台"
        )

    backend.capture_error = RuntimeError(
        "GDI 前台兼容捕获要求目标窗口位于前台"
    )
    backend.on_capture_error = fallback_to_gdi
    engine.start(allow_activation=True)

    waiting = tick_many(engine, clock, 10)

    assert waiting.state == State.WAITING_FOREGROUND
    assert waiting.foreground is False
    assert waiting.count == 0
    assert waiting.detection is None
    assert backend.request_count == 0
    assert adapter.points == []

    backend.capture_error = None
    backend.on_capture_error = None
    backend.status_supported = True
    backend.status_valid_frame = True
    backend.status_fresh = True
    backend.foreground_value = True
    backend.clear = True
    clock.advance()
    assert engine.tick().state == State.MONITORING

    snapshot = tick_many(engine, clock, 15)

    assert snapshot.state == State.UNCONFIRMED
    assert snapshot.frame.backend == "gdi"
    assert backend.request_count == 0
    assert len(adapter.points) == 1


def test_unrelated_gdi_capture_error_is_not_treated_as_foreground_wait():
    engine, backend, adapter, _notifications, clock = make_rig()
    def fallback_to_gdi(native):
        native.capture_backend = "gdi"
        native.status_supported = False
        native.status_valid_frame = False
        native.status_fresh = False
        native.status_reason = (
            "Previous GDI capture asked for foreground; this failure is unrelated"
        )

    backend.capture_error = RuntimeError("GDI capture API failed: device context unavailable")
    backend.on_capture_error = fallback_to_gdi
    engine.start()

    snapshot = tick_many(engine, clock, 10)

    assert snapshot.state == State.ERROR
    assert "GDI capture API failed" in snapshot.reason
    assert adapter.points == []


@pytest.mark.parametrize("target_change", ["identity", "size"])
def test_gdi_foreground_failure_remains_terminal_for_incompatible_target(target_change):
    engine, backend, adapter, _notifications, clock = make_rig()

    def fallback_and_invalidate_target(native):
        native.capture_backend = "gdi"
        native.status_supported = False
        native.status_valid_frame = False
        native.status_fresh = False
        native.status_reason = "GDI 前台兼容捕获要求目标窗口位于前台"
        if target_change == "identity":
            native.identity = replace(native.identity, pid=native.identity.pid + 1)
        else:
            native.geometry = replace(native.geometry, width=native.geometry.width + 1)

    backend.capture_error = RuntimeError("GDI 前台兼容捕获要求目标窗口位于前台")
    backend.on_capture_error = fallback_and_invalidate_target
    engine.start()

    snapshot = tick_many(engine, clock, 10)

    assert snapshot.state == State.ERROR
    assert adapter.points == []


def test_resume_rebuild_uses_new_generation_and_rejects_prebarrier_frame():
    engine, backend, adapter, _notifications, clock = make_rig(
        settings=Settings(stable_seconds=0.2)
    )
    success = Template(pattern(99), (320, 240), 96, "成功")
    engine.configure(
        backend.identity,
        engine.button,
        success,
        settings=Settings(stable_seconds=0.2),
        mode=Mode.AUTO,
    )
    backend.foreground_on_request = True
    engine.start(allow_activation=True)

    for _ in range(30):
        clock.advance()
        engine.tick()
        if engine.snapshot().attempted:
            break
    assert engine.snapshot().state == State.VERIFYING
    assert backend.request_count == 1
    assert len(adapter.points) == 1

    engine.pause()
    assert engine.snapshot().manual_paused is True
    backend.generation += 1  # UI worker released and recreated the native capture session.
    backend.image[150:174, 20:80] = pattern(99)
    attempted_at = engine._attempted_at
    completed_at = engine._input_completed_at
    engine.resume()
    barrier = engine._foreground_barrier
    assert barrier == clock()

    def prebarrier_frame(frame):
        backend.frame_transform = lambda later: later
        return replace(frame, captured_at=barrier)

    backend.frame_transform = prebarrier_frame
    clock.advance(0.1)
    engine.tick()  # Geometry must restabilize after the manual release.
    clock.advance(0.22)
    snapshot = engine.tick()

    assert snapshot.state == State.VERIFYING, snapshot.reason
    assert snapshot.manual_paused is False
    assert snapshot.attempted is True
    assert engine._success_count == 0
    assert engine._capture_generation == backend.generation
    assert engine._attempted_at == attempted_at
    assert engine._input_completed_at == completed_at
    assert backend.request_count == 1
    assert len(adapter.points) == 1


def test_physical_input_conflict_defers_activation_until_clean():
    engine, backend, adapter, _notifications, clock = make_rig()
    backend.conflict = True
    backend.foreground_on_request = True
    engine.start(allow_activation=True)

    snapshot = tick_many(engine, clock)
    assert snapshot.state in {State.BACKGROUND, State.WAITING_FOREGROUND}
    assert backend.request_count == 0
    assert adapter.points == []

    backend.conflict = False
    snapshot = tick_many(engine, clock, 20)
    assert backend.request_count == 1
    assert len(adapter.points) == 1
    assert snapshot.state == State.UNCONFIRMED


def test_window_move_blocks_background_activation():
    engine, backend, adapter, _notifications, clock = make_rig()
    backend.moving_value = True
    engine.start(allow_activation=True)

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.PAUSED
    assert backend.request_count == 0
    assert adapter.points == []


def test_physical_input_conflict_blocks_foreground_click_until_released():
    engine, backend, adapter, _notifications, clock = make_rig()
    backend.foreground_value = True
    backend.clear = True
    backend.conflict = True
    engine.start()

    snapshot = tick_many(engine, clock)
    assert snapshot.state in {State.BACKGROUND, State.CONFIRMING, State.PAUSED}
    assert adapter.points == []

    backend.conflict = False
    snapshot = tick_many(engine, clock, 20)
    assert snapshot.state == State.UNCONFIRMED
    assert len(adapter.points) == 1


def test_background_gdi_frames_are_never_used_as_background_recognition():
    engine, backend, adapter, notifications, clock = make_rig(backend="gdi")
    engine.start(allow_activation=True)

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.PAUSED
    assert backend.sequence == 0
    assert adapter.points == []
    assert notifications == []
    assert backend.request_count == 0


def test_missing_emergency_hotkey_does_not_disable_reminder_mode():
    engine, _backend, adapter, notifications, clock = make_rig(
        mode=Mode.REMIND, hotkey=False
    )
    engine.start()

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.REMINDED
    assert notifications == ["notified"]
    assert adapter.points == []


def test_pause_revokes_activation_and_resume_accepts_new_generation_baseline():
    engine, backend, adapter, _notifications, clock = make_rig()
    engine.start(allow_activation=True)
    engine.pause()
    engine.resume()
    backend.generation += 1

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.WAITING_FOREGROUND
    assert snapshot.activation_allowed is False
    assert snapshot.activation_attempted is False
    assert backend.request_count == 0
    assert adapter.points == []


@pytest.mark.parametrize(
    "transform",
    [
        lambda frame: replace(frame, source_sequence=1),
        lambda frame: replace(frame, source_time_100ns=0),
        lambda frame: replace(frame, received_at=frame.received_at + 2),
    ],
)
def test_stale_or_duplicate_wgc_source_evidence_never_reaches_input(transform):
    engine, backend, adapter, _notifications, clock = make_rig()
    backend.frame_transform = transform
    engine.start(allow_activation=True)

    snapshot = tick_many(engine, clock)

    assert snapshot.state != State.SUCCESS
    assert adapter.points == []
    assert backend.request_count == 0


def test_capture_status_generation_mismatch_rejects_frame():
    engine, backend, adapter, _notifications, clock = make_rig()
    backend.status_generation = 4
    engine.start()

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.PAUSED
    assert adapter.points == []


def test_minimized_capture_status_stops_background_detection():
    engine, backend, adapter, notifications, clock = make_rig()
    backend.minimized = True
    engine.start(allow_activation=True)

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.PAUSED
    assert backend.sequence == 0
    assert notifications == []
    assert adapter.points == []


def test_gdi_recognition_still_requires_visible_unobscured_foreground():
    engine, backend, adapter, notifications, clock = make_rig(backend="gdi")
    backend.foreground_value = True
    backend.clear = False
    engine.configure(
        backend.identity,
        engine.button,
        settings=Settings(capture_method="gdi"),
        mode=Mode.REMIND,
    )
    engine.set_hotkey_available(False)
    engine.start()

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.PAUSED
    assert notifications == []
    assert adapter.points == []


def test_unexpected_capture_generation_change_ends_round():
    engine, backend, adapter, _notifications, clock = make_rig()
    engine.start()
    tick_many(engine, clock, 3)
    assert backend.sequence > 0

    backend.generation += 1
    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.ERROR
    assert "重新启用" in snapshot.reason
    assert adapter.points == []


def test_target_identity_change_after_activation_cannot_continue_to_click():
    engine, backend, adapter, _notifications, clock = make_rig()

    def replace_identity(native, _cancelled):
        native.identity = replace(native.identity, process_started=999)

    backend.request_hook = replace_identity
    engine.start(allow_activation=True)

    snapshot = tick_many(engine, clock)

    assert snapshot.state == State.ERROR
    assert backend.request_count == 1
    assert adapter.points == []


def test_manual_return_does_not_reuse_candidate_after_button_disappears():
    engine, backend, adapter, _notifications, clock = make_rig()
    engine.start()
    assert tick_many(engine, clock).state == State.WAITING_FOREGROUND

    backend.foreground_value = True
    backend.image[:] = 35
    clock.advance()
    engine.tick()
    snapshot = tick_many(engine, clock, 20)

    assert snapshot.state == State.MONITORING
    assert adapter.points == []


def test_multiple_candidates_after_activation_are_not_clicked():
    engine, backend, adapter, _notifications, clock = make_rig()
    backend.foreground_on_request = True
    engine.start(allow_activation=True)

    while backend.request_count == 0:
        clock.advance()
        engine.tick()
    backend.image[140:164, 120:180] = pattern()
    snapshot = tick_many(engine, clock, 20)

    assert backend.request_count == 1
    assert snapshot.state == State.MONITORING
    assert adapter.points == []


def test_focus_lost_during_final_recapture_prevents_click():
    engine, backend, adapter, _notifications, clock = make_rig()
    backend.foreground_value = True
    backend.clear = True
    changed = [False]

    def lose_focus_on_final_capture(native, _frame):
        if engine._count >= engine.settings.consecutive_frames and not changed[0]:
            native.foreground_value = False
            changed[0] = True

    backend.on_capture = lose_focus_on_final_capture
    engine.start()

    snapshot = tick_many(engine, clock)

    assert changed[0]
    assert snapshot.state == State.WAITING_FOREGROUND
    assert adapter.points == []


def test_stop_after_background_wait_prevents_old_frame_from_rearming():
    engine, _backend, adapter, notifications, clock = make_rig()
    engine.start()
    assert tick_many(engine, clock).state == State.WAITING_FOREGROUND
    engine.stop()

    snapshot = tick_many(engine, clock, 10)

    assert snapshot.state == State.DISARMED
    assert adapter.points == []
    assert notifications == ["notified"]


def test_duplicate_callbacks_still_consume_only_one_click_attempt():
    engine, backend, adapter, _notifications, clock = make_rig()
    backend.foreground_value = True
    backend.clear = True
    engine.start()

    for _ in range(30):
        clock.advance()
        engine.tick()
        if adapter.points:
            break
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: engine.tick(), range(32)))

    assert len(adapter.points) == 1
