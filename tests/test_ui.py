from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog

from auto_ready.app import main
from auto_ready.config import ConfigStore
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
from auto_ready.ui import ImagePreview, MainWindow, _WorkerResult


@pytest.fixture
def close_main_windows(qtbot):
    windows = []

    def register(window):
        qtbot.addWidget(window)
        windows.append(window)
        return window

    yield register

    for window in windows:
        window.close()
        qtbot.waitUntil(lambda window=window: not window.worker.isRunning(), timeout=5000)
        qtbot.waitUntil(lambda window=window: window._closed, timeout=3000)


class FakeBackend:
    def __init__(self) -> None:
        self.identity = Identity(101, 202, 303, "mock-game.exe", "模拟目标")
        self.geometry = Geometry(20, 30, 200, 120, 96)
        self.image = np.full((120, 200, 3), 28, dtype=np.uint8)
        pattern = np.random.default_rng(9).integers(0, 255, (24, 32, 3), dtype=np.uint8)
        self.image[42:66, 76:108] = pattern
        self.button_image = self.image.copy()
        self.success_image = np.full((120, 200, 3), 28, dtype=np.uint8)
        success_pattern = np.random.default_rng(17).integers(0, 255, (24, 32, 3), dtype=np.uint8)
        self.success_image[42:66, 130:162] = success_pattern
        self.foreground_active = True
        self.sequence = 0
        self._last_captured_at = 0.0
        self.clock: Callable[[], float] = time.monotonic

    def show_button(self) -> None:
        self.image = self.button_image.copy()

    def show_success(self) -> None:
        self.image = self.success_image.copy()

    def list_windows(self) -> list[Identity]:
        return [self.identity]

    def inspect(self, identity: Identity) -> Geometry:
        if identity != self.identity:
            raise RuntimeError("unknown identity")
        return self.geometry

    def capture(self, identity: Identity, session_id: str) -> Frame:
        self.sequence += 1
        captured_at = self.clock()
        while captured_at <= self._last_captured_at:
            if self.clock is time.monotonic:
                time.sleep(0.001)
            captured_at = self.clock()
        self._last_captured_at = captured_at
        return Frame(self.image.copy(), self.geometry, identity, captured_at, session_id,
                     self.sequence)

    def foreground(self, _identity: Identity) -> bool:
        return self.foreground_active

    def moving(self, _identity: Identity) -> bool:
        return False

    def unobscured(self, _identity: Identity, _rect: Rect, _geometry: Geometry) -> bool:
        return True


