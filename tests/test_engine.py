import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import numpy as np
import pytest

from auto_ready.engine import Engine
from auto_ready.models import Frame, Geometry, Identity, Mode, Region, Settings, State, Template


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now

    def advance(self, amount=0.2):
        self.now += amount


def pattern(seed=1):
    return np.random.default_rng(seed).integers(0, 256, (24, 60, 3), dtype=np.uint8)


class Backend:
    def __init__(self, clock):
        self.clock = clock
        self.identity = Identity(100, 200, 300, "mock.exe", "模拟")
        self.geometry = Geometry(100, 200, 320, 240, 96)
        self.visible = True
        self.dragging = False
        self.clear = True
        self.valid = True
        self.sequence = 0
        self.image = np.full((240, 320, 3), 35, dtype=np.uint8)
        self.image[80:104, 120:180] = pattern()
        self.on_capture = None
        self.frame_transform = lambda f: f
        self.queued_frames = []

    def inspect(self, identity):
        if not self.valid or identity != self.identity:
            raise RuntimeError("目标已关闭或身份变化")
        return self.geometry

    def foreground(self, identity):
        return self.visible

    def moving(self, identity):
        return self.dragging

    def unobscured(self, identity, rect, geometry):
        return self.clear

    def capture(self, identity, session_id):
        if self.queued_frames:
            return self.queued_frames.pop(0)
        self.sequence += 1
        # A new actual capture has a newer timestamp, even during final recapture.
        self.clock.advance(0.001)
        frame = Frame(
            self.image.copy(), self.geometry, self.identity, self.clock(), session_id, self.sequence
        )
        if self.on_capture:
            self.on_capture(self)
        return self.frame_transform(frame)


class Input:
    def __init__(self):
        self.points = []
        self.error = False

    def click(self, point, cancelled):
        if cancelled.is_set():
            return
        self.points.append(point)
        if self.error:
            raise OSError("partial SendInput")


@pytest.fixture
def rig():
    clock = Clock()
    backend, adapter, sounds = Backend(clock), Input(), []
    engine = Engine(backend, adapter, lambda: sounds.append(True), clock)
    button = Template(pattern(), (320, 240), 96)
    engine.configure(backend.identity, button, mode=Mode.AUTO)
    engine.set_hotkey_available(True)
    return engine, backend, adapter, sounds, clock


def run(rig, count=12):
    engine, _, _, _, clock = rig
    for _ in range(count):
        clock.advance()
        engine.tick()
    return engine.snapshot()


def test_one_click_and_no_success_without_evidence(rig):
    engine, _, adapter, sounds, _ = rig
    engine.start()
    assert run(rig).state == State.UNCONFIRMED
    assert adapter.points == [(250, 292)]
    run(rig, 20)
    assert len(adapter.points) == len(sounds) == 1
    assert State.ATTEMPTED in [state for _, state, _ in engine.history]


def test_translation_tracks_negative_desktop_coordinates(rig):
    engine, backend, adapter, _, _ = rig
    engine.start()
    backend.geometry = replace(backend.geometry, left=-700, top=-100)
    run(rig)
    assert adapter.points == [(-550, -8)]


@pytest.mark.parametrize("field", ["dragging", "visible", "clear"])
def test_transient_safety_gate_resets_and_recovers(rig, field):
    engine, backend, adapter, _, _ = rig
    engine.start()
    setattr(backend, field, field == "dragging")
    run(rig)
    assert adapter.points == []
    setattr(backend, field, field != "dragging")
    run(rig)
    assert len(adapter.points) == 1


@pytest.mark.parametrize("change", ["dpi", "size", "closed", "identity", "minimized"])
def test_geometry_or_identity_break_is_terminal(rig, change):
    engine, backend, adapter, _, _ = rig
    engine.start()
    if change == "dpi":
        backend.geometry = replace(backend.geometry, dpi=144)
    elif change == "size":
        backend.geometry = replace(backend.geometry, width=500)
    elif change == "identity":
        backend.identity = replace(backend.identity, process_started=301)
    else:
        backend.valid = False
    assert run(rig).state == State.ERROR
    assert not adapter.points


def test_move_during_final_capture_never_uses_old_coordinates(rig):
    engine, backend, adapter, _, _ = rig
    engine.start()
    backend.on_capture = lambda b: setattr(
        b, "geometry", replace(b.geometry, left=b.geometry.left + 10)
    )
    run(rig, 25)
    assert not adapter.points


@pytest.mark.parametrize(
    "transform",
    [
        lambda f: replace(f, captured_at=f.captured_at - 2),
        lambda f: replace(f, captured_at=f.captured_at + 2),
        lambda f: replace(f, session_id="old"),
        lambda f: replace(f, identity=replace(f.identity, hwnd=101)),
        lambda f: replace(f, sequence=0, captured_at=10.5),
    ],
)
def test_stale_duplicate_or_old_session_frame_cannot_click(rig, transform):
    engine, backend, adapter, _, _ = rig
    backend.frame_transform = transform
    engine.start()
    run(rig)
    assert not adapter.points


