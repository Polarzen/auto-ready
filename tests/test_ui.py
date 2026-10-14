from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QMessageBox, QSystemTrayIcon

from auto_ready.app import main
from auto_ready.config import ConfigStore, ProfileMetadata
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
from auto_ready.profile_dialogs import ProfileSelectionDialog
from auto_ready.ui import ImagePreview, MainWindow, _IOWorker, _WorkerResult, close_and_wait


@pytest.fixture
def close_main_windows(qtbot):
    windows = []

    def register(window, *, wait_for_startup: bool = True):
        qtbot.addWidget(window)
        windows.append(window)
        if wait_for_startup:
            qtbot.waitUntil(
                lambda: (
                    window._startup_default_load_started
                    and window._pending_profile_request is None
                    and not window._config_loading
                ),
                timeout=5000,
            )
        return window

    yield register

    for window in windows:
        close_and_wait(window)


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
        self.metadata = ProfileMetadata("默认配置", "未标注游戏")

    def save(self, *values: object, metadata: ProfileMetadata | None = None) -> None:
        self.saved = values
        if metadata is not None:
            self.metadata = metadata

    def load_profile(self, _path=None) -> tuple[object, ...]:
        from auto_ready.models import Region, Settings

        return Settings(), Region(), None, None, self.metadata

    def read_profile_metadata(self, _path=None) -> ProfileMetadata:
        return self.metadata


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
    def __init__(self, start: float, step: float = 0.02) -> None:
        self.value = start
        self.step = step
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            current = self.value
            self.value += self.step
            return current


def _profile_index(combo, path: Path) -> int:
    expected = Path(path).resolve()
    return next(
        (index for index in range(combo.count())
         if combo.itemData(index) is not None
         and Path(combo.itemData(index)).resolve() == expected),
        -1,
    )


def _template(backend: FakeBackend, name: str = "测试按钮") -> Template:
    return Template(
        backend.button_image[42:66, 76:108].copy(),
        (backend.geometry.width, backend.geometry.height),
        backend.geometry.dpi,
        name,
    )


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
    assert "下一步：" in window.status_label.text()
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
    first_frame = window._static_frame
    window.capture_static()
    qtbot.waitUntil(lambda: window._static_frame is not first_frame, timeout=3000)
    assert window.button is button_template
    assert window.region == expected_region
    window.selection_combo.setCurrentIndex(3)
    window._selection_made(Rect(130, 42, 32, 24))
    assert window.success is not None
    success_template = window.success
    assert window.button is button_template

    backend.show_button()
    second_frame = window._static_frame
    window.capture_static()
    qtbot.waitUntil(lambda: window._static_frame is not second_frame, timeout=3000)
    assert window.button is button_template
    assert window.success is success_template
    assert window.region == expected_region

    window.frames_spin.setValue(2)
    window.stable_spin.setValue(0.2)
    fake_clock = FakeClock(backend._last_captured_at + 0.001, step=0.01)
    backend.clock = fake_clock
    window.engine.clock = fake_clock

    preview_results = []
    handle_result = window._handle_result

    def record_result(result):
        handle_result(result)
        if result.action == "preview":
            preview_results.append(result)

    window._handle_result = record_result
    window.preview_without_input()
    qtbot.waitUntil(lambda: bool(preview_results), timeout=3000)
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
    qtbot.waitUntil(lambda: window.engine.snapshot().state == State.REMINDED, timeout=5000)
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
                 and window.engine.snapshot().state in {
                     State.SUCCESS, State.UNCONFIRMED, State.ERROR, State.REMINDED,
                 }),
        timeout=10000,
    )
    snapshot = window.engine.snapshot()
    assert snapshot.state == State.SUCCESS, snapshot
    assert input_adapter.calls == [(112, 84)]

    hotkey = window._hotkey
    assert window.engine._hotkey_ready()
    hotkey.available = False
    assert not window.engine._hotkey_ready()
    qtbot.waitUntil(lambda: window.worker._last_hotkey_availability is False, timeout=3000)
    window.start_round()
    qtbot.waitUntil(lambda: not window.start_button.isEnabled(), timeout=3000)
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
    fake_clock = FakeClock(backend._last_captured_at + 0.001, step=0.01)
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