class FakeInput:
    def __init__(self, on_click: Callable[[], None] | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.on_click = on_click

    def click(self, point: tuple[int, int], _cancelled: object) -> None:
        self.calls.append(point)
        if self.on_click is not None:
            self.on_click()


class FakeStore:
    def __init__(self) -> None:
        self.saved = None

    def save(self, *values: object) -> None:
        self.saved = values

    def load(self) -> tuple[object, ...]:
        from auto_ready.models import Region, Settings

        return Settings(), Region(), None, None


class FakeHotkey:
    error = ""
    available = True

    def __init__(self, spec: str, _callback: Callable[[], None]) -> None:
        self.spec = spec
        self.callback = _callback

    def start(self) -> bool:
        return True

    def close(self) -> None:
        pass


class FakeClock:
    def __init__(self, start: float) -> None:
        self.value = start
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            current = self.value
            self.value += 0.02
            return current


def test_image_preview_maps_letterboxed_drag_to_physical_pixels(qtbot) -> None:
    backend = FakeBackend()
    frame = backend.capture(backend.identity, "static")
    preview = ImagePreview()
    preview.resize(500, 350)
    preview.set_frame(frame)
    preview.set_selection_enabled(True)
    qtbot.addWidget(preview)
    preview.show()
    qtbot.wait(20)

    display = preview._display_rect()
    start = QPoint(display.x() + 5, display.y() + 5)
    end = QPoint(display.x() + display.width() - 6, display.y() + display.height() - 6)
    first = preview._to_image_pixel(start)
    last = preview._to_image_pixel(end)
    assert first is not None and last is not None
    assert preview._to_image_pixel(QPoint(display.center().x(), display.y() - 1)) is None
    selections: list[Rect] = []
    preview.selectionMade.connect(selections.append)

    QTest.mousePress(preview, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(preview, end)
    QTest.mouseRelease(preview, Qt.MouseButton.LeftButton, pos=end)

    assert selections == [Rect(min(first[0], last[0]), min(first[1], last[1]),
                               abs(last[0] - first[0]) + 1,
                               abs(last[1] - first[1]) + 1)]


def test_fake_backend_workflow_previews_and_starts_in_remind_mode(
    qtbot, close_main_windows
) -> None:
    backend = FakeBackend()
    input_adapter = FakeInput(backend.show_success)
    window = MainWindow(backend=backend, input_adapter=input_adapter, store=FakeStore(),
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()

    qtbot.waitUntil(lambda: window.target_combo.count() == 2, timeout=3000)
    assert window.status_label.text().startswith("已找到 1 个可见窗口")
    window.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window.geometry == backend.geometry, timeout=3000)
    window.capture_static()
    qtbot.waitUntil(lambda: window._static_frame is not None, timeout=3000)
    assert window.target_combo.currentData() == backend.identity

    window.selection_combo.setCurrentIndex(1)
    window._selection_made(Rect(76, 42, 32, 24))
    assert window.button is not None
    assert window.engine.button is window.button
    assert window.mode == Mode.REMIND

    window.selection_combo.setCurrentIndex(2)
    window._selection_made(Rect(30, 20, 150, 80))
    expected_region = window.region
    button_template = window.button

    backend.show_success()
    window.capture_static()
    qtbot.waitUntil(lambda: window._static_frame is not None, timeout=3000)
    assert window.button is button_template
    assert window.region == expected_region
    window.selection_combo.setCurrentIndex(3)
    window._selection_made(Rect(130, 42, 32, 24))
    assert window.success is not None
    success_template = window.success
    assert window.button is button_template

    backend.show_button()
    window.capture_static()
    qtbot.waitUntil(lambda: window._static_frame is not None, timeout=3000)
    assert window.button is button_template
    assert window.success is success_template
    assert window.region == expected_region

    window.frames_spin.setValue(2)
    window.stable_spin.setValue(0.2)
    fake_clock = FakeClock(backend._last_captured_at + 0.01)
    backend.clock = fake_clock
    window.engine.clock = fake_clock

    window.preview_without_input()
    qtbot.waitUntil(lambda: window.status_label.text().startswith("无点击预览"), timeout=3000)
    assert window.engine.snapshot().state == State.DISARMED

    backend.foreground_active = False
    window.start_round()
    qtbot.waitUntil(lambda: window._pending_start_request is None, timeout=3000)
    qtbot.waitUntil(lambda: window.engine.snapshot().state == State.PAUSED, timeout=3000)
    qtbot.waitUntil(lambda: window.pause_button.isEnabled(), timeout=1000)
    assert window.pause_button.isEnabled()
    assert not window.resume_button.isEnabled()
    window.pause_round()
    assert "手动暂停" in window.engine.snapshot().reason
    assert window.resume_button.isEnabled()
    backend.foreground_active = True
    window.resume_round()
    qtbot.waitUntil(lambda: window.engine.snapshot().state == State.UNCONFIRMED, timeout=5000)
    snapshot = window.engine.snapshot()
    assert "仅提醒模式未发送输入" in snapshot.reason
    assert input_adapter.calls == []

    window.mode_combo.setCurrentIndex(1)
    assert window.mode == Mode.AUTO
    previous_session = window.engine.snapshot().session_id
    assert input_adapter.calls == []
    window.start_round()
    qtbot.waitUntil(
        lambda: (window.engine.snapshot().session_id != previous_session
                 and window.engine.snapshot().state == State.SUCCESS),
        timeout=6000,
    )
    snapshot = window.engine.snapshot()
    assert input_adapter.calls == [(112, 84)]

    hotkey = window._hotkey
    assert window.engine._hotkey_ready()
    hotkey.available = False
    assert not window.engine._hotkey_ready()
    qtbot.waitUntil(lambda: window.worker._last_hotkey_availability is False, timeout=3000)
    window.start_round()
    qtbot.waitUntil(lambda: window.status_label.text().startswith("无法开始"), timeout=3000)
    assert input_adapter.calls == [(112, 84)]


def test_invalid_button_recollection_clears_old_engine_binding(qtbot, close_main_windows) -> None:
    backend = FakeBackend()
    window = MainWindow(backend=backend, input_adapter=FakeInput(), store=FakeStore(),
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()

    qtbot.waitUntil(lambda: window.target_combo.count() == 2, timeout=3000)
    window.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window.geometry == backend.geometry, timeout=3000)
    window.capture_static()
    qtbot.waitUntil(lambda: window._static_frame is not None, timeout=3000)
    window.selection_combo.setCurrentIndex(1)
    window._selection_made(Rect(76, 42, 32, 24))
    assert window.engine.button is not None

    window._selection_made(Rect(80, 44, 1, 1))

    assert window.button is None
    assert window.engine.button is None
    assert not window.start_button.isEnabled()
    window.start_round()
    assert window.engine.snapshot().state == State.DISARMED


def test_hotkey_revokes_start_while_worker_is_inside_start(qtbot, close_main_windows) -> None:
    backend = FakeBackend()
    input_adapter = FakeInput()
    window = MainWindow(backend=backend, input_adapter=input_adapter, store=FakeStore(),
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: window.target_combo.count() == 2, timeout=3000)
    window.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window.geometry == backend.geometry, timeout=3000)
    window.capture_static()
    qtbot.waitUntil(lambda: window._static_frame is not None, timeout=3000)
    window.selection_combo.setCurrentIndex(1)
    window._selection_made(Rect(76, 42, 32, 24))
    window.mode_combo.setCurrentIndex(1)

    entered_start = threading.Event()
    release_start = threading.Event()
    real_start = window.engine.start

    def blocked_start() -> str:
        entered_start.set()
        assert release_start.wait(3)
        return real_start()

    window.engine.start = blocked_start
    window.start_round()
    qtbot.waitUntil(entered_start.is_set, timeout=3000)
    window._hotkey.callback()
    release_start.set()

    qtbot.waitUntil(lambda: window._pending_start_request is None, timeout=3000)
    assert window.engine.snapshot().state == State.DISARMED
    assert input_adapter.calls == []


def test_failed_restart_shows_frozen_previous_duration(qtbot, close_main_windows) -> None:
    backend = FakeBackend()
    input_adapter = FakeInput()
    window = MainWindow(backend=backend, input_adapter=input_adapter, store=FakeStore(),
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    qtbot.waitUntil(lambda: window.target_combo.count() == 2, timeout=3000)
    window.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window.geometry == backend.geometry, timeout=3000)
    window.capture_static()
    qtbot.waitUntil(lambda: window._static_frame is not None, timeout=3000)
    window.selection_combo.setCurrentIndex(1)
    window._selection_made(Rect(76, 42, 32, 24))
    fake_clock = FakeClock(backend._last_captured_at + 0.01)
    backend.clock = window.engine.clock = fake_clock
    backend.foreground_active = False
    window.start_round()
    qtbot.waitUntil(lambda: window.engine.snapshot().state == State.PAUSED, timeout=3000)
    window.stop_round()
    completed = window.engine.snapshot()

    def minimized(_identity):
        raise RuntimeError("The selected window is minimized")

    backend.inspect = minimized
    window.start_round()
    qtbot.waitUntil(lambda: window.status_label.text().startswith("无法开始："), timeout=3000)
    expected = f"上轮用时（已结束）：{completed.elapsed:.1f} 秒"
    qtbot.waitUntil(lambda: expected in window.candidates_text.toPlainText(), timeout=3000)
    with fake_clock.lock:
        fake_clock.value += 180
    qtbot.wait(250)
    window._refresh_worker_state()
    snapshot = window.engine.snapshot()
    assert snapshot.state == State.DISARMED
    assert snapshot.session_id == completed.session_id
    assert snapshot.elapsed == completed.elapsed
    assert expected in window.candidates_text.toPlainText()
    assert not window.pause_button.isEnabled()
    assert not window.resume_button.isEnabled()
    assert not window.stop_button.isEnabled()
    assert input_adapter.calls == []


def test_startup_smoke_test_closes_without_input(qtbot) -> None:
    del qtbot
    assert main(["--smoke-test"]) == 0


def test_loading_config_never_selects_or_enables_a_target(qtbot, close_main_windows) -> None:
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=FakeStore(),
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()

    window.load_config()
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)
    assert window.identity is None
    assert window.target_combo.currentIndex() == 0
    assert window.engine.snapshot().state == State.DISARMED
    assert not window.start_button.isEnabled()


