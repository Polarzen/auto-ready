from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from auto_ready.config import ConfigStore, ProfileMetadata
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
        self.success_image = self.image.copy()
        success_pattern = np.random.default_rng(17).integers(
            0, 255, (24, 32, 3), dtype=np.uint8
        )
        self.success_image[42:66, 130:162] = success_pattern
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


def _drive_wizard_to_save(
    window: MainWindow, qtbot, name: str = "wizard-profile", *, new_profile: bool = True
):
    if new_profile:
        window.new_profile_button.click()
    else:
        window.onboarding_button.click()
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
    assert wizard.game_edit.text() == "模拟目标"
    wizard.name_edit.setText(name)
    wizard.game_edit.setText("用户游戏标签")
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
    wizard.finishRequested.connect(lambda value, metadata: emitted.append((value, metadata)))
    wizard.finish_button.click()
    assert emitted == [(draft, ProfileMetadata("保留草稿", "模拟目标"))]
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
    profiles_before = {path.resolve() for path in store.root.glob("*.json")}
    window.show()
    wizard = _drive_wizard_to_save(window, qtbot)
    assert window._wizard is wizard
    qtbot.waitUntil(lambda: window._wizard is None, timeout=5000)

    profile_path = store.root / "wizard-profile.json"
    assert profile_path.is_file()
    assert {path.resolve() for path in store.root.glob("*.json")} == (
        profiles_before | {profile_path.resolve()}
    )
    saved_settings, saved_region, saved_button, saved_success, metadata = store.load_profile(
        profile_path
    )
    assert saved_settings == Settings()
    assert saved_region == Region()
    assert saved_button is not None
    assert saved_success is None
    assert metadata == ProfileMetadata("wizard-profile", "用户游戏标签")
    assert window.current_config_label.text() == (
        "当前配置：wizard-profile\n对应游戏：用户游戏标签"
    )
    assert window._current_config_path == profile_path
    assert window.button is not None
    assert window.settings == Settings()
    assert window.mode == Mode.REMIND
    assert window.engine.snapshot().state == State.DISARMED
    assert input_adapter.calls == []


def test_editing_current_profile_keeps_same_file_and_saved_crop_without_capture(
    qtbot, tmp_path, close_main_windows
) -> None:
    backend = FakeBackend()
    store = ConfigStore(tmp_path / "profiles")
    store.save(Settings(threshold=0.913), Region(), _template(backend, "保留按钮"), None,
               metadata=ProfileMetadata("旧名称", "旧游戏"))
    active_path = store.root / "existing.json"
    existing_button = _template(backend, "保留按钮")
    existing_success = Template(
        backend.success_image[42:66, 130:162].copy(),
        (backend.geometry.width, backend.geometry.height),
        backend.geometry.dpi,
        "保留成功标志",
    )
    store.save_to(
        active_path, Settings(threshold=0.913), Region(), existing_button,
        existing_success, metadata=ProfileMetadata("旧名称", "旧游戏"),
    )
    default_bytes = store.config_path.read_bytes()
    profile_paths_before = {path.resolve() for path in store.root.glob("*.json")}

    window = _window(backend, store)
    close_main_windows(window)
    window.load_config(active_path)
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)
    assert window._current_config_path == active_path
    assert np.array_equal(window.button.image, existing_button.image)
    assert np.array_equal(window.success.image, existing_success.image)

    assert window.onboarding_button.text() == "修改当前配置…"
    window.onboarding_button.click()
    wizard = window._wizard
    assert wizard is not None
    assert wizard.draft.edit_current
    assert wizard.draft.profile_name == "旧名称"
    assert wizard.draft.game_label == "旧游戏"
    assert wizard.draft.frame is None
    assert np.array_equal(wizard.draft.button.image, existing_button.image)
    assert wizard.retained_button_preview.pixmap().isNull() is False
    qtbot.waitUntil(lambda: wizard.target_combo.count() == 2, timeout=3000)
    wizard.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: wizard.draft.geometry == backend.geometry, timeout=3000)
    assert wizard.draft.frame is None
    assert backend.sequence == 0

    wizard.next()
    assert wizard.pages.currentIndex() == 1
    wizard.next()
    wizard.next()
    assert wizard.pages.currentIndex() == 3
    wizard.name_edit.setText("更新名称")
    wizard.game_edit.setText("更新游戏标签")
    wizard.finish_button.click()
    qtbot.waitUntil(lambda: window._wizard is None, timeout=5000)

    assert window._current_config_path == active_path
    assert store.config_path.read_bytes() == default_bytes
    assert {path.resolve() for path in store.root.glob("*.json")} == profile_paths_before
    settings, _region, button, success, metadata = store.load_profile(active_path)
    assert settings.threshold == pytest.approx(0.913)
    assert metadata == ProfileMetadata("更新名称", "更新游戏标签")
    assert button is not None and np.array_equal(button.image, existing_button.image)
    assert success is not None and np.array_equal(success.image, existing_success.image)
    assert np.array_equal(window.button.image, existing_button.image)
    assert np.array_equal(window.success.image, existing_success.image)
    assert backend.sequence == 0