def test_startup_loads_default_template_pixels_and_static_view_without_rewriting(
    qtbot, tmp_path, close_main_windows
) -> None:
    backend = FakeBackend()
    store = ConfigStore(tmp_path / "profiles")
    button = _template(backend, "默认按钮")
    success = Template(
        backend.success_image[42:66, 130:162].copy(),
        (backend.geometry.width, backend.geometry.height),
        backend.geometry.dpi,
        "默认成功标志",
    )
    metadata = ProfileMetadata("真实默认名称", "真实游戏")
    store.save(Settings(threshold=0.934), Region(), button, success, metadata=metadata)
    config_bytes = store.config_path.read_bytes()
    assets = {path: path.read_bytes() for path in store.templates_path.glob("*.png")}

    window = MainWindow(
        backend=backend, input_adapter=FakeInput(), store=store,
        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0,
    )
    close_main_windows(window)

    assert window._profile_content_ready
    assert window.settings.threshold == pytest.approx(0.934)
    assert window._current_profile_metadata == metadata
    assert np.array_equal(window.button.image, button.image)
    assert window.success is not None
    assert np.array_equal(window.success.image, success.image)
    assert window.identity is None
    assert window.mode == Mode.REMIND
    assert backend.sequence == 0
    assert not window.button_template_thumbnail.pixmap().isNull()
    window.show_template_preview()
    assert not window.template_preview_dialog.button_card.image.pixmap().isNull()
    assert "DPI 96" in window.template_preview_dialog.button_card.details.text()
    assert store.config_path.read_bytes() == config_bytes
    assert {path: path.read_bytes() for path in store.templates_path.glob("*.png")} == assets


def test_corrupt_startup_profile_blocks_writes_and_allows_explicit_recovery(
    qtbot, tmp_path, close_main_windows
) -> None:
    backend = FakeBackend()
    store = ConfigStore(tmp_path / "profiles")
    store.root.mkdir(parents=True)
    store.config_path.write_bytes(b"not a profile")
    corrupt_bytes = store.config_path.read_bytes()
    recovery_path = tmp_path / "recovery.json"
    recovery_button = _template(backend, "恢复按钮")
    store.save_to(
        recovery_path, Settings(threshold=0.941), Region(), recovery_button, None,
        metadata=ProfileMetadata("恢复配置", "恢复游戏"),
    )

    window = MainWindow(
        backend=backend, input_adapter=FakeInput(), store=store,
        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0,
    )
    close_main_windows(window)
    assert not window._profile_content_ready
    assert "载入失败" in window.profile_status_label.text()
    assert not window.save_button.isEnabled()
    assert not window.rename_profile_button.isEnabled()
    assert not window.onboarding_button.isEnabled()

    window.save_config()
    window.rename_profile()
    assert store.config_path.read_bytes() == corrupt_bytes

    window.load_config(recovery_path)
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)
    assert window._profile_content_ready
    assert window._current_config_path == recovery_path
    assert window.settings.threshold == pytest.approx(0.941)
    assert np.array_equal(window.button.image, recovery_button.image)


def test_startup_default_load_retries_queue_rejection(qtbot, tmp_path, close_main_windows) -> None:
    backend = FakeBackend()
    store = ConfigStore(tmp_path / "profiles")
    store.save(Settings(threshold=0.927), Region(), _template(backend), None)
    window = MainWindow(
        backend=backend, input_adapter=FakeInput(), store=store,
        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0,
    )
    submit = window.worker.submit
    attempts = 0

    def reject_first_default_load(action, payload=None):
        nonlocal attempts
        if action == "load_config" and payload is not None and payload[0] is None:
            attempts += 1
            if attempts == 1:
                return None
        return submit(action, payload)

    window.worker.submit = reject_first_default_load
    close_main_windows(window)

    assert attempts == 2
    assert window._profile_content_ready
    assert window.settings.threshold == pytest.approx(0.927)


