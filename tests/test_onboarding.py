from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from auto_ready.config import ConfigStore
from auto_ready.models import (
    CaptureStatus,
    Detection,
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
from auto_ready.onboarding import OnboardingDraft, OnboardingWizard
from auto_ready.ui import MainWindow, close_and_wait


class FakeBackend:
    def __init__(self) -> None:
        self.identity = Identity(101, 202, 303, "mock-game.exe", "模拟目标")
        self.geometry = Geometry(20, 30, 200, 120, 96)
        self.image = np.full((120, 200, 3), 28, dtype=np.uint8)
        pattern = np.random.default_rng(9).integers(0, 255, (24, 32, 3), dtype=np.uint8)
        self.image[42:66, 76:108] = pattern
        self.sequence = 0
        self.releases = 0

    def list_windows(self) -> list[Identity]:
        return [self.identity]

    def inspect(self, identity: Identity) -> Geometry:
        if identity != self.identity:
            raise RuntimeError("unknown identity")
        return self.geometry

    def capture(self, identity: Identity, session_id: str) -> Frame:
        self.sequence += 1
        return Frame(self.image.copy(), self.geometry, identity, time.monotonic(),
                     session_id, self.sequence)

    def foreground(self, _identity: Identity) -> bool:
        return True

    def moving(self, _identity: Identity) -> bool:
        return False

    def unobscured(self, _identity: Identity, _rect: Rect, _geometry: Geometry) -> bool:
        return True

    def capture_status(self) -> CaptureStatus:
        return CaptureStatus(backend="wgc", supported=True, valid_frame=True,
                             background_supported=True, fresh=True)

    def release_capture(self) -> None:
        self.releases += 1


class FakeInput:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def click(self, point: tuple[int, int], _cancelled: object) -> None:
        self.calls.append(point)


class FakeHotkey:
    error = ""
    available = True

    def __init__(self, spec: str, _callback: Callable[[], None]) -> None:
        self.spec = spec

    def start(self) -> bool:
        return True

    def close(self) -> None:
        pass


@pytest.fixture
def close_main_windows(qtbot):
    windows = []

    def register(window):
        qtbot.addWidget(window)
        windows.append(window)
        return window

    yield register

    for window in windows:
        if window._wizard is not None:
            window._wizard.reject()
        close_and_wait(window)


def _window(backend: FakeBackend, store: ConfigStore) -> MainWindow:
    return MainWindow(backend=backend, input_adapter=FakeInput(), store=store,
                      hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)


def _template(backend: FakeBackend, name: str = "旧按钮") -> Template:
    return Template(backend.image[42:66, 76:108].copy(),
                    (backend.geometry.width, backend.geometry.height),
                    backend.geometry.dpi, name)


def _drive_wizard_to_save(window: MainWindow, qtbot, name: str = "wizard-profile"):
    window.open_onboarding()
    wizard = window._wizard
    assert wizard is not None
    qtbot.waitUntil(lambda: wizard.target_combo.count() == 2, timeout=3000)
    wizard.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: wizard.draft.geometry == window.backend.geometry, timeout=3000)
    wizard.capture_button.click()
    qtbot.waitUntil(lambda: wizard.draft.frame is not None, timeout=3000)
    wizard._selection_made(Rect(76, 42, 32, 24))
    wizard.next()
    wizard.next()
    wizard.next()
    wizard.name_edit.setText(name)
    wizard.finish_button.click()
    return wizard


def test_wizard_selection_undo_and_cancel_preserve_original_templates(qtbot) -> None:
    backend = FakeBackend()
    original = _template(backend)
    old_success = _template(backend, "旧成功标志")
    frame = backend.capture(backend.identity, "static")
    draft = OnboardingDraft(
        backend.identity, backend.geometry, Settings(), Region(), original, old_success,
        original_region=Region(), original_button=original, original_success=old_success,
    )
    wizard = OnboardingWizard(draft)
    qtbot.addWidget(wizard)
    wizard.show()
    wizard.set_targets([backend.identity])
    wizard.set_target_geometry(backend.identity, backend.geometry)
    wizard.set_frame(frame)

    wizard._selection_made(Rect(76, 42, 1, 1))
    assert draft.button is original
    wizard._selection_made(Rect(76, 42, 32, 24))
    assert draft.button is not original
    wizard.undo_selection()
    assert draft.button is original
    assert draft.success is old_success
    wizard.reject()
    assert draft.button is original
    assert draft.success is old_success