def test_loading_profile_from_wizard_discards_stale_draft_on_success(
    qtbot, tmp_path, close_main_windows
) -> None:
    backend = FakeBackend()
    store = ConfigStore(tmp_path / "profiles")
    default_button = _template(backend, "默认按钮")
    store.save(Settings(), Region(), default_button, None)
    selected_path = store.root / "selected.json"
    selected_button = Template(
        backend.success_image[42:66, 130:162].copy(),
        (backend.geometry.width, backend.geometry.height),
        backend.geometry.dpi,
        "已载入按钮",
    )
    store.save_to(
        selected_path, Settings(threshold=0.932), Region(), selected_button, None,
        metadata=ProfileMetadata("已载入配置", "已载入游戏"),
    )
    window = _window(backend, store)
    close_main_windows(window)
    window.open_onboarding()
    wizard = window._wizard
    assert wizard is not None
    wizard.draft.profile_name = "过期草稿"
    wizard.draft.button = _template(backend, "过期模板")

    window.choose_config_to_load = lambda *, from_wizard=False: (
        window._request_profile_switch(selected_path, from_wizard=from_wizard)
    )
    wizard.load_profile_button.click()
    qtbot.waitUntil(lambda: window._wizard is None, timeout=5000)

    assert window._current_config_path == selected_path
    assert window._current_profile_metadata == ProfileMetadata("已载入配置", "已载入游戏")
    assert window.settings.threshold == pytest.approx(0.932)
    assert np.array_equal(window.button.image, selected_button.image)
    assert window.engine.snapshot().state == State.DISARMED
    assert backend.sequence == 0