def test_named_profile_reuse_needs_no_capture_and_incompatible_target_keeps_crop(
    qtbot, tmp_path, close_main_windows
) -> None:
    backend = FakeBackend()
    store = ConfigStore(tmp_path / "profiles")
    default_button = _template(backend, "默认按钮")
    named_button = Template(
        backend.success_image[42:66, 130:162].copy(),
        (backend.geometry.width, backend.geometry.height),
        backend.geometry.dpi,
        "命名按钮",
    )
    incompatible_button = Template(
        named_button.image.copy(),
        (backend.geometry.width + 1, backend.geometry.height),
        backend.geometry.dpi,
        "尺寸不匹配按钮",
    )
    store.save(Settings(), Region(), default_button, None)
    named_path = store.root / "named.json"
    incompatible_path = store.root / "incompatible.json"
    store.save_to(
        named_path, Settings(), Region(), named_button, None,
        metadata=ProfileMetadata("命名配置", "模拟游戏"),
    )
    store.save_to(
        incompatible_path, Settings(), Region(), incompatible_button, None,
        metadata=ProfileMetadata("不匹配配置", "模拟游戏"),
    )
    window = MainWindow(
        backend=backend, input_adapter=FakeInput(), store=store,
        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0,
    )
    close_main_windows(window)
    assert np.array_equal(window.button.image, default_button.image)

    window.load_config(named_path)
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)
    assert np.array_equal(window.button.image, named_button.image)
    qtbot.waitUntil(lambda: window.target_combo.count() == 2, timeout=3000)
    window.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window.geometry == backend.geometry, timeout=3000)
    assert backend.sequence == 0
    assert window.start_button.isEnabled()
    assert "无需重复框选" in window.template_preview_dialog.compatibility_label.text()

    window.load_config(incompatible_path)
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)
    assert np.array_equal(window.button.image, incompatible_button.image)
    assert not window.start_button.isEnabled()
    assert "模板保留" in window.template_preview_dialog.compatibility_label.text()
    assert backend.sequence == 0