@pytest.mark.parametrize("scene", ["black", "blank", "wrong", "duplicate", "absent"])
def test_invalid_image_or_ambiguous_match_never_clicks(rig, scene):
    engine, backend, adapter, _, _ = rig
    if scene == "black":
        backend.image[:] = 0
    elif scene in {"blank", "absent"}:
        backend.image[:] = 128
    elif scene == "wrong":
        backend.image[80:104, 120:180] = pattern(2)
    else:
        backend.image[140:164, 120:180] = pattern()
    engine.start()
    run(rig)
    assert not adapter.points


def test_reminder_dedup_never_invokes_real_input(rig):
    engine, _, adapter, sounds, _ = rig
    engine.configure(engine.identity, engine.button, mode=Mode.REMIND)
    engine.set_hotkey_available(False)
    engine.start()
    snapshot = run(rig, 30)
    assert snapshot.state == State.REMINDED
    assert not adapter.points
    assert sounds == [True]


def test_manual_pause_requires_resume(rig):
    engine, _, adapter, _, _ = rig
    engine.start()
    engine.pause()
    snapshot = run(rig)
    assert snapshot.state == State.PAUSED
    assert snapshot.manual_paused is True
    assert not adapter.points
    engine.resume()
    assert engine.snapshot().manual_paused is False
    run(rig)
    assert len(adapter.points) == 1


def test_stop_clears_manual_pause_snapshot_state(rig):
    engine, _, _, _, _ = rig
    engine.start()
    engine.pause()
    assert engine.snapshot().manual_paused is True

    engine.stop()

    assert engine.snapshot().state == State.DISARMED
    assert engine.snapshot().manual_paused is False
    assert engine._manual_pause is False
    assert engine._pause_requested.is_set() is False


@pytest.mark.parametrize("action", ["stop", "close", "hotkey", "configure"])
def test_control_cancels_current_round(rig, action):
    engine, _, adapter, _, _ = rig
    engine.start()
    if action == "hotkey":
        engine.set_hotkey_available(False)
    elif action == "configure":
        engine.configure(engine.identity, engine.button, settings=Settings(threshold=0.95))
    else:
        getattr(engine, action)()
    run(rig)
    assert not adapter.points


def test_hotkey_failure_rejects_automatic_start(rig):
    engine, _, adapter, _, _ = rig
    engine.set_hotkey_available(False)
    with pytest.raises(ValueError, match="急停"):
        engine.start()
    assert not adapter.points


def test_missing_target_or_template_cannot_start(rig):
    engine, _, adapter, _, _ = rig
    engine.configure(None, None)
    with pytest.raises(ValueError):
        engine.start()
    assert not adapter.points


def test_input_exception_consumes_eligibility(rig):
    engine, _, adapter, _, _ = rig
    adapter.error = True
    engine.start()
    snapshot = run(rig)
    assert snapshot.state == State.UNCONFIRMED and snapshot.attempted
    run(rig)
    assert len(adapter.points) == 1


def test_success_requires_positive_template_and_two_new_frames(rig):
    engine, backend, adapter, _, _ = rig
    success = Template(pattern(99), (320, 240), 96, "成功")
    engine.configure(engine.identity, engine.button, success, mode=Mode.AUTO)
    engine.start()
    run(rig, 8)
    assert engine.snapshot().state == State.VERIFYING
    backend.image[80:104, 120:180] = 35  # Disappearance is not success.
    run(rig, 2)
    assert engine.snapshot().state == State.VERIFYING
    backend.image[150:174, 20:80] = pattern(99)
    run(rig, 2)
    assert engine.snapshot().state == State.SUCCESS
    assert len(adapter.points) == 1


def test_success_frames_captured_during_delayed_click_do_not_confirm(rig):
    engine, backend, adapter, _, clock = rig
    success = Template(pattern(99), (320, 240), 96, "成功")
    engine.configure(
        engine.identity,
        engine.button,
        success,
        settings=Settings(max_frame_age=0.5, verification_timeout=1.0),
        mode=Mode.AUTO,
    )
    engine.start()
    capture = backend.capture

    def delayed_click(point, cancelled):
        assert not cancelled.is_set()
        backend.image[150:174, 20:80] = pattern(99)
        clock.advance(0.13)
        during_click = [capture(engine.identity, engine.snapshot().session_id) for _ in range(2)]
        backend.queued_frames.extend(during_click)
        clock.advance(0.02)
        adapter.points.append(point)

    adapter.click = delayed_click
    for _ in range(12):
        clock.advance()
        engine.tick()
        if adapter.points:
            break

    assert len(adapter.points) == 1
    assert engine.snapshot().state == State.VERIFYING
    attempted_at = engine._attempted_at
    for _ in range(2):
        clock.advance(0.1)
        snapshot = engine.tick()
    assert snapshot.state == State.VERIFYING
    assert engine._success_count == 0
    assert engine._attempted_at == attempted_at

    clock.advance(0.65)
    snapshot = engine.tick()
    assert snapshot.state == State.UNCONFIRMED
    assert len(adapter.points) == 1