def test_profile_dialogs_save_switch_and_update_the_selected_profile(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "default")
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    profile_a = tmp_path / "profiles" / "profile-a.json"
    profile_b = tmp_path / "profiles" / "profile-b.json"
    save_paths = iter((profile_a, profile_b))
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *_args: (str(next(save_paths)), "JSON 配置 (*.json)"),
    )

    window.threshold_spin.setValue(0.901)
    QTest.mouseClick(window.save_as_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert window._current_config_path == profile_a
    assert window.current_config_label.text() == "当前配置：profile-a.json"
    assert window.current_config_label.toolTip() == str(profile_a)
    assert store.load_from(profile_a)[0].threshold == pytest.approx(0.901)

    window.threshold_spin.setValue(0.921)
    QTest.mouseClick(window.save_as_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert window._current_config_path == profile_b
    assert store.load_from(profile_b)[0].threshold == pytest.approx(0.921)

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName",
        lambda *_args: (str(profile_a), "JSON 配置 (*.json)"),
    )
    window.threshold_spin.setValue(0.941)
    QTest.mouseClick(window.load_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert window.settings.threshold == pytest.approx(0.901)
    assert window._current_config_path == profile_a
    assert window.mode == Mode.REMIND
    assert window.identity is None
    assert window.engine.snapshot().state == State.DISARMED

    window.threshold_spin.setValue(0.911)
    window.save_config()
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert store.load_from(profile_a)[0].threshold == pytest.approx(0.911)


def test_profile_load_cancel_and_failure_preserve_config_and_path(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "default")
    broken_profile = tmp_path / "broken.json"
    broken_profile.write_text("not json", encoding="utf-8")
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    original_settings = window.settings
    original_path = window._current_config_path
    selected_path = [""]
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName",
        lambda *_args: (selected_path[0], "JSON 配置 (*.json)"),
    )

    window.choose_config_to_load()
    assert window.settings == original_settings
    assert window._current_config_path == original_path
    assert window.engine.snapshot().state == State.DISARMED

    selected_path[0] = str(broken_profile)
    window.choose_config_to_load()
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert window.settings == original_settings
    assert window._current_config_path == original_path
    assert window.current_config_label.text() == "当前配置：config.json"
    assert not window._config_loading
    assert window.engine.snapshot().state == State.DISARMED


def test_stale_profile_load_releases_controls_without_applying_or_relabeling(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "default")
    profile = tmp_path / "stale.json"
    ConfigStore(tmp_path).save_to(profile, Settings(threshold=0.903), Region(), None, None)
    entered_load = threading.Event()
    release_load = threading.Event()
    load_from = store.load_from

    def blocked_load(path):
        entered_load.set()
        assert release_load.wait(3)
        return load_from(path)

    monkeypatch.setattr(store, "load_from", blocked_load)
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName",
        lambda *_args: (str(profile), "JSON 配置 (*.json)"),
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    original_path = window._current_config_path

    window.choose_config_to_load()
    qtbot.waitUntil(entered_load.is_set, timeout=3000)
    window.threshold_spin.setValue(0.947)
    release_load.set()
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)

    assert window.settings.threshold == pytest.approx(0.947)
    assert window._current_config_path == original_path
    assert not window._config_loading
    assert window.engine.snapshot().state == State.DISARMED
    assert not window.start_button.isEnabled()