def test_profile_dialogs_save_switch_and_update_the_selected_profile(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "default")
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    window.show_advanced()
    profile_a = tmp_path / "profiles" / "profile-a.json"
    profile_b = tmp_path / "profiles" / "profile-b.json"
    save_paths = iter((profile_a, profile_b))
    metadata_values = iter((
        ProfileMetadata("配置甲", "游戏甲"),
        ProfileMetadata("配置乙", "游戏乙"),
    ))
    monkeypatch.setattr(
        window, "_edit_profile_metadata",
        lambda _title, _description, _initial: next(metadata_values),
    )
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *_args: (str(next(save_paths)), "JSON 配置 (*.json)"),
    )

    window.threshold_spin.setValue(0.901)
    QTest.mouseClick(window.save_as_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert window._current_config_path == profile_a
    assert window.current_config_label.text() == "当前配置：配置甲\n对应游戏：游戏甲"
    assert window.current_config_label.toolTip() == str(profile_a)
    assert store.load_from(profile_a)[0].threshold == pytest.approx(0.901)
    assert store.load_profile(profile_a)[4] == ProfileMetadata("配置甲", "游戏甲")

    window.threshold_spin.setValue(0.921)
    QTest.mouseClick(window.save_as_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert window._current_config_path == profile_b
    assert store.load_from(profile_b)[0].threshold == pytest.approx(0.921)
    assert store.load_profile(profile_b)[4] == ProfileMetadata("配置乙", "游戏乙")

    def choose_first_profile(dialog):
        dialog.choice = next(
            choice for choice in window._profile_choices()
            if choice.path == profile_a.resolve()
        )
        return 1

    monkeypatch.setattr(ProfileSelectionDialog, "exec", choose_first_profile)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args: QMessageBox.StandardButton.Discard,
    )
    window.threshold_spin.setValue(0.941)
    QTest.mouseClick(window.load_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert window.settings.threshold == pytest.approx(0.901)
    assert window._current_config_path == profile_a
    assert window._current_profile_metadata == ProfileMetadata("配置甲", "游戏甲")
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
    assert window.current_config_label.text() == (
        "当前配置：默认配置\n对应游戏：未标注游戏"
    )
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
    load_profile = store.load_profile

    def blocked_load(path):
        entered_load.set()
        assert release_load.wait(3)
        return load_profile(path)

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName",
        lambda *_args: (str(profile), "JSON 配置 (*.json)"),
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    window.show_advanced()
    monkeypatch.setattr(store, "load_profile", blocked_load)
    original_path = window._current_config_path
    original_metadata = window._current_profile_metadata
    original_label = window.current_config_label.text()

    window.choose_config_to_load()
    qtbot.waitUntil(entered_load.is_set, timeout=3000)
    window.threshold_spin.setValue(0.947)
    release_load.set()
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)

    assert window.settings.threshold == pytest.approx(0.947)
    assert window._current_config_path == original_path
    assert window._current_profile_metadata == original_metadata
    assert window.current_config_label.text() == f"{original_label} · 未保存修改"
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

    def blocked_save(path, settings, region, button, success, *, metadata=None):
        entered_save.set()
        assert release_save.wait(3)
        return save_to(path, settings, region, button, success, metadata=metadata)

    monkeypatch.setattr(store, "save_to", blocked_save)
    metadata = ProfileMetadata("快照名称", "快照游戏")
    monkeypatch.setattr(
        # The Save As dialog captures the immutable metadata alongside the settings.
        # The stale completion must not apply it to the active profile.
        MainWindow, "_edit_profile_metadata",
        lambda _self, _title, _description, _initial: metadata,
    )
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *_args: (str(profile), "JSON 配置 (*.json)"),
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    window.show_advanced()
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
    assert store.load_profile(profile)[4] == metadata
    assert window.settings.threshold == pytest.approx(0.931)
    assert window._current_config_path == original_path
    assert window._current_profile_metadata == ProfileMetadata("默认配置", "未标注游戏")
    assert window.current_config_label.text().startswith(original_label)
    assert "未保存修改" in window.current_config_label.text()


def test_rename_cancellation_failure_and_retry_keep_profile_identity_atomic(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "profiles")
    active = store.root / "active.json"
    old_metadata = ProfileMetadata("原名称", "原游戏")
    store.save_to(active, Settings(), Region(), None, None, metadata=old_metadata)
    original_bytes = active.read_bytes()
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    window._set_current_config_path(active)
    window._set_current_profile_metadata(old_metadata)
    window._saved_config_signature = window._config_signature()
    old_label = window.current_config_label.text()

    monkeypatch.setattr(window, "_edit_profile_metadata", lambda *_args: None)
    window.rename_profile()
    assert active.read_bytes() == original_bytes
    assert window._current_profile_metadata == old_metadata
    assert window.current_config_label.text() == old_label

    new_metadata = ProfileMetadata("新名称", "新游戏")
    monkeypatch.setattr(window, "_edit_profile_metadata", lambda *_args: new_metadata)
    original_save_to = store.save_to
    save_failed = threading.Event()

    def failed_save(*_args, **_kwargs):
        save_failed.set()
        raise OSError("模拟磁盘失败")

    monkeypatch.setattr(store, "save_to", failed_save)
    window.threshold_spin.setValue(0.923)
    window.rename_profile()
    qtbot.waitUntil(save_failed.is_set, timeout=3000)
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert window._current_config_path == active
    assert window._current_profile_metadata == old_metadata
    assert window.current_config_label.text().startswith(old_label)
    assert active.read_bytes() == original_bytes

    monkeypatch.setattr(store, "save_to", original_save_to)
    window.rename_profile()
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    saved_settings, _region, _button, _success, saved_metadata = store.load_profile(active)
    assert saved_settings.threshold == pytest.approx(0.923)
    assert saved_metadata == new_metadata
    assert window._current_config_path == active
    assert window._current_profile_metadata == new_metadata
    assert window.current_config_label.text() == "当前配置：新名称\n对应游戏：新游戏"


def test_save_as_metadata_cancellation_has_no_file_or_profile_write(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "profiles")
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    original_path = window._current_config_path
    original_metadata = window._current_profile_metadata
    picker_calls = []
    monkeypatch.setattr(window, "_edit_profile_metadata", lambda *_args: None)
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *_args: picker_calls.append(True)
    )

    window.save_config_as()

    assert picker_calls == []
    assert window._current_config_path == original_path
    assert window._current_profile_metadata == original_metadata
    assert not store.config_path.exists()
    assert not store.root.exists()