def test_blocked_save_commits_the_submitted_draft_snapshot(
    qtbot, tmp_path, close_main_windows, monkeypatch
) -> None:
    backend = FakeBackend()
    store = ConfigStore(tmp_path / "profiles")
    active_path = store.root / "current.json"
    original_button = _template(backend, "提交按钮")
    original_success = Template(
        backend.success_image[42:66, 130:162].copy(),
        (backend.geometry.width, backend.geometry.height), backend.geometry.dpi,
        "提交成功标志",
    )
    store.save_to(
        active_path, Settings(threshold=0.914), Region(), original_button,
        original_success, metadata=ProfileMetadata("原名称", "原游戏"),
    )
    window = _window(backend, store)
    close_main_windows(window)
    window.load_config(active_path)
    qtbot.waitUntil(lambda: not window._config_loading, timeout=3000)
    window.open_onboarding()
    wizard = window._wizard
    assert wizard is not None
    qtbot.waitUntil(lambda: wizard.target_combo.count() == 2, timeout=3000)
    wizard.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: wizard.draft.geometry == backend.geometry, timeout=3000)
    wizard.next()
    wizard.next()
    wizard.next()
    wizard.name_edit.setText("提交名称")
    wizard.game_edit.setText("提交游戏")

    entered_save = threading.Event()
    release_save = threading.Event()
    save_to = store.save_to

    def blocked_save_to(path, *args, **kwargs):
        if Path(path) == active_path:
            entered_save.set()
            assert release_save.wait(5)
        return save_to(path, *args, **kwargs)

    monkeypatch.setattr(store, "save_to", blocked_save_to)
    try:
        wizard.finish_button.click()
        qtbot.waitUntil(entered_save.is_set, timeout=3000)
        submitted_request = window._wizard_request
        assert submitted_request is not None
        assert wizard._saving
        assert not wizard.target_combo.isEnabled()
        assert not wizard.selection_combo.isEnabled()
        assert not wizard.undo_button.isEnabled()
        assert not wizard.name_edit.isEnabled()
        assert not wizard.game_edit.isEnabled()
        assert not wizard.preview.is_selection_enabled

        # Simulate a late internal draft mutation while the worker owns the save.
        # Completion must commit the detached snapshot that the worker persisted.
        wizard.draft.button = _template(backend, "后改按钮")
        wizard.draft.settings = Settings(threshold=0.999)
        wizard.draft.profile_name = "后改名称"
        wizard.draft.game_label = "后改游戏"
        release_save.set()
        qtbot.waitUntil(lambda: window._wizard is None, timeout=5000)
    finally:
        release_save.set()

    settings, _region, saved_button, saved_success, metadata = store.load_profile(active_path)
    assert settings.threshold == pytest.approx(0.914)
    assert metadata == ProfileMetadata("提交名称", "提交游戏")
    assert saved_button is not None
    assert np.array_equal(saved_button.image, original_button.image)
    assert saved_success is not None
    assert np.array_equal(saved_success.image, original_success.image)
    assert window.settings.threshold == pytest.approx(0.914)
    assert window._current_profile_metadata == metadata
    assert window.button is not None
    assert np.array_equal(window.button.image, original_button.image)
    assert backend.sequence == 0


def test_finish_during_blocked_recognition_can_be_retried(
    qtbot, tmp_path, close_main_windows
) -> None:
    backend = FakeBackend()
    store = ConfigStore(tmp_path / "profiles")
    window = _window(backend, store)
    close_main_windows(window)
    window.open_onboarding(new_profile=True)
    wizard = window._wizard
    assert wizard is not None
    qtbot.waitUntil(lambda: wizard.target_combo.count() == 2, timeout=3000)
    wizard.target_combo.setCurrentIndex(1)
    qtbot.waitUntil(lambda: wizard.draft.geometry == backend.geometry, timeout=3000)
    wizard.draft.button = _template(backend, "识别按钮")
    wizard._refresh_template_feedback()
    wizard.next()
    wizard.next()

    entered_recognition = threading.Event()
    release_recognition = threading.Event()
    capture = backend.capture

    def blocked_capture(identity: Identity, session_id: str) -> Frame:
        entered_recognition.set()
        assert release_recognition.wait(5)
        return capture(identity, session_id)

    backend.capture = blocked_capture
    wizard.check_button.click()
    qtbot.waitUntil(entered_recognition.is_set, timeout=3000)
    wizard.next()
    assert wizard.pages.currentIndex() == 3
    wizard.name_edit.setText("识别后保存")
    wizard.game_edit.setText("示例游戏")
    wizard.finish_button.click()

    assert window._wizard is wizard
    assert window._wizard_request is not None
    assert wizard._saving is False
    assert wizard.finish_button.isEnabled()
    assert "仍在处理" in wizard.save_feedback.text()

    release_recognition.set()
    qtbot.waitUntil(lambda: window._wizard_request is None, timeout=5000)
    wizard.finish_button.click()
    qtbot.waitUntil(lambda: window._wizard is None, timeout=5000)
    assert (store.root / "识别后保存.json").is_file()


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
