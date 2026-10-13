from __future__ import annotations

from threading import Event

import numpy as np
import pytest

from auto_ready.models import Frame, Geometry, Identity, Rect
from auto_ready.native import SafetyError
from scripts.verify_simulator import (
    RefusingInput,
    SimulatorBoundInput,
    SimulatorOnlyBackend,
    build_input_adapter,
    same_identity,
)


def _identity(
    hwnd: int = 10,
    pid: int = 99,
    process_started: int = 1234,
    executable: str = r"C:\Python\python.exe",
    title: str = "Auto Ready 模拟器",
) -> Identity:
    return Identity(hwnd, pid, process_started, executable, title)


class FakeBackend:
    def __init__(self, identities: list[Identity] | None = None):
        self.identities = list(identities or [])
        self.calls: list[tuple] = []
        self.geometry = Geometry(100, 200, 640, 480, 144)
        self.is_foreground = True
        self.capture_value = "frame"
        self.on_foreground = None

    def list_windows(self) -> list[Identity]:
        self.calls.append(("list_windows",))
        return list(self.identities)

    def inspect(self, identity: Identity) -> Geometry:
        self.calls.append(("inspect", identity))
        return self.geometry

    def foreground(self, identity: Identity) -> bool:
        self.calls.append(("foreground", identity))
        if self.on_foreground is not None:
            self.on_foreground()
        return self.is_foreground

    def moving(self, identity: Identity) -> bool:
        self.calls.append(("moving", identity))
        return False

    def unobscured(self, identity: Identity, rect: Rect, geometry: Geometry) -> bool:
        self.calls.append(("unobscured", identity, rect, geometry))
        return True

    def capture(self, identity: Identity, session_id: str):
        self.calls.append(("capture", identity, session_id))
        return self.capture_value


class FakeInput:
    def __init__(self):
        self.calls: list[tuple] = []

    def click(self, point: tuple[int, int], cancelled: Event) -> None:
        self.calls.append((point, cancelled))


def test_identity_checks_hwnd_pid_start_and_executable_but_not_title():
    expected = _identity()
    assert same_identity(_identity(title="changed title"), expected)
    assert not same_identity(_identity(hwnd=11), expected)
    assert not same_identity(_identity(pid=100), expected)
    assert not same_identity(_identity(process_started=1235), expected)
    assert not same_identity(_identity(executable=r"C:\Other\python.exe"), expected)


@pytest.mark.parametrize("frozen", [False, True])
def test_own_simulator_identity_handles_venv_redirector_without_accepting_other_executable(
    monkeypatch, frozen,
):
    from types import SimpleNamespace

    from auto_ready import verification

    monkeypatch.setattr(verification.sys, "executable", r"C:\venv\Scripts\python.exe")
    monkeypatch.setattr(verification.sys, "_base_executable", r"C:\Python\python.exe")
    monkeypatch.setattr(verification.sys, "frozen", frozen, raising=False)
    identity = _identity(pid=verification.os.getpid())
    backend = FakeBackend([identity])
    window = SimpleNamespace(winId=lambda: identity.hwnd)
    if frozen:
        with pytest.raises(SafetyError, match="uniquely identify"):
            verification._simulator_identity(backend, window)
    else:
        assert verification._simulator_identity(backend, window) == identity
    backend.identities = [_identity(pid=verification.os.getpid(), executable=r"C:\Other\app.exe")]
    with pytest.raises(SafetyError, match="uniquely identify"):
        verification._simulator_identity(backend, window)


def test_target_inventory_exposes_only_the_captured_simulator_identity():
    target = _identity()
    backend = FakeBackend([_identity(hwnd=20, title="other"), target])
    restricted = SimulatorOnlyBackend(backend, target)
    assert restricted.list_windows() == [target]

    backend.identities = [target, _identity(hwnd=11)]
    restricted.assert_simulator_current()

    backend.identities = [_identity(hwnd=10, pid=99, process_started=1235)]
    with pytest.raises(SafetyError, match="no longer present"):
        restricted.assert_simulator_current()


@pytest.mark.parametrize("operation", ["inspect", "foreground", "moving", "unobscured", "capture"])
def test_backend_rejects_any_identity_other_than_the_captured_simulator(operation: str):
    target = _identity()
    wrong = _identity(hwnd=11)
    raw = FakeBackend([target, wrong])
    restricted = SimulatorOnlyBackend(raw, target)

    with pytest.raises(SafetyError, match="only its own simulator"):
        if operation == "inspect":
            restricted.inspect(wrong)
        elif operation == "foreground":
            restricted.foreground(wrong)
        elif operation == "moving":
            restricted.moving(wrong)
        elif operation == "unobscured":
            restricted.unobscured(wrong, Rect(0, 0, 10, 10), raw.geometry)
        else:
            restricted.capture(wrong, "session")
    assert raw.calls == []