def test_duplicate_profile_labels_show_filename_and_path_in_chooser(
    qtbot, tmp_path, close_main_windows
) -> None:
    store = ConfigStore(tmp_path / "profiles")
    metadata = ProfileMetadata("同名配置", "同一游戏")
    first = store.root / "first.json"
    second = store.root / "second.json"
    store.save_to(first, Settings(), Region(), None, None, metadata=metadata)
    store.save_to(second, Settings(), Region(), None, None, metadata=metadata)
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: first.resolve() in window._known_profiles
                    and second.resolve() in window._known_profiles, timeout=3000)

    first_index = _profile_index(window.profile_combo, first)
    second_index = _profile_index(window.profile_combo, second)
    first_label = window.profile_combo.itemText(first_index)
    second_label = window.profile_combo.itemText(second_index)
    assert first_label != second_label
    assert "first.json" in first_label
    assert "second.json" in second_label
    choices = window._profile_choices()
    assert any(
        str(first) in choice.label and choice.path == first.resolve() for choice in choices
    )
    assert any(
        str(second) in choice.label and choice.path == second.resolve() for choice in choices
    )


def test_legacy_profile_fallback_is_saveable_and_used_for_dirty_switch_save(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "legacy")
    store.save(Settings(threshold=0.921), Region(), None, None)
    candidate = store.root / "candidate.json"
    store.save_to(
        candidate, Settings(threshold=0.913), Region(), None, None,
        metadata=ProfileMetadata("候选配置", "候选游戏"),
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: store.config_path.resolve() in window._known_profiles, timeout=3000)
    assert window._current_profile_metadata == ProfileMetadata("默认配置", "未标注游戏")
    assert window.current_config_label.text() == "当前配置：默认配置\n对应游戏：未标注游戏"

    profile_refresh_started = threading.Event()
    release_profile_refresh = threading.Event()
    second_save_started = threading.Event()
    release_second_save = threading.Event()
    profile_refresh_applied = threading.Event()
    filler_profile_started = threading.Event()
    release_filler_profile = threading.Event()
    original_execute = window.worker._execute
    original_populate = window._populate_profile_combo
    save_count = 0
    profile_list_count = 0

    def gated_execute(action: str, payload: object) -> object:
        nonlocal profile_list_count, save_count
        if action == "list_profiles":
            profile_list_count += 1
            if profile_list_count == 1:
                profile_refresh_started.set()
                if not release_profile_refresh.wait(5):
                    raise RuntimeError("timed out waiting to release profile refresh")
            elif profile_list_count == 2:
                filler_profile_started.set()
                if not release_filler_profile.wait(5):
                    raise RuntimeError("timed out waiting to release queued profile reads")
        if action == "save_config":
            save_count += 1
            if save_count == 2:
                second_save_started.set()
                if not release_second_save.wait(5):
                    raise RuntimeError("timed out waiting to release second save")
        return original_execute(action, payload)

    def observe_profile_refresh(profiles: list[tuple[Path, ProfileMetadata]]) -> None:
        original_populate(profiles)
        if profile_refresh_started.is_set():
            profile_refresh_applied.set()

    monkeypatch.setattr(window.worker, "_execute", gated_execute)
    monkeypatch.setattr(window, "_populate_profile_combo", observe_profile_refresh)
    try:
        window.threshold_spin.setValue(0.931)
        window.save_config()
        qtbot.waitUntil(
            lambda: profile_refresh_started.is_set()
            and window._pending_profile_request is None,
            timeout=3000,
        )
        assert store.read_profile_metadata() == ProfileMetadata("默认配置", "未标注游戏")

        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *_args: QMessageBox.StandardButton.Save,
        )
        window.threshold_spin.setValue(0.941)
        window.profile_combo.setCurrentIndex(_profile_index(window.profile_combo, candidate))
        assert window._pending_profile_switch == candidate.resolve()
        assert window._pending_profile_action == "save_config"

        release_profile_refresh.set()
        qtbot.waitUntil(
            lambda: second_save_started.is_set() and profile_refresh_applied.is_set(),
            timeout=3000,
        )
        assert window._pending_profile_switch == candidate.resolve()
        assert window.profile_combo.currentData() == candidate.resolve()

        # Keep three harmless reads queued while one read is executing. The save
        # completion must reserve the last worker slot for the requested load.
        for _ in range(4):
            assert window.worker.submit("list_profiles", window._profile_roots()) is not None
        release_second_save.set()
        qtbot.waitUntil(
            lambda: filler_profile_started.is_set()
            and window._pending_profile_action == "load_config",
            timeout=3000,
        )
        assert window._pending_profile_switch_active

        release_filler_profile.set()
        qtbot.waitUntil(
            lambda: window._current_config_path == candidate.resolve()
            and window._pending_profile_request is None and not window._config_loading,
            timeout=5000,
        )
    finally:
        release_profile_refresh.set()
        release_second_save.set()
        release_filler_profile.set()

    assert store.load_profile(store.config_path)[4] == ProfileMetadata(
        "默认配置", "未标注游戏"
    )
    assert window._current_profile_metadata == ProfileMetadata("候选配置", "候选游戏")
    assert window._pending_profile_switch is None