def test_stale_save_as_writes_captured_path_without_switching_current_profile(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "default")
    profile = tmp_path / "saved-snapshot.json"
    entered_save = threading.Event()
    release_save = threading.Event()
    save_to = store.save_to

    def blocked_save(path, settings, region, button, success):
        entered_save.set()
        assert release_save.wait(3)
        return save_to(path, settings, region, button, success)

    monkeypatch.setattr(store, "save_to", blocked_save)
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *_args: (str(profile), "JSON 配置 (*.json)"),
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    original_path = window._current_config_path
    original_label = window.current_config_label.text()
    window.threshold_spin.setValue(0.911)

    QTest.mouseClick(window.save_as_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(entered_save.is_set, timeout=3000)
    window.threshold_spin.setValue(0.931)
    release_save.set()
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)

    assert profile.is_file()
    assert store.load_from(profile)[0].threshold == pytest.approx(0.911)
    assert window.settings.threshold == pytest.approx(0.931)
    assert window._current_config_path == original_path
    assert window.current_config_label.text() == original_label


def test_cancelled_template_import_stops_active_round_before_opening_picker(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    backend = FakeBackend()
    input_adapter = FakeInput()
    store = ConfigStore(tmp_path)
    window = MainWindow(backend=backend, input_adapter=input_adapter, store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: window.target_combo.count() == 2, timeout=3000)
    window.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window.geometry == backend.geometry, timeout=3000)
    old_template = Template(
        backend.success_image[42:66, 130:162].copy(),
        (backend.geometry.width, backend.geometry.height), backend.geometry.dpi,
        "保留的按钮模板",
    )
    window.button = old_template
    assert window._configure_engine()
    assert window.engine.start()
    qtbot.waitUntil(lambda: window.engine.snapshot().state == State.MONITORING, timeout=3000)
    window.selection_combo.setCurrentIndex(1)
    original_path = window._current_config_path
    picker_state: list[State] = []

    def cancel_import(*_args):
        picker_state.append(window.engine.snapshot().state)
        assert input_adapter.calls == []
        return "", "PNG 图片 (*.png)"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", cancel_import)
    window.import_template()

    assert picker_state == [State.DISARMED]
    assert window.engine.snapshot().state == State.DISARMED
    assert input_adapter.calls == []
    assert window.button is old_template
    assert window._current_config_path == original_path
    assert window._pending_import_request is None