def test_preexisting_success_evidence_blocks_click(rig):
    engine, backend, adapter, _, _ = rig
    success = Template(pattern(99), (320, 240), 96, "成功")
    backend.image[150:174, 20:80] = pattern(99)
    engine.configure(engine.identity, engine.button, success, mode=Mode.AUTO)
    engine.start()
    run(rig)
    assert not adapter.points


def test_verification_timeout_no_retry_or_success(rig):
    engine, backend, adapter, _, _ = rig
    engine.configure(
        engine.identity, engine.button, Template(pattern(99), (320, 240), 96), mode=Mode.AUTO
    )
    engine.start()
    run(rig, 8)
    backend.image[:] = 0
    assert run(rig, 50).state == State.UNCONFIRMED
    assert len(adapter.points) == 1


def test_round_timeout_never_auto_restarts(rig):
    engine, backend, adapter, _, _ = rig
    engine.configure(
        engine.identity, engine.button, settings=Settings(round_timeout=5), mode=Mode.AUTO
    )
    backend.visible = False
    engine.start()
    assert run(rig, 40).state == State.UNCONFIRMED
    backend.visible = True
    run(rig)
    assert not adapter.points


def test_concurrent_callbacks_are_serialized_once(rig):
    engine, _, adapter, _, clock = rig
    engine.start()
    clock.advance(1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: engine.tick(), range(24)))
    assert len(adapter.points) == 1


def test_stop_during_blocked_capture_cancels_pending_click(rig):
    engine, backend, adapter, _, clock = rig
    engine.start()
    clock.advance(1)
    entered, release = threading.Event(), threading.Event()

    def capture_hook(_):
        entered.set()
        assert release.wait(2)

    backend.on_capture = capture_hook
    thread = threading.Thread(target=engine.tick)
    thread.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=engine.stop)
    stopper.start()
    assert engine._cancel.wait(2)
    release.set()
    thread.join(2)
    stopper.join(2)
    assert not thread.is_alive() and not stopper.is_alive()
    assert not adapter.points


def test_old_session_cannot_rearm_new_round(rig):
    engine, backend, adapter, _, _ = rig
    old_id = engine.start()
    new_id = engine.start()
    assert new_id != old_id
    backend.frame_transform = lambda f: replace(f, session_id=old_id)
    run(rig)
    assert not adapter.points


def test_preview_is_non_clicking_even_after_start(rig):
    engine, _, adapter, _, _ = rig
    engine.start()
    assert engine.preview().state == State.DISARMED
    run(rig)
    assert not adapter.points


def test_normalized_search_region_limits_candidate(rig):
    engine, backend, adapter, _, _ = rig
    backend.image[140:164, 120:180] = pattern()
    engine.configure(engine.identity, engine.button, region=Region(0, 0, 1, 0.5), mode=Mode.AUTO)
    engine.start()
    run(rig)
    assert adapter.points == [(250, 292)]


@pytest.mark.parametrize("change", ["move", "dpi", "foreground", "occlusion", "stop", "pause"])
def test_final_recapture_rechecks_all_mutable_safety_conditions(rig, change):
    engine, backend, adapter, _, _ = rig

    def mutate_on_final(b):
        if b.sequence != 4:
            return
        if change == "move":
            b.geometry = replace(b.geometry, left=900)
        elif change == "dpi":
            b.geometry = replace(b.geometry, dpi=144)
        elif change == "foreground":
            b.visible = False
        elif change == "occlusion":
            b.clear = False
        else:
            getattr(engine, change)()

    backend.on_capture = mutate_on_final
    engine.start()
    run(rig, 5)
    assert backend.sequence == 4
    assert not adapter.points


def test_pause_while_capture_is_pending_prevents_execution(rig):
    engine, backend, adapter, _, clock = rig
    engine.start()
    clock.advance(1)
    engine.tick()
    engine.tick()
    entered, release = threading.Event(), threading.Event()

    def hook(_):
        entered.set()
        assert release.wait(2)

    backend.on_capture = hook
    runner = threading.Thread(target=engine.tick)
    runner.start()
    assert entered.wait(2)
    pauser = threading.Thread(target=engine.pause)
    pauser.start()
    assert engine._pause_requested.wait(2)
    release.set()
    runner.join(2)
    pauser.join(2)
    assert not adapter.points
    assert engine.snapshot().state == State.PAUSED