def test_dirty_save_can_switch_back_to_default_profile(qtbot, tmp_path, close_main_windows,
                                                       monkeypatch) -> None:
    store = ConfigStore(tmp_path / "profiles")
    store.save(Settings(threshold=0.921), Region(), None, None,
               metadata=ProfileMetadata("默认", "游戏甲"))
    candidate = store.root / "candidate.json"
    store.save_to(
        candidate, Settings(threshold=0.913), Region(), None, None,
        metadata=ProfileMetadata("候选", "游戏乙"),
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: candidate.resolve() in window._known_profiles, timeout=3000)

    window.profile_combo.setCurrentIndex(_profile_index(window.profile_combo, candidate))
    qtbot.waitUntil(
        lambda: window._current_config_path == candidate.resolve()
        and window._pending_profile_request is None and not window._config_loading,
        timeout=3000,
    )
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args: QMessageBox.StandardButton.Save,
    )
    window.threshold_spin.setValue(0.941)
    window.profile_combo.setCurrentIndex(window.profile_combo.findData(None))
    assert window._pending_profile_switch is None
    assert window._pending_profile_switch_active

    qtbot.waitUntil(
        lambda: window._current_config_path == store.config_path.resolve()
        and window._pending_profile_request is None and not window._config_loading,
        timeout=5000,
    )
    assert not window._pending_profile_switch_active
    assert store.load_profile(candidate)[0].threshold == pytest.approx(0.941)
    assert window.settings.threshold == pytest.approx(0.921)
    assert window._current_profile_metadata == ProfileMetadata("默认", "游戏甲")


