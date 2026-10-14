"""Offscreen simulator receipts paired with the production Engine and fake input only."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest

from auto_ready.engine import Engine
from auto_ready.models import (
    Frame,
    Geometry,
    Identity,
    Mode,
    Rect,
    Region,
    Settings,
    State,
    Template,
)
from auto_ready.recognition import validate_template
from auto_ready.simulator import SimulatorWindow


@dataclass
class FakeClock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float = 0.2) -> None:
        self.value += seconds


def _render_client(window: SimulatorWindow) -> np.ndarray:
    pixmap = window.centralWidget().grab()
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
    width, height = image.width(), image.height()
    bytes_per_line = image.bytesPerLine()
    pixels = np.frombuffer(
        image.constBits(), dtype=np.uint8, count=image.sizeInBytes()
    ).reshape(height, bytes_per_line)
    rgb = pixels[:, :width * 3].reshape(height, width, 3).copy()
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _widget_image_rect(widget: Any, root: Any, image: np.ndarray) -> Rect:
    origin = widget.mapTo(root, QPoint(0, 0))
    scale_x = image.shape[1] / root.width()
    scale_y = image.shape[0] / root.height()
    left = round(origin.x() * scale_x)
    top = round(origin.y() * scale_y)
    right = round((origin.x() + widget.width()) * scale_x)
    bottom = round((origin.y() + widget.height()) * scale_y)
    return Rect(left, top, right - left, bottom - top)


def _crop(image: np.ndarray, rect: Rect) -> np.ndarray:
    return np.ascontiguousarray(
        image[rect.y:rect.y + rect.height, rect.x:rect.x + rect.width].copy()
    )


class RenderBackend:
    """Controlled identity/scene with frames rendered from the owned Qt simulator."""

    def __init__(self, window: SimulatorWindow, clock: FakeClock, geometry: Geometry):
        self.window = window
        self.clock = clock
        self.identity = Identity(51001, 51002, 51003, "test-fake-simulator", window.windowTitle())
        self.geometry = geometry
        self.sequence = 0
        self.front = True
        self.clear = True
        self.dragging = False
        self.valid = True
        self.stale_by = 0.0
        self.session_override: str | None = None

    def inspect(self, identity: Identity) -> Geometry:
        if not self.valid or identity != self.identity:
            raise RuntimeError("fake simulator identity is invalid")
        return self.geometry

    def capture(self, identity: Identity, session_id: str) -> Frame:
        if identity != self.identity:
            raise RuntimeError("capture requested for an unknown fake identity")
        self.sequence += 1
        self.clock.advance(0.001)
        image = _render_client(self.window)
        return Frame(
            image=image,
            geometry=self.geometry,
            identity=self.identity,
            captured_at=self.clock() - self.stale_by,
            session_id=self.session_override or session_id,
            sequence=self.sequence,
        )

    def foreground(self, identity: Identity) -> bool:
        return self.front and identity == self.identity

    def moving(self, identity: Identity) -> bool:
        return self.dragging and identity == self.identity

    def unobscured(self, identity: Identity, _rect: Rect, _geometry: Geometry) -> bool:
        return self.clear and identity == self.identity


class QtFakeInput:
    """Maps a guarded engine point to QTest mouse events; never calls QPushButton.click."""

    def __init__(self, window: SimulatorWindow, geometry: Geometry):
        self.window = window
        self.geometry = geometry
        self.points: list[tuple[int, int]] = []

    def click(self, point: tuple[int, int], cancelled: Any) -> None:
        if cancelled.is_set():
            return
        self.points.append(point)
        root = self.window.centralWidget()
        button = self.window.ready_button
        scale_x = root.width() / self.geometry.width
        scale_y = root.height() / self.geometry.height
        logical_x = (point[0] - self.geometry.left) * scale_x
        logical_y = (point[1] - self.geometry.top) * scale_y
        origin = button.mapTo(root, QPoint(0, 0))
        local = QPoint(round(logical_x - origin.x()), round(logical_y - origin.y()))
        if not button.rect().contains(local):
            raise AssertionError(f"engine point {point} does not map into rendered ready button")
        QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=local)


def _capture_templates(window: SimulatorWindow) -> tuple[Geometry, Template, Template]:
    root = window.centralWidget()
    button = window.ready_button
    state_label = window.state_label
    image = _render_client(window)
    dpr = float(root.devicePixelRatioF())
    geometry = Geometry(120, 80, image.shape[1], image.shape[0], round(96 * dpr))
    button_rect = _widget_image_rect(button, root, image)
    button_template = Template(
        _crop(image, button_rect), geometry.template_key[:2], geometry.dpi, "渲染按钮模板"
    )
    validate_template(button_template.image)

    # Produce the success screen through an actual Qt test event, then sample its rendered
    # status text as a separate visual template. This is calibration only; its receipts are
    # discarded before the Engine scenario begins.
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    if window.state != "success":
        raise AssertionError("the simulator's rendered ready button did not enter success")
    success_image = _render_client(window)
    success_rect = _widget_image_rect(state_label, root, success_image)
    success_template = Template(
        _crop(success_image, success_rect), geometry.template_key[:2], geometry.dpi,
        "渲染成功状态模板",
    )
    validate_template(success_template.image)

    window.reset()
    window.manual_ready()
    return geometry, button_template, success_template


def _tick(engine: Engine, clock: FakeClock, count: int = 12) -> None:
    for _ in range(count):
        clock.advance()
        engine.tick()


def _tick_until_reason(engine: Engine, clock: FakeClock, text: str, count: int = 24) -> None:
    for _ in range(count):
        clock.advance()
        snapshot = engine.tick()
        if text in snapshot.reason:
            return
    raise AssertionError(f"engine did not report expected fake-scene reason: {text}")


_CASES = (
    ("reminder", State.REMINDED),
    ("disarmed", State.DISARMED),
    ("stop", State.DISARMED),
    ("emergency_stop", State.DISARMED),
    ("foreground", State.PAUSED),
    ("occlusion", State.PAUSED),
    ("moving", State.PAUSED),
    ("stale_frame", State.PAUSED),
    ("multiple_candidates", State.MONITORING),
    ("old_session", State.PAUSED),
    ("duplicate_callbacks", State.SUCCESS),
    ("exit", State.DISARMED),
)


@pytest.mark.parametrize(("scenario", "expected_state"), _CASES)
def test_rendered_simulator_fake_input_acceptance_matrix(
    qtbot, record_property, scenario: str, expected_state: State
) -> None:
    simulator_events: list[dict[str, Any]] = []

    def observe_simulator(event: str, **fields: Any) -> None:
        simulator_events.append({"event": event, **fields})

    window = SimulatorWindow(observer=observe_simulator, evidence_source="test_fake")
    qtbot.addWidget(window)
    window.show()
    qtbot.wait(20)
    window.manual_ready()
    if scenario == "multiple_candidates":
        window.duplicate_check.setChecked(True)
        qtbot.wait(10)

    geometry, button, success = _capture_templates(window)
    simulator_events.clear()

    # Keep the fake geometry tied to the pixels captured from the QWidget client render.
    rendered = _render_client(window)
    assert rendered.shape[:2] == (geometry.height, geometry.width)
    clock = FakeClock()
    backend = RenderBackend(window, clock, geometry)
    fake_input = QtFakeInput(window, geometry)
    engine = Engine(backend, fake_input, lambda: None, clock)
    mode = Mode.REMIND if scenario == "reminder" else Mode.AUTO
    engine.configure(backend.identity, button, success, Region(), Settings(), mode)
    engine.set_hotkey_available(True)
    engine_events: list[dict[str, Any]] = []
    engine.set_observer(lambda event, **fields: engine_events.append({"event": event, **fields}))

    try:
        if scenario not in {"disarmed"}:
            engine.start()
        if scenario == "stop":
            engine.stop("测试场景：手动停止")
        elif scenario == "emergency_stop":
            engine.stop("全局急停")
        elif scenario == "foreground":
            backend.front = False
        elif scenario == "occlusion":
            backend.clear = False
        elif scenario == "moving":
            backend.dragging = True
        elif scenario == "stale_frame":
            backend.stale_by = 2.0
        elif scenario == "multiple_candidates":
            # The simulator visibly renders two equal ready buttons; recognition must reject
            # both candidates as ambiguous without invoking the fake input adapter.
            assert window.duplicate_button.isVisible()
        elif scenario == "old_session":
            backend.session_override = "previous-session"
        elif scenario == "exit":
            engine.close()
        elif scenario == "duplicate_callbacks":
            # Duplicate tick callbacks are delivered sequentially on Qt's test thread so the
            # fake adapter's QTest mouse events stay in the GUI thread.
            _tick(engine, clock, 32)

        blocked_reasons = {
            "foreground": "不在前台",
            "occlusion": "遮挡",
            "moving": "正在移动",
            "stale_frame": "截图过期",
            "old_session": "旧会话",
        }
        if scenario in blocked_reasons:
            _tick_until_reason(engine, clock, blocked_reasons[scenario])
        elif scenario not in {"disarmed", "stop", "emergency_stop", "exit",
                              "duplicate_callbacks"}:
            _tick(engine, clock)
        elif scenario in {"disarmed", "stop", "emergency_stop", "exit"}:
            for _ in range(3):
                clock.advance()
                engine.tick()

        snapshot = engine.snapshot()
        attempts = sum(event["event"] == "input_attempt" for event in engine_events)
        receipts = {
            "press": sum(event["event"] == "simulator_button_mouse_press"
                         for event in simulator_events),
            "release": sum(event["event"] == "simulator_button_mouse_release"
                           for event in simulator_events),
            "click": sum(event["event"] == "simulator_button_clicked"
                         for event in simulator_events),
        }
        assert all(event.get("source") == "test_fake" for event in simulator_events)
        assert snapshot.state == expected_state
        assert attempts == (1 if scenario == "duplicate_callbacks" else 0)
        assert len(fake_input.points) == attempts
        if scenario == "duplicate_callbacks":
            assert receipts == {"press": 1, "release": 1, "click": 1}
        else:
            assert receipts == {"press": 0, "release": 0, "click": 0}

        row = {
            "scenario": scenario,
            "evidence_kind": "UNIT_FAKE_QT",
            "input_attempts": attempts,
            "simulator_receipts": receipts,
            "final_state": snapshot.state.value,
            "status": "PASS",
            "reason": snapshot.reason,
            "real_input_status": "NOT_EXECUTED",
        }
        for key, value in row.items():
            record_property(key, json.dumps(value, ensure_ascii=False)
                            if isinstance(value, (dict, list)) else str(value))
        record_property("simulator_press", str(receipts["press"]))
        record_property("simulator_release", str(receipts["release"]))
        record_property("simulator_click", str(receipts["click"]))
    finally:
        engine.close()
        window.reset()
        window.close()
        assert engine._closed
        assert engine._cancel.is_set()
        assert not window.ready_timer.isActive()
        assert not window.expiry_timer.isActive()