def test_live_hotkey_loss_at_final_boundary_is_fail_closed(rig):
    engine, backend, adapter, _, _ = rig
    alive = [True]
    engine.set_hotkey_probe(lambda: alive[0])
    engine.start()
    backend.on_capture = lambda b: alive.__setitem__(0, b.sequence < 4)
    run(rig, 10)
    assert engine.snapshot().state == State.ERROR
    assert not adapter.points


def test_invalid_configuration_cannot_reuse_previous_click_meaning(rig):
    engine, backend, adapter, _, _ = rig
    with pytest.raises(ValueError):
        engine.configure(
            backend.identity,
            Template(np.zeros((24, 60, 3), np.uint8), (320, 240), 96),
            mode=Mode.AUTO,
        )
    with pytest.raises(ValueError):
        engine.start()
    run(rig)
    assert not adapter.points


def test_success_evidence_must_stay_at_same_location_across_new_frames(rig):
    engine, backend, adapter, _, _ = rig
    engine.configure(
        engine.identity, engine.button, Template(pattern(99), (320, 240), 96), mode=Mode.AUTO
    )
    engine.start()
    run(rig, 6)
    assert len(adapter.points) == 1
    for x in (20, 140, 20, 140):
        backend.image[150:174, :] = 35
        backend.image[150:174, x : x + 60] = pattern(99)
        run(rig, 1)
        assert engine.snapshot().state == State.VERIFYING
    run(rig, 1)
    assert engine.snapshot().state == State.SUCCESS


def test_elapsed_freezes_at_first_terminal_transition_even_at_clock_zero(rig):
    engine, _, adapter, _, clock = rig
    clock.now = 0.0
    engine.start()
    engine.stop()
    stopped = engine.snapshot()
    assert stopped.state == State.DISARMED
    assert stopped.elapsed == 0

    clock.advance(5)
    engine.stop("重复停止")
    assert engine.snapshot().elapsed == stopped.elapsed
    assert adapter.points == []


@pytest.mark.parametrize("terminal", [State.SUCCESS, State.UNCONFIRMED, State.ERROR])
def test_terminal_finish_freezes_elapsed_without_input(rig, terminal):
    engine, _, adapter, _, clock = rig
    engine.start()
    clock.advance(1.25)
    engine._finish(terminal, "测试终态")
    finished = engine.snapshot()
    assert finished.state == terminal

    clock.advance(4)
    assert engine.snapshot().elapsed == finished.elapsed
    assert adapter.points == []


def test_emergency_stop_transition_freezes_elapsed(rig):
    engine, _, adapter, _, clock = rig
    engine.start()
    clock.advance(1.25)
    engine.set_hotkey_available(False)
    finished = engine.snapshot()
    assert finished.state == State.ERROR

    clock.advance(4)
    assert engine.snapshot().elapsed == finished.elapsed
    assert adapter.points == []


def test_failed_initial_start_keeps_elapsed_at_zero(rig):
    engine, _, adapter, _, clock = rig
    engine.configure(None, None)
    clock.advance(5)
    with pytest.raises(ValueError):
        engine.start()

    clock.advance(4)
    assert engine.snapshot().state == State.DISARMED
    assert engine.snapshot().elapsed == 0
    assert adapter.points == []


def test_failed_restart_preserves_previous_completed_elapsed(rig):
    engine, backend, adapter, _, clock = rig
    session = engine.start()
    clock.advance(1.25)
    engine.stop()
    completed = engine.snapshot().elapsed
    clock.advance(3)
    backend.valid = False

    with pytest.raises(RuntimeError, match="目标已关闭"):
        engine.start()

    snapshot = engine.snapshot()
    assert snapshot.session_id == session
    assert snapshot.state == State.DISARMED
    assert snapshot.elapsed == completed
    assert adapter.points == []


def test_successful_restart_resets_elapsed_and_advances(rig):
    engine, _, adapter, _, clock = rig
    first_session = engine.start()
    clock.advance(1.25)
    engine.stop()
    clock.advance(3)

    second_session = engine.start()
    assert second_session != first_session
    assert engine.snapshot().elapsed == 0
    clock.advance(0.75)
    assert engine.snapshot().elapsed == pytest.approx(0.75)
    assert adapter.points == []


def test_paused_time_still_counts_toward_round_timeout(rig):
    engine, _, adapter, _, clock = rig
    engine.configure(
        engine.identity,
        engine.button,
        settings=Settings(round_timeout=5),
        mode=Mode.AUTO,
    )
    engine.start()
    engine.pause()
    clock.advance(6)
    engine.resume()

    assert engine.tick().state == State.UNCONFIRMED
    assert adapter.points == []