def test_profile_load_queue_rejection_keeps_current_profile(qtbot, tmp_path, close_main_windows,
                                                            monkeypatch) -> None:
    store = ConfigStore(tmp_path / "profiles")
    store.save(Settings(threshold=0.921), Region(), None, None,
               metadata=ProfileMetadata("默认", "游戏甲"))
    candidate = store.root / "candidate.json"
    store.save_to(
        candidate, Settings(threshold=0.913), Region(), None, None,
        metadata=ProfileMetadata("候选", "游戏乙"),
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: candidate.resolve() in window._known_profiles, timeout=3000)
    original_submit = window.worker.submit

    def reject_profile_load(action: str, payload: object = None) -> int | None:
        if action == "load_config":
            return None
        return original_submit(action, payload)

    monkeypatch.setattr(window.worker, "submit", reject_profile_load)
    window.profile_combo.setCurrentIndex(_profile_index(window.profile_combo, candidate))

    assert window._current_config_path == store.config_path
    assert window._current_profile_metadata == ProfileMetadata("默认", "游戏甲")
    assert window.profile_combo.currentData() is None
    assert window._pending_profile_request is None
    assert window._pending_profile_switch is None
    assert not window._pending_profile_switch_active
    assert not window._config_loading
    assert "未载入所选配置" in window.status_label.text()
    assert store.load_profile(store.config_path)[0].threshold == pytest.approx(0.921)


def test_long_legacy_filename_uses_bounded_fallback_and_can_be_saved(
    qtbot, tmp_path, close_main_windows
) -> None:
    store = ConfigStore(tmp_path / "profiles")
    profile = store.root / f"{'L' * 100}.json"
    store.save_to(profile, Settings(threshold=0.913), Region(), None, None)
    assert store.read_profile_metadata(profile) == ProfileMetadata()

    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: profile.resolve() in window._known_profiles, timeout=3000)
    index = _profile_index(window.profile_combo, profile)
    assert "L" * 80 in window.profile_combo.itemText(index)
    assert window.profile_combo.itemData(index, Qt.ItemDataRole.ToolTipRole) == str(
        profile.resolve()
    )

    window.profile_combo.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: window._current_config_path == profile.resolve()
        and window._pending_profile_request is None and not window._config_loading,
        timeout=3000,
    )
    assert window._current_profile_metadata == ProfileMetadata("L" * 80, "未标注游戏")
    assert window.current_config_label.toolTip() == str(profile.resolve())
    window.save_config()
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)
    assert store.load_profile(profile)[4] == ProfileMetadata("L" * 80, "未标注游戏")


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
    load_profile = store.load_profile

    def blocked_load(path):
        load_entered.set()
        assert release_load.wait(3)
        return load_profile(path)

    monkeypatch.setattr(store, "load_profile", blocked_load)
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


def test_dashboard_keeps_stop_visible_and_defaults_disarmed_and_unchecked(
    qtbot, close_main_windows
) -> None:
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=FakeStore(),
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.wait(20)

    assert window.stop_button.isVisible()
    assert not window.stop_button.isEnabled()
    assert window.mode == Mode.REMIND
    assert not window.allow_activation_checkbox.isChecked()
    assert window.start_button.text() == "开始等待"
    assert window.advanced_dialog.isHidden()
    assert window.advanced_dialog.isAncestorOf(window.load_button)
    assert window.advanced_dialog.isAncestorOf(window.save_as_button)


def test_capture_release_failure_is_retained_and_reported() -> None:
    class FailingReleaseBackend(FakeBackend):
        def release_capture(self) -> None:
            raise OSError("native stop failed")

    backend = FailingReleaseBackend()
    engine = Engine(backend, FakeInput(), lambda: None)
    worker = _IOWorker(engine, backend, FakeStore(), lambda *_args: None, lambda: True)
    worker._capture_may_be_active = True

    assert not worker._release_capture()
    assert worker.capture_release_errors() == ("OSError: native stop failed",)
    results = worker.take_results()
    assert [(result.action, result.error) for result in results] == [
        ("capture_release", "OSError: native stop failed")
    ]