def test_profile_and_import_actions_exclude_each_other_and_release_after_failure(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "default")
    profile = tmp_path / "profile.json"
    ConfigStore(tmp_path / "profiles").save_to(
        profile, Settings(threshold=0.913), Region(), None, None
    )
    load_entered = threading.Event()
    release_load = threading.Event()
    load_from = store.load_from

    def blocked_load(path):
        load_entered.set()
        assert release_load.wait(3)
        return load_from(path)

    monkeypatch.setattr(store, "load_from", blocked_load)
    open_calls: list[str] = []
    dialogs = [(str(profile), "JSON 配置 (*.json)"), ("fake-template.png", "PNG (*.png)")]

    def select_dialog(_parent, title, *_args):
        open_calls.append(title)
        return dialogs[len(open_calls) - 1]

    monkeypatch.setattr(QFileDialog, "getOpenFileName", select_dialog)
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: window.target_combo.count() == 2, timeout=3000)
    window.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window.geometry == window.backend.geometry, timeout=3000)

    window.choose_config_to_load()
    qtbot.waitUntil(load_entered.is_set, timeout=3000)
    assert not window.import_button.isEnabled()
    window.import_template()
    assert open_calls == ["选择并载入配置"]

    release_load.set()
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert window.import_button.isEnabled()

    import_entered = threading.Event()
    release_import = threading.Event()

    def blocked_template_load(_path, _geometry):
        import_entered.set()
        assert release_import.wait(3)
        raise ValueError("mock template failure")

    monkeypatch.setattr("auto_ready.config.load_template", blocked_template_load)
    window.selection_combo.setCurrentIndex(1)
    window.import_template()
    qtbot.waitUntil(import_entered.is_set, timeout=3000)
    assert not window.load_button.isEnabled()
    window.choose_config_to_load()
    assert len(open_calls) == 2

    release_import.set()
    qtbot.waitUntil(lambda: window._pending_import_request is None, timeout=3000)
    assert window.load_button.isEnabled()
    assert window.import_button.isEnabled()


def test_profile_result_survives_generic_worker_result_overflow(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "default")
    profile = tmp_path / "durable.json"
    ConfigStore(tmp_path / "profiles").save_to(
        profile, Settings(threshold=0.917), Region(), None, None
    )
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName",
        lambda *_args: (str(profile), "JSON 配置 (*.json)"),
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    window._timer.stop()
    window.choose_config_to_load()
    qtbot.waitUntil(lambda: window.worker._profile_result is not None, timeout=3000)
    profile_result = window.worker._profile_result
    assert profile_result is not None
    for request_id in range(100, 112):
        window.worker._record_result(_WorkerResult(request_id, "tick", error="noise"))

    results = window.worker.take_results()
    assert len(results) == 9
    assert results[-1] == profile_result
    for result in results:
        window._handle_result(result)

    assert window.settings.threshold == pytest.approx(0.917)
    assert window._current_config_path == profile
    assert window._pending_profile_request is None
    assert not window._config_loading
    assert window.load_button.isEnabled()