def test_wizard_save_failure_keeps_draft_and_no_match_is_explicitly_unconfirmed(qtbot) -> None:
    backend = FakeBackend()
    frame = backend.capture(backend.identity, "static")
    draft = OnboardingDraft(backend.identity, backend.geometry, Settings(), Region(),
                            _template(backend), None)
    wizard = OnboardingWizard(draft)
    qtbot.addWidget(wizard)
    wizard.show()
    wizard.set_targets([backend.identity])
    wizard.set_target_geometry(backend.identity, backend.geometry)
    wizard.set_frame(frame)
    wizard.next()
    wizard.next()
    wizard.recognition_completed(Detection(reason="当前画面没有唯一匹配"))
    assert not draft.confirmed
    assert "未确认配置" in draft.recognition_text
    assert "没有成功标志" in wizard.save_feedback.text()
    wizard.next()
    wizard.name_edit.setText("保留草稿")
    emitted = []
    wizard.finishRequested.connect(lambda value, name: emitted.append((value, name)))
    wizard.finish_button.click()
    assert emitted == [(draft, "保留草稿")]
    assert wizard._saving
    wizard.save_failed("磁盘不可写")
    assert not wizard._saving
    assert wizard.draft is draft
    assert draft.button is not None
    assert "草稿仍保留" in wizard.save_feedback.text()
    assert wizard.isVisible()


def test_wizard_commits_only_after_save_and_never_starts_a_round(
    qtbot, tmp_path, close_main_windows
) -> None:
    backend = FakeBackend()
    input_adapter = FakeInput()
    store = ConfigStore(tmp_path)
    window = MainWindow(backend=backend, input_adapter=input_adapter, store=store,
                        hotkey_factory=FakeHotkey, notifier=lambda: None, capture_delay=0)
    close_main_windows(window)
    window.show()
    wizard = _drive_wizard_to_save(window, qtbot)
    assert window._wizard is wizard
    qtbot.waitUntil(lambda: window._wizard is None, timeout=5000)

    profile_path = store.root / "wizard-profile.json"
    assert profile_path.is_file()
    saved_settings, saved_region, saved_button, saved_success = store.load_from(profile_path)
    assert saved_settings == Settings()
    assert saved_region == Region()
    assert saved_button is not None
    assert saved_success is None
    assert window._current_config_path == profile_path
    assert window.button is not None
    assert window.settings == Settings()
    assert window.mode == Mode.REMIND
    assert window.engine.snapshot().state == State.DISARMED
    assert "尚未确认" in window.status_label.text()
    assert input_adapter.calls == []


def test_cancelled_wizard_does_not_change_active_config_or_profile(
    qtbot, tmp_path, close_main_windows
) -> None:
    backend = FakeBackend()
    store = ConfigStore(tmp_path)
    active_path = store.root / "active.json"
    old_template = _template(backend)
    store.save_to(active_path, Settings(threshold=0.907), Region(), old_template, None)
    original_bytes = active_path.read_bytes()
    window = _window(backend, store)
    close_main_windows(window)
    window.settings = Settings(threshold=0.907)
    window.identity = backend.identity
    window.geometry = backend.geometry
    window.button = old_template
    window._set_current_config_path(active_path)
    window._saved_config_signature = window._config_signature()
    window._configure_engine()

    window.open_onboarding()
    wizard = window._wizard
    assert wizard is not None
    qtbot.waitUntil(lambda: wizard.target_combo.count() == 2, timeout=3000)
    wizard.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: wizard.draft.geometry == backend.geometry, timeout=3000)
    wizard.capture_button.click()
    qtbot.waitUntil(lambda: wizard.draft.frame is not None, timeout=3000)
    wizard._selection_made(Rect(76, 42, 32, 24))
    assert window.button is old_template
    wizard.reject()
    assert window._wizard is None
    assert window.button is old_template
    assert window._current_config_path == active_path
    assert active_path.read_bytes() == original_bytes
    assert not (store.root / "wizard-profile.json").exists()
    assert window.settings.threshold == pytest.approx(0.907)


def test_rapid_target_switch_inspects_latest_wizard_selection(
    qtbot, tmp_path, close_main_windows
) -> None:
    backend = FakeBackend()
    second_identity = Identity(102, 203, 304, "mock-game.exe", "第二个目标")
    second_geometry = Geometry(40, 50, 240, 140, 96)
    entered_first_inspect = threading.Event()
    release_first_inspect = threading.Event()
    inspected = []
    original_inspect = backend.inspect

    def inspect(identity: Identity) -> Geometry:
        inspected.append(identity)
        if identity == backend.identity:
            entered_first_inspect.set()
            assert release_first_inspect.wait(3)
            return original_inspect(identity)
        if identity == second_identity:
            return second_geometry
        raise RuntimeError("unknown identity")

    backend.inspect = inspect
    backend.list_windows = lambda: [backend.identity, second_identity]
    window = _window(backend, ConfigStore(tmp_path))
    close_main_windows(window)
    window.show()
    window.open_onboarding()
    wizard = window._wizard
    assert wizard is not None

    try:
        qtbot.waitUntil(lambda: wizard.target_combo.count() == 3, timeout=3000)
        wizard.target_combo.setCurrentIndex(1)
        qtbot.waitUntil(entered_first_inspect.is_set, timeout=3000)
        wizard.target_combo.setCurrentIndex(2)
        assert wizard.draft.identity == second_identity
        release_first_inspect.set()
        qtbot.waitUntil(lambda: wizard.draft.geometry == second_geometry, timeout=5000)
        assert wizard.target_combo.currentData() == second_identity
        assert inspected == [backend.identity, second_identity]
    finally:
        release_first_inspect.set()