@pytest.mark.parametrize(
    ("choice", "expected_threshold"),
    [
        (QMessageBox.StandardButton.Cancel, 0.931),
        (QMessageBox.StandardButton.Discard, 0.915),
        (QMessageBox.StandardButton.Save, 0.915),
    ],
)
def test_dirty_profile_switch_requires_explicit_choice(
    qtbot, tmp_path, close_main_windows, monkeypatch, choice, expected_threshold
) -> None:
    store = ConfigStore(tmp_path / "profiles")
    profile = store.root / "candidate.json"
    store.save_to(profile, Settings(threshold=0.915), Region(), None, None)
    answers = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: (answers.append(True), choice)[1]
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: _profile_index(window.profile_combo, profile) >= 0, timeout=3000)

    window.threshold_spin.setValue(0.931)
    index = _profile_index(window.profile_combo, profile)
    window.profile_combo.setCurrentIndex(index)
    qtbot.waitUntil(
        lambda: window._pending_profile_request is None and not window._config_loading
        and window.settings.threshold == pytest.approx(expected_threshold),
        timeout=5000,
    )

    assert answers == [True]
    if choice == QMessageBox.StandardButton.Cancel:
        assert window._current_config_path == store.config_path
        assert window.profile_combo.currentData() is None
    else:
        assert window._current_config_path == profile.resolve()
    if choice == QMessageBox.StandardButton.Save:
        assert store.load()[0].threshold == pytest.approx(0.931)


def test_failed_dirty_profile_save_prevents_switch(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    store = ConfigStore(tmp_path / "profiles")
    profile = store.root / "candidate.json"
    store.save_to(profile, Settings(threshold=0.915), Region(), None, None)

    def fail_save(*_args, **_kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(store, "save", fail_save)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Save
    )
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    qtbot.waitUntil(lambda: _profile_index(window.profile_combo, profile) >= 0, timeout=3000)
    window.threshold_spin.setValue(0.931)
    window.profile_combo.setCurrentIndex(_profile_index(window.profile_combo, profile))
    qtbot.waitUntil(lambda: window._pending_profile_request is None, timeout=3000)

    assert window.settings.threshold == pytest.approx(0.931)
    assert window._current_config_path == store.config_path
    assert window.profile_combo.currentData() is None
    assert window._pending_profile_switch is None
    assert "simulated disk failure" in window.status_label.text()


def test_stale_notification_is_discarded_and_tray_menu_restores_and_hides(
    qtbot, close_main_windows, monkeypatch
) -> None:
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    sounds = []
    window = MainWindow(backend=FakeBackend(), input_adapter=FakeInput(), store=FakeStore(),
                        hotkey_factory=FakeHotkey, notifier=lambda: sounds.append("sound"),
                        capture_delay=0)
    close_main_windows(window)
    window.show()
    menu = window._tray_icon.contextMenu()
    actions = {action.text(): action for action in menu.actions() if action.text()}
    assert {"显示主界面", "隐藏主界面", "停止本轮", "退出"}.issubset(actions)

    actions["隐藏主界面"].trigger()
    assert not window.isVisible()
    actions["显示主界面"].trigger()
    assert window.isVisible()

    snapshot = window.engine.snapshot()
    revision, epoch = window.worker.notification_context()
    window._emit_notification("当前通知")
    assert sounds == ["sound"]
    window.worker.invalidate_authorization()
    window._show_notification("过期通知", snapshot.session_id, snapshot.state.value,
                              revision, epoch)
    assert sounds == ["sound"]

    actions["停止本轮"].trigger()
    assert window.engine.snapshot().state == State.DISARMED
    actions["退出"].trigger()
    qtbot.waitUntil(lambda: window._closed, timeout=5000)