def test_default_adapter_refuses_without_constructing_real_input(monkeypatch):
    from auto_ready import native

    target = _identity()
    backend = SimulatorOnlyBackend(FakeBackend([target]), target)
    constructions: list[str] = []
    monkeypatch.setattr(
        native,
        "WindowsInput",
        lambda: constructions.append("constructed"),
    )

    adapter = build_input_adapter(
        backend,
        target,
        allow_real_input=False,
    )

    assert isinstance(adapter, RefusingInput)
    assert constructions == []
    with pytest.raises(SafetyError, match="--allow-real-input"):
        adapter.click((110, 210), Event())


def test_opt_in_adapter_rechecks_identity_foreground_and_bounds_before_one_delegate_call():
    target = _identity()
    raw = FakeBackend([target])
    restricted = SimulatorOnlyBackend(raw, target)
    delegate = FakeInput()
    constructions: list[str] = []
    clock = [100.0]
    raw.capture_value = Frame(
        np.zeros((raw.geometry.height, raw.geometry.width, 3), dtype=np.uint8),
        raw.geometry,
        target,
        clock[0],
        "round-1",
        1,
    )
    restricted.capture(target, "round-1")
    raw.calls.clear()

    adapter = build_input_adapter(
        restricted,
        target,
        allow_real_input=True,
        input_factory=lambda: constructions.append("constructed") or delegate,
        clock=lambda: clock[0],
    )
    assert isinstance(adapter, SimulatorBoundInput)
    assert constructions == ["constructed"]
    adapter.set_runtime_probes(max_frame_age=lambda: 0.35, session_id=lambda: "round-1")

    raw.identities.clear()
    with pytest.raises(SafetyError, match="identity is no longer present"):
        adapter.click((110, 210), Event())
    assert delegate.calls == []

    raw.identities.append(target)
    raw.is_foreground = False
    with pytest.raises(SafetyError, match="foreground"):
        adapter.click((110, 210), Event())
    assert delegate.calls == []

    raw.is_foreground = True
    with pytest.raises(SafetyError, match="outside the simulator client"):
        adapter.click((99, 210), Event())
    assert delegate.calls == []

    # A moved window still contains this old global point, but the frame geometry is stale.
    raw.geometry = Geometry(105, 205, 640, 480, 144)
    with pytest.raises(SafetyError, match="moved or resized"):
        adapter.click((420, 500), Event())
    raw.geometry = Geometry(100, 200, 640, 480, 144)

    clock[0] = 100.36
    with pytest.raises(SafetyError, match="capture is stale"):
        adapter.click((420, 500), Event())
    clock[0] = 100.0

    adapter.set_runtime_probes(max_frame_age=lambda: 0.35, session_id=lambda: "round-2")
    with pytest.raises(SafetyError, match="active round"):
        adapter.click((420, 500), Event())
    adapter.set_runtime_probes(max_frame_age=lambda: 0.35, session_id=lambda: "round-1")

    cancelled_before = Event()
    cancelled_before.set()
    with pytest.raises(SafetyError, match="cancelled"):
        adapter.click((420, 500), cancelled_before)

    cancelled_during = Event()
    raw.on_foreground = cancelled_during.set
    with pytest.raises(SafetyError, match="cancelled"):
        adapter.click((420, 500), cancelled_during)
    raw.on_foreground = None
    assert delegate.calls == []

    cancelled = Event()
    adapter.click((420, 500), cancelled)
    assert delegate.calls == [((420, 500), cancelled)]
    assert raw.calls[-3][0] == "list_windows"
    assert raw.calls[-2][0] == "foreground"
    assert raw.calls[-1][0] == "inspect"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [("geometry", "moved or resized before input"), ("stale", "capture became stale")],
)
def test_last_boundary_rechecks_geometry_and_freshness(mutation: str, message: str):
    target = _identity()
    raw = FakeBackend([target])
    restricted = SimulatorOnlyBackend(raw, target)
    delegate = FakeInput()
    clock = [100.0]
    raw.capture_value = Frame(
        np.zeros((raw.geometry.height, raw.geometry.width, 3), dtype=np.uint8),
        raw.geometry,
        target,
        clock[0],
        "round-1",
        1,
    )
    restricted.capture(target, "round-1")
    adapter = build_input_adapter(
        restricted,
        target,
        allow_real_input=True,
        input_factory=lambda: delegate,
        clock=lambda: clock[0],
    )
    assert isinstance(adapter, SimulatorBoundInput)
    adapter.set_runtime_probes(max_frame_age=lambda: 0.35, session_id=lambda: "round-1")

    foreground_calls = 0

    def mutate_at_final_foreground_check():
        nonlocal foreground_calls
        foreground_calls += 1
        if foreground_calls == 2:
            if mutation == "geometry":
                # The old point remains within the overlapping client rectangle.
                raw.geometry = Geometry(102, 202, 640, 480, 144)
            else:
                clock[0] = 100.36

    raw.on_foreground = mutate_at_final_foreground_check
    with pytest.raises(SafetyError, match=message):
        adapter.click((420, 500), Event())

    assert foreground_calls == 2
    assert delegate.calls == []