def test_legacy_f12_config_requires_explicit_migration_without_rewriting_file(
    qtbot, tmp_path, close_main_windows
) -> None:
    legacy_hotkey = " cTrL + aLt + f 12 "
    store = ConfigStore(tmp_path)
    store.save(Settings(hotkey=legacy_hotkey), Region(), None, None)
    original_config = store.config_path.read_bytes()
    created: list[FakeHotkey] = []

    def factory(spec: str, callback: Callable[[], None]) -> FakeHotkey:
        hotkey = FakeHotkey(spec, callback)
        created.append(hotkey)
        return hotkey

    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=factory, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    window.load_config()
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)

    assert window.settings.hotkey == legacy_hotkey
    assert window.hotkey_combo.currentText() == legacy_hotkey
    assert "F12 已保留" in window.hotkey_status_label.text()
    assert window._hotkey is None
    assert [hotkey.spec for hotkey in created] == ["Ctrl+Alt+F11"]
    assert not window.mode_combo.model().item(1).isEnabled()
    assert store.config_path.read_bytes() == original_config

    window.hotkey_combo.setCurrentText("Ctrl+Alt+F11")
    assert window.settings.hotkey == "Ctrl+Alt+F11"
    assert window._hotkey_available
    assert window.mode_combo.model().item(1).isEnabled()
    assert [hotkey.spec for hotkey in created] == ["Ctrl+Alt+F11", "Ctrl+Alt+F11"]
    assert store.config_path.read_bytes() == original_config


def test_custom_hotkey_value_is_preserved_exactly_on_config_load(
    qtbot, tmp_path, close_main_windows
) -> None:
    custom_hotkey = " ctrl + shift + f10 "
    store = ConfigStore(tmp_path)
    store.save(Settings(hotkey=custom_hotkey), Region(), None, None)
    created: list[FakeHotkey] = []

    def factory(spec: str, callback: Callable[[], None]) -> FakeHotkey:
        hotkey = FakeHotkey(spec, callback)
        created.append(hotkey)
        return hotkey

    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=factory, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    window.load_config()
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)

    assert window.settings.hotkey == custom_hotkey
    assert window.hotkey_combo.currentText() == custom_hotkey
    assert created[-1].spec == custom_hotkey
    assert window._hotkey_available


def test_hotkey_registration_conflict_disables_auto_without_fallback(
    qtbot, close_main_windows
) -> None:
    requested: list[str] = []

    class ConflictingHotkey(FakeHotkey):
        def __init__(self, spec: str, callback: Callable[[], None]) -> None:
            super().__init__(spec, callback)
            self.available = False
            self.error = "快捷键已被占用"

        def start(self) -> bool:
            return False

    def factory(spec: str, callback: Callable[[], None]) -> ConflictingHotkey:
        requested.append(spec)
        return ConflictingHotkey(spec, callback)

    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=FakeStore(),
                        hotkey_factory=factory, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()

    assert requested == ["Ctrl+Alt+F11"]
    assert not window._hotkey_available
    assert "快捷键已被占用" in window.hotkey_status_label.text()
    assert not window.mode_combo.model().item(1).isEnabled()
    assert not window.start_button.isEnabled()


def test_hotkey_text_change_revokes_a_queued_start(qtbot, close_main_windows) -> None:
    backend = FakeBackend()
    input_adapter = FakeInput()
    window = MainWindow(backend=backend, input_adapter=input_adapter, store=FakeStore(),
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: window.target_combo.count() == 2, timeout=3000)
    window.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window.geometry == backend.geometry, timeout=3000)
    window.capture_static()
    qtbot.waitUntil(lambda: window._static_frame is not None, timeout=3000)
    window.selection_combo.setCurrentIndex(1)
    window._selection_made(Rect(76, 42, 32, 24))
    window.mode_combo.setCurrentIndex(1)

    entered_start = threading.Event()
    release_start = threading.Event()
    real_start = window.engine.start

    def blocked_start() -> str:
        entered_start.set()
        assert release_start.wait(3)
        return real_start()

    window.engine.start = blocked_start
    window.start_round()
    qtbot.waitUntil(entered_start.is_set, timeout=3000)
    window.hotkey_combo.setCurrentText("Ctrl+Alt+F10")
    release_start.set()

    qtbot.waitUntil(lambda: window._pending_start_request is None, timeout=3000)
    assert window.engine.snapshot().state == State.DISARMED
    assert window.settings.hotkey == "Ctrl+Alt+F10"
    assert input_adapter.calls == []

    backend.foreground_active = False
    window.start_round()
    qtbot.waitUntil(lambda: window.engine.snapshot().state == State.PAUSED, timeout=3000)
    qtbot.waitUntil(lambda: window.worker._last_hotkey_availability is True, timeout=3000)
    window._hotkey.available = False
    qtbot.waitUntil(lambda: window.engine.snapshot().state == State.ERROR, timeout=3000)
    assert input_adapter.calls == []
