"""Fake-input scenario evidence. These tests never assert a real mouse receipt."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_engine import Backend, Clock, Input, pattern

from auto_ready.app import _arguments
from auto_ready.engine import Engine
from auto_ready.models import Mode, State, Template


def make_engine():
    clock = Clock()
    backend, adapter = Backend(clock), Input()
    engine = Engine(backend, adapter, lambda: None, clock)
    engine.configure(backend.identity, Template(pattern(), (320, 240), 96), mode=Mode.AUTO)
    engine.set_hotkey_available(True)
    return engine, backend, adapter, clock


@pytest.mark.parametrize("scenario", [
    "disarmed", "reminder", "stop", "emergency_stop", "foreground", "occlusion",
    "moving", "stale", "multiple", "old_session", "duplicate_callbacks", "exit",
    "translated", "no_success_template", "success_absent",
])
def test_fake_acceptance_matrix(scenario, record_property):
    engine, backend, adapter, clock = make_engine()
    events = []
    engine.set_observer(lambda event, **data: events.append({"event": event, **data}))
    if scenario == "reminder":
        engine.configure(engine.identity, engine.button, mode=Mode.REMIND)
    if scenario == "success_absent":
        engine.configure(engine.identity, engine.button, Template(pattern(99), (320, 240), 96),
                         mode=Mode.AUTO)
    if scenario != "disarmed":
        engine.start()
    if scenario in {"stop", "emergency_stop"}:
        engine.stop("全局急停" if scenario == "emergency_stop" else "已停止")
    elif scenario == "exit":
        engine.close()
    elif scenario == "foreground":
        backend.visible = False
    elif scenario == "occlusion":
        backend.clear = False
    elif scenario == "moving":
        backend.dragging = True
    elif scenario == "stale":
        backend.frame_transform = lambda frame: replace(frame, captured_at=frame.captured_at - 2)
    elif scenario == "multiple":
        backend.image[140:164, 120:180] = pattern()
    elif scenario == "old_session":
        backend.frame_transform = lambda frame: replace(frame, session_id="previous-round")
    elif scenario == "translated":
        backend.geometry = replace(backend.geometry, left=-700, top=-100)
    if scenario == "duplicate_callbacks":
        clock.advance(1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: engine.tick(), range(30)))
    else:
        for _ in range(40):
            clock.advance()
            engine.tick()
    final = engine.snapshot()
    expected_attempts = int(scenario in {
        "duplicate_callbacks", "translated", "no_success_template", "success_absent",
    })
    assert len(adapter.points) == expected_attempts
    assert sum(e["event"] == "input_attempt" for e in events) == expected_attempts
    assert final.state != State.SUCCESS
    if expected_attempts:
        assert final.state == State.UNCONFIRMED
    if scenario == "translated":
        assert adapter.points == [(-550, -8)]
    record_property("acceptance", json.dumps({
        "scenario": scenario, "kind": "unit_fake_input", "input_attempts": len(adapter.points),
        "system_calls": 0, "simulator_receipts": None, "final_state": final.state.value,
        "status": "PASS", "reason": final.reason,
        "receipt_note": "No Qt simulator in this engine unit test; receipts not measured.",
        "real_input_status": "NOT_EXECUTED",
    }, ensure_ascii=False))


def test_success_evidence_is_separate_from_input_return():
    engine, backend, adapter, clock = make_engine()
    events = []
    engine.set_observer(lambda event, **data: events.append({"event": event, **data}))
    engine.configure(engine.identity, engine.button, Template(pattern(99), (320, 240), 96),
                     mode=Mode.AUTO)
    engine.start()
    for _ in range(8):
        clock.advance()
        engine.tick()
    assert engine.snapshot().state == State.VERIFYING
    assert len(adapter.points) == 1
    before = [e for e in events if e["event"] == "success_detection"
              and e["stage"] == "before_input"]
    assert len(before) == 1 and before[0]["candidates"] == 0
    # Explicit synthetic test image, never a real-simulator receipt.
    backend.image[150:174, 20:80] = pattern(99)
    for _ in range(2):
        clock.advance()
        engine.tick()
    assert engine.snapshot().state == State.SUCCESS
    positive = [e for e in events if e["event"] == "success_detection"
                and e["stage"] == "after_input" and e["candidates"] == 1]
    assert len(positive) == 2
    assert positive[0]["frame_sequence"] < positive[1]["frame_sequence"]
    assert len({e["session_id"] for e in events if e["session_id"]}) == 1


@pytest.mark.parametrize("change", ["stale", "stop", "movement", "observer_error"])
def test_evidence_observer_cannot_bypass_final_gate_or_retry(change):
    engine, backend, adapter, clock = make_engine()

    def observer(event, **data):
        if event != "input_attempt":
            return
        if change == "stale":
            clock.advance(1)
        elif change == "stop":
            engine.stop()
        elif change == "movement":
            backend.geometry = replace(backend.geometry, left=110)
        else:
            raise OSError("evidence storage unavailable")

    engine.set_observer(observer)
    engine.start()
    for _ in range(30):
        clock.advance()
        engine.tick()
    assert len(adapter.points) == (1 if change == "observer_error" else 0)
    assert engine.snapshot().state == State.UNCONFIRMED


@pytest.mark.parametrize("args", [
    ["--allow-real-input"], ["--simulator", "--allow-real-input"],
    ["--smoke-test", "--allow-real-input"], ["--verify-simulator", "--smoke-test"],
    ["--verify-simulator", "--simulator"],
])
def test_cli_rejects_unrestricted_or_mixed_real_input_flags(args):
    with pytest.raises(SystemExit) as error:
        _arguments(args)
    assert error.value.code == 2


@pytest.mark.parametrize("args", [[], ["--simulator"], ["--smoke-test"], ["--verify-simulator"]])
def test_cli_defaults_never_authorize_real_verification_input(args):
    assert not _arguments(args).allow_real_input
