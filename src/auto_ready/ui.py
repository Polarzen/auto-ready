"""Chinese desktop workflow for explicit, single-round ready assistance."""

from __future__ import annotations

import queue
import re
import threading
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import (
    QEventLoop,
    QPoint,
    QRect,
    QRectF,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .config import ProfileMetadata
from .engine import TERMINAL, Engine
from .models import (
    Frame,
    Geometry,
    Identity,
    Mode,
    Rect,
    Region,
    Settings,
    Snapshot,
    State,
    Template,
    preview_to_pixel,
)
from .profile_dialogs import (
    ProfileChoice,
    ProfileMetadataDialog,
    ProfileSelectionDialog,
)
from .template_preview import TemplatePreviewDialog, TemplateThumbnail


def _fallback_profile_metadata(path: Path | None, default_path: Path | None) -> ProfileMetadata:
    if path is None or (
        default_path is not None
        and Path(path).resolve() == Path(default_path).resolve()
    ):
        name = "默认配置"
    else:
        stem = Path(path).stem
        name = "".join(
            "�" if unicodedata.category(character) == "Cc" else character
            for character in stem
        )[:80] or "默认配置"
    return ProfileMetadata(name, "未标注游戏")


def _display_profile_metadata(
    metadata: ProfileMetadata, path: Path | None, default_path: Path | None
) -> ProfileMetadata:
    metadata.validate()
    fallback = _fallback_profile_metadata(path, default_path)
    display_metadata = ProfileMetadata(
        metadata.name or fallback.name, metadata.game or fallback.game
    )
    display_metadata.validate()
    return display_metadata


def _snapshot_template(template: Template | None) -> Template | None:
    if template is None:
        return None
    return Template(
        np.ascontiguousarray(template.image).copy(),
        tuple(template.reference_size),
        template.dpi,
        template.name,
    )


def _snapshot_frame(frame: Frame | None) -> Frame | None:
    if frame is None:
        return None
    return replace(frame, image=np.ascontiguousarray(frame.image).copy())


def _snapshot_onboarding_draft(draft: Any) -> Any:
    return replace(
        draft,
        button=_snapshot_template(draft.button),
        success=_snapshot_template(draft.success),
        frame=_snapshot_frame(draft.frame),
        original_button=_snapshot_template(draft.original_button),
        original_success=_snapshot_template(draft.original_success),
    )


@dataclass(frozen=True)
class _WorkerResult:
    request_id: int
    action: str
    value: Any = None
    error: str = ""


class _IOWorker(QThread):
    """One bounded command lane plus one replaceable engine snapshot."""

    def __init__(self, engine: Engine, backend: Any, store: Any, notifier: Any,
                 hotkey_probe: Any):
        super().__init__()
        self._engine = engine
        self._backend = backend
        self._store = store
        self._notifier = notifier
        self._hotkey_probe = hotkey_probe
        self._last_hotkey_availability: bool | None = None
        self._commands: queue.Queue[tuple[int, str, Any]] = queue.Queue(maxsize=4)
        self._results: deque[_WorkerResult] = deque(maxlen=8)
        self._profile_result: _WorkerResult | None = None
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._next_id = 0
        self._semantic_revision = 0
        self._authorization_epoch = 0
        self._capture_may_be_active = False
        self._capture_release_errors: deque[str] = deque(maxlen=8)
        self._cancel = threading.Event()

    def submit(self, action: str, payload: Any = None) -> int | None:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            if action == "start":
                payload = (payload, self._authorization_epoch)
        try:
            self._commands.put_nowait((request_id, action, payload))
        except queue.Full:
            # A requested profile load is state-changing and must retain a lane
            # behind already queued work. Replace one stale profile-list read
            # rather than silently rejecting the user's switch at queue pressure.
            if action != "load_config" or not self._make_room_for_profile_load():
                return None
            try:
                self._commands.put_nowait((request_id, action, payload))
            except queue.Full:
                return None
        return request_id

    def _make_room_for_profile_load(self) -> bool:
        commands = self._commands
        with commands.mutex:
            for index, (_request_id, action, _payload) in enumerate(commands.queue):
                if action == "list_profiles":
                    del commands.queue[index]
                    commands.not_full.notify()
                    return True
        return False

    def invalidate_authorization(self) -> None:
        """Revoke queued start commands before a stop signal is processed."""
        with self._lock:
            self._authorization_epoch += 1

    def latest_snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    def set_semantic_revision(self, revision: int) -> None:
        with self._lock:
            self._semantic_revision = revision

    def notification_context(self) -> tuple[int, int]:
        with self._lock:
            return self._semantic_revision, self._authorization_epoch

    def capture_release_errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._capture_release_errors)

    def take_results(self) -> list[_WorkerResult]:
        with self._lock:
            results = list(self._results)
            self._results.clear()
            # Drain generic noise first; request/revision guards make a profile
            # completion safe to handle last if intervening results changed state.
            if self._profile_result is not None:
                results.append(self._profile_result)
                self._profile_result = None
        return results

    def shutdown(self) -> None:
        self.invalidate_authorization()
        self._cancel.set()
        while True:
            try:
                self._commands.get_nowait()
            except queue.Empty:
                break
        self.requestInterruption()

    def _record_result(self, result: _WorkerResult) -> None:
        with self._lock:
            if result.action in {
                "load_config", "save_config", "save_config_as", "wizard_save"
            }:
                self._profile_result = result
            else:
                self._results.append(result)

    def _refresh_hotkey_availability(self) -> None:
        available = bool(self._hotkey_probe())
        if available != self._last_hotkey_availability:
            self._engine.set_hotkey_available(available)
            self._last_hotkey_availability = available

    def _execute(self, action: str, payload: Any) -> Any:
        if action == "refresh_targets":
            return self._backend.list_windows()
        if action == "inspect":
            identity: Identity = payload
            return identity, self._backend.inspect(identity)
        if action == "capture":
            identity, delay = payload
            if delay and self._cancel.wait(delay):
                raise RuntimeError("采集已取消")
            if self._cancel.is_set():
                raise RuntimeError("采集已取消")
            geometry = self._backend.inspect(identity)
            self._capture_may_be_active = True
            frame = self._backend.capture(identity, str(uuid.uuid4()))
            return frame, geometry
        if action == "wizard_targets":
            own_hwnd = int(payload)
            return [item for item in self._backend.list_windows() if item.hwnd != own_hwnd]
        if action == "wizard_inspect":
            identity: Identity = payload
            return identity, self._backend.inspect(identity)
        if action == "wizard_capture":
            identity, delay = payload
            if delay and self._cancel.wait(delay):
                raise RuntimeError("采集已取消")
            if self._cancel.is_set():
                raise RuntimeError("采集已取消")
            geometry = self._backend.inspect(identity)
            self._capture_may_be_active = True
            frame = self._backend.capture(identity, str(uuid.uuid4()))
            return identity, frame, geometry
        if action == "wizard_recognize":
            identity, button, region, settings, revision, draft_revision = payload
            with self._lock:
                if revision != self._semantic_revision:
                    raise RuntimeError("设置已更改，已取消过期识别检查")
            geometry = self._backend.inspect(identity)
            self._capture_may_be_active = True
            frame = self._backend.capture(identity, str(uuid.uuid4()))
            if frame.identity != identity or frame.geometry != geometry:
                raise RuntimeError("目标窗口或客户区已变化，请重新获取画面")
            from .recognition import match

            return identity, draft_revision, match(frame, button, region, settings)
        if action == "wizard_save":
            intent, path, draft, metadata = payload
            profile_path = Path(path) if path is not None else None
            if intent == "edit":
                if profile_path is None:
                    raise ValueError("当前配置路径不可用，无法保存编辑结果。")
                default_path = getattr(self._store, "config_path", None)
                if (default_path is not None
                        and profile_path.resolve() == Path(default_path).resolve()):
                    self._store.save(
                        draft.settings, draft.region, draft.button, draft.success,
                        metadata=metadata,
                    )
                else:
                    self._store.save_to(
                        profile_path, draft.settings, draft.region, draft.button,
                        draft.success, metadata=metadata,
                    )
            elif intent == "new":
                if profile_path is None:
                    raise ValueError("新配置路径不可用。")
                if profile_path.exists() or profile_path.is_symlink():
                    raise ValueError("该名称的配置已存在；请选择另一个名称以保留旧配置。")
                self._store.save_to(
                    profile_path, draft.settings, draft.region, draft.button,
                    draft.success, metadata=metadata,
                )
            else:
                raise ValueError("未知的向导保存意图。")
            return intent, profile_path, draft, metadata
        if action == "list_profiles":
            roots = tuple(Path(root) for root in payload if root is not None)
            profiles: dict[Path, ProfileMetadata] = {}
            for root in roots:
                try:
                    for path in root.glob("*.json"):
                        if path.is_file():
                            try:
                                profiles[path.resolve()] = self._store.read_profile_metadata(path)
                            except (OSError, ValueError):
                                # Malformed unrelated profile files do not hide valid choices.
                                continue
                except OSError:
                    continue
            return sorted(
                profiles.items(),
                key=lambda item: (item[1].game.casefold(), item[1].name.casefold(), str(item[0])),
            )
        if action == "set_capture_method":
            setter = getattr(self._backend, "set_capture_method", None)
            if setter is None:
                return "当前后端不支持切换捕获方式"
            if not self._release_capture():
                raise RuntimeError("原捕获资源释放失败，未切换捕获方式")
            return setter(payload)
        if action == "preview":
            revision = payload
            with self._lock:
                if revision != self._semantic_revision:
                    raise RuntimeError("识别设置已更改，已取消过期预览")
            self._capture_may_be_active = True
            snapshot = self._engine.preview()
            with self._lock:
                if revision != self._semantic_revision:
                    return None
            return revision, snapshot
        if action == "start":
            (revision, allow_activation), authorization_epoch = payload
            self._refresh_hotkey_availability()
            with self._lock:
                if (revision != self._semantic_revision
                        or authorization_epoch != self._authorization_epoch):
                    raise RuntimeError("识别设置已更改，已取消排队中的开始操作")
            session_id = (
                self._engine.start(allow_activation=True)
                if allow_activation else self._engine.start()
            )
            with self._lock:
                stale = (revision != self._semantic_revision
                         or authorization_epoch != self._authorization_epoch)
            if stale:
                self._engine.stop("识别设置已更改，已取消排队中的开始操作")
                raise RuntimeError("识别设置已更改，已取消排队中的开始操作")
            return revision, session_id, allow_activation
        if action == "save_config":
            path, revision, settings, region, button, success, metadata = payload
            default_path = getattr(self._store, "config_path", None)
            if path is None or (default_path is not None and Path(path) == Path(default_path)):
                self._store.save(settings, region, button, success, metadata=metadata)
            else:
                self._store.save_to(
                    Path(path), settings, region, button, success, metadata=metadata
                )
            return Path(path) if path is not None else None, revision, metadata
        if action == "save_config_as":
            path, revision, settings, region, button, success, metadata = payload
            profile_path = Path(path)
            self._store.save_to(
                profile_path, settings, region, button, success, metadata=metadata
            )
            return profile_path, revision, metadata
        if action == "load_config":
            path, revision = payload
            config = (
                self._store.load_profile()
                if path is None else self._store.load_profile(Path(path))
            )
            return revision, path, config
        if action == "import_template":
            path, geometry, identity, revision, template_kind = payload
            from .config import load_template

            return identity, revision, template_kind, load_template(Path(path), geometry)
        if action == "test_sound":
            self._notifier("测试提示音")
            return "已播放系统提示音"
        raise ValueError(f"未知后台操作：{action}")

    def _release_capture(self) -> bool:
        if not self._capture_may_be_active:
            return True
        release = getattr(self._backend, "release_capture", None)
        self._capture_may_be_active = False
        if release is None:
            return True
        try:
            release()
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._capture_release_errors.append(message)
            self._record_result(_WorkerResult(0, "capture_release", error=message))
            return False
        return True

    def run(self) -> None:
        try:
            self._run_loop()
        finally:
            self._release_capture()

    def _run_loop(self) -> None:
        next_tick = 0.0
        while not self._cancel.is_set() and not self.isInterruptionRequested():
            try:
                self._refresh_hotkey_availability()
            except Exception as exc:
                self._record_result(_WorkerResult(0, "hotkey", error=str(exc)))
            try:
                request_id, action, payload = self._commands.get_nowait()
            except queue.Empty:
                pass
            else:
                try:
                    value = self._execute(action, payload)
                    if action == "preview" and isinstance(value, Snapshot):
                        with self._lock:
                            self._snapshot = value
                    self._record_result(_WorkerResult(request_id, action, value))
                except Exception as exc:  # Surface backend and disk errors in the UI.
                    self._record_result(_WorkerResult(request_id, action, error=str(exc)))

            if self._cancel.is_set() or self.isInterruptionRequested():
                break
            now = time.monotonic()
            if now >= next_tick:
                try:
                    before = self._engine.snapshot()
                    if (before.session_id and before.state not in TERMINAL
                            and not (before.state == State.PAUSED and before.manual_paused)):
                        self._capture_may_be_active = True
                    snapshot = self._engine.tick()
                except Exception as exc:
                    self._record_result(_WorkerResult(0, "tick", error=str(exc)))
                else:
                    status_probe = getattr(self._backend, "capture_status", None)
                    if status_probe is not None:
                        try:
                            status = status_probe()
                            if status is not None:
                                snapshot = replace(snapshot, capture_status=status)
                        except Exception:
                            pass
                    with self._lock:
                        self._snapshot = snapshot
                    if (snapshot.state in TERMINAL
                            or (snapshot.state == State.PAUSED and snapshot.manual_paused)):
                        self._release_capture()
                next_tick = time.monotonic() + max(
                    0.05, float(self._engine.settings.poll_seconds)
                )
            self._cancel.wait(min(0.05, max(0.0, next_tick - time.monotonic())))


class ImagePreview(QWidget):
    """Letterboxed physical-pixel preview with drag selection."""

    selectionMade = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(480, 260)
        self.setMouseTracking(True)
        self._image: np.ndarray | None = None
        self._image_size = (0, 0)
        self._candidates: tuple[Any, ...] = ()
        self._drag_start: QPoint | None = None
        self._drag_end: QPoint | None = None
        self._selectable = False

    def set_frame(self, frame: Frame | None, candidates: tuple[Any, ...] = ()) -> None:
        self._image = None if frame is None else frame.image
        self._image_size = (0, 0) if frame is None else (
            int(frame.image.shape[1]), int(frame.image.shape[0])
        )
        self._candidates = candidates
        self._drag_start = None
        self._drag_end = None
        self.update()

    def set_selection_enabled(self, enabled: bool) -> None:
        self._selectable = enabled
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)

    @property
    def is_selection_enabled(self) -> bool:
        return self._selectable

    def _display_rect(self) -> QRect:
        width, height = self._image_size
        if width <= 0 or height <= 0:
            return QRect()
        scale = min(self.width() / width, self.height() / height)
        drawn_width = max(1, round(width * scale))
        drawn_height = max(1, round(height * scale))
        return QRect((self.width() - drawn_width) // 2, (self.height() - drawn_height) // 2,
                     drawn_width, drawn_height)

    def _to_image_pixel(self, point: QPoint) -> tuple[int, int] | None:
        display = self._display_rect()
        return preview_to_pixel(
            point.x(), point.y(),
            Rect(display.x(), display.y(), display.width(), display.height()),
            self._image_size,
        )

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#171b22"))
        if self._image is None:
            painter.setPen(QColor("#aeb8c6"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "先选择目标窗口，再采集静态截图")
            return
        rgb = np.ascontiguousarray(self._image[:, :, ::-1])
        height, width = rgb.shape[:2]
        qimage = QImage(rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888).copy()
        display = self._display_rect()
        painter.drawPixmap(display, QPixmap.fromImage(qimage))
        if self._candidates:
            sx, sy = display.width() / width, display.height() / height
            painter.setPen(QPen(QColor("#ffb84d"), 2))
            for candidate in self._candidates:
                rect = candidate.rect
                mapped = QRectF(display.x() + rect.x * sx, display.y() + rect.y * sy,
                                rect.width * sx, rect.height * sy)
                painter.drawRect(mapped)
                painter.drawText(mapped.topLeft() + QPoint(2, -3), f"{candidate.score:.4f}")
        if self._drag_start is not None and self._drag_end is not None:
            painter.setPen(QPen(QColor("#44d7b6"), 2, Qt.PenStyle.DashLine))
            painter.drawRect(QRect(self._drag_start, self._drag_end).normalized())

    def mousePressEvent(self, event: Any) -> None:
        if self._selectable and event.button() == Qt.MouseButton.LeftButton:
            if self._to_image_pixel(event.position().toPoint()) is not None:
                self._drag_start = event.position().toPoint()
                self._drag_end = self._drag_start
                self.update()

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag_start is not None:
            self._drag_end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: Any) -> None:
        if (self._drag_start is None or event.button() != Qt.MouseButton.LeftButton
                or self._image is None):
            return
        end_point = event.position().toPoint()
        start_pixel = self._to_image_pixel(self._drag_start)
        end_pixel = self._to_image_pixel(end_point)
        if start_pixel is not None and end_pixel is not None:
            x1, y1 = start_pixel
            x2, y2 = end_pixel
            rect = Rect(min(x1, x2), min(y1, y2), abs(x2 - x1) + 1, abs(y2 - y1) + 1)
            self.selectionMade.emit(rect)
        self._drag_start = None
        self._drag_end = None
        self.update()


class MainWindow(QMainWindow):
    """The explicit target → collect → preview → mode → manual start workflow."""

    emergencyStop = Signal()
    notificationRequested = Signal(str, str, str, int, int)

    def __init__(self, backend: Any = None, input_adapter: Any = None, store: Any = None,
                 hotkey_factory: Any = None, *, notifier: Any = None, capture_delay: float = 3.0):
        super().__init__()
        from .config import ConfigStore
        from .native import GlobalHotkey, WindowsBackend, WindowsInput, hotkey_policy_reason
        from .sound import notify

        self.setWindowTitle("Auto Ready · 等待游戏就绪")
        self.resize(760, 560)
        self.backend = backend if backend is not None else WindowsBackend()
        self.store = store if store is not None else ConfigStore()
        self._sound_notifier = notifier if notifier is not None else notify
        self._notifier = self._emit_notification
        self._hotkey_policy_reason = hotkey_policy_reason
        input_adapter = input_adapter if input_adapter is not None else WindowsInput()
        self._hotkey_factory = hotkey_factory or (
            lambda spec, callback: GlobalHotkey(spec, callback)
        )
        self._capture_delay = max(0.0, capture_delay)
        self.settings = Settings()
        self.region = Region()
        self.mode = Mode.REMIND
        self.identity: Identity | None = None
        self.geometry: Geometry | None = None
        self.button: Template | None = None
        self.success: Template | None = None
        self._static_frame: Frame | None = None
        self._last_displayed_key: tuple[str, int] | None = None
        self._semantic_revision = 0
        self._pending_start_request: int | None = None
        self._pending_capture_request: int | None = None
        self._capture_clears_selections = False
        self._pending_import_request: int | None = None
        self._pending_profile_request: int | None = None
        self._pending_profile_action: str | None = None
        self._config_loading = False
        self._profile_content_ready = False
        self._profile_content_error = ""
        self._startup_default_load_started = False
        self._startup_load_retry_scheduled = False
        self._loading_from_wizard = False
        self._pending_profile_switch_from_wizard = False
        config_path = getattr(self.store, "config_path", None)
        self._current_config_path = Path(config_path) if config_path is not None else None
        self._current_profile_metadata = _fallback_profile_metadata(
            self._current_config_path, Path(config_path) if config_path is not None else None
        )
        self._current_profile_metadata_initialized = False
        self._refreshing_targets = False
        self._hotkey_available = False
        self._hotkey_error = ""
        self._hotkey: Any = None
        self._closed = False
        self._shutdown_started = False
        self._shutdown_poll_timer: QTimer | None = None
        self._manual_pause_requested = False
        self._allow_activation_round = False
        self._known_profiles: dict[Path, ProfileMetadata] = {}
        self._pending_profile_switch: Path | None = None
        self._pending_profile_switch_active = False
        self._saved_config_signature: tuple[Any, ...] | None = None
        self._capture_backend_name = "unselected"
        self._wizard: Any = None
        self._wizard_request: int | None = None
        self._wizard_action: str | None = None
        self._wizard_pending_identity: Identity | None = None
        self._wizard_revision = 0
        self._wizard_save_path: Path | None = None
        self._tray_icon: QSystemTrayIcon | None = None
        self._tray_status_action: QAction | None = None
        self._last_dashboard_status: tuple[State, str] | None = None
        self._start_failure_key: tuple[State, str] | None = None
        self._start_failure_message = ""

        self.engine = Engine(self.backend, input_adapter, self._notifier)
        self.worker = _IOWorker(
            self.engine, self.backend, self.store, self._notifier,
            lambda: bool(self._hotkey is not None and self._hotkey.available),
        )
        self.worker.finished.connect(
            self._worker_finished_for_close, Qt.ConnectionType.QueuedConnection
        )
        try:
            self.emergencyStop.connect(self._show_emergency_stop)
            self.notificationRequested.connect(self._show_notification)
            self._build_ui()
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._refresh_worker_state)
            self._install_hotkey()
            self.worker.start()
            self._timer.start(80)
            QTimer.singleShot(0, self._load_startup_default_profile)
            QTimer.singleShot(0, self.refresh_targets)
        except Exception:
            self.engine.close()
            self.worker.shutdown()
            if self.worker.isRunning():
                _process_gui_until_thread_stops(self.worker)
            if self._hotkey is not None:
                self._hotkey.close()
                self._hotkey = None
            raise

    def _build_ui(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        target_box = QGroupBox("1 · 每次启动都要重新选择目标")
        target_row = QHBoxLayout(target_box)
        self.target_combo = QComboBox()
        self.target_combo.addItem("请选择目标窗口", None)
        self.target_combo.currentIndexChanged.connect(self._target_changed)
        self.refresh_button = QPushButton("刷新窗口")
        self.refresh_button.clicked.connect(self.refresh_targets)
        self.target_info = QLabel("未选择目标；配置文件不会保存窗口绑定。")
        self.target_info.setWordWrap(True)
        target_row.addWidget(self.target_combo, 2)
        target_row.addWidget(self.refresh_button)
        target_row.addWidget(self.target_info, 4)
        outer.addWidget(target_box)

        workflow = QHBoxLayout()
        left = QVBoxLayout()
        collect_box = QGroupBox("2 · 静态截图与框选（不会发送鼠标输入）")
        collect_layout = QVBoxLayout(collect_box)
        self.preview = ImagePreview()
        self.preview.selectionMade.connect(self._selection_made)
        self.selection_combo = QComboBox()
        self.selection_combo.addItem("选择要框选的内容…", None)
        self.selection_combo.addItem("按钮模板", "button")
        self.selection_combo.addItem("搜索区域", "region")
        self.selection_combo.addItem("成功标志模板（可选）", "success")
        self.selection_combo.currentIndexChanged.connect(self._selection_mode_changed)
        capture_row = QHBoxLayout()
        self.capture_button = QPushButton("等待 3 秒采集静态截图（保留已有模板）")
        self.capture_button.clicked.connect(self.capture_static)
        self.recollect_button = QPushButton("重新采集并清除旧框选")
        self.recollect_button.clicked.connect(self.recollect_static)
        capture_row.addWidget(self.capture_button)
        capture_row.addWidget(self.recollect_button)
        collect_layout.addWidget(self.preview, 1)
        collect_layout.addWidget(self.selection_combo)
        collect_layout.addLayout(capture_row)
        left.addWidget(collect_box, 1)

        diagnostics = QGroupBox("识别诊断")
        diagnostics_layout = QVBoxLayout(diagnostics)
        self.state_label = QLabel("未启用 · 默认仅提醒")
        self.state_label.setStyleSheet("font-weight: 700; font-size: 14px")
        self.state_label.setWordWrap(True)
        self.status_label = QLabel("请选择目标窗口；启动时不会恢复上次窗口绑定。")
        self.status_label.setWordWrap(True)
        self.capture_status_label = QLabel("捕获能力和当前画面状态见此处。")
        self.capture_status_label.setWordWrap(True)
        self.candidates_text = QPlainTextEdit()
        self.candidates_text.setReadOnly(True)
        self.candidates_text.setMaximumBlockCount(100)
        diagnostics_layout.addWidget(self.state_label)
        diagnostics_layout.addWidget(self.status_label)
        diagnostics_layout.addWidget(self.capture_status_label)
        diagnostics_layout.addWidget(self.candidates_text)
        left.addWidget(diagnostics, 1)
        workflow.addLayout(left, 3)

        right = QVBoxLayout()
        mode_box = QGroupBox("3 · 模式与手动控制")
        mode_layout = QFormLayout(mode_box)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("仅提醒（不点击）", Mode.REMIND)
        self.mode_combo.addItem("自动确认（手动开始后执行）", Mode.AUTO)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.preview_button = QPushButton("无点击预览")
        self.preview_button.clicked.connect(self.preview_without_input)
        self.start_button = QPushButton("手动开始本轮")
        self.start_button.clicked.connect(self.start_round)
        self.pause_button = QPushButton("暂停")
        self.pause_button.clicked.connect(self.pause_round)
        self.resume_button = QPushButton("继续")
        self.resume_button.clicked.connect(self.resume_round)
        self.stop_button = QPushButton("停止本轮")
        self.stop_button.clicked.connect(self.stop_round)
        mode_layout.addRow("运行模式", self.mode_combo)
        mode_layout.addRow(self.preview_button)
        mode_layout.addRow(self.start_button)
        control_row = QHBoxLayout()
        control_row.addWidget(self.pause_button)
        control_row.addWidget(self.resume_button)
        control_row.addWidget(self.stop_button)
        mode_layout.addRow(control_row)
        right.addWidget(mode_box)

        settings_box = QGroupBox("识别参数（修改后当前轮立即取消）")
        settings_form = QFormLayout(settings_box)
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0.8, 0.9999)
        self.threshold_spin.setDecimals(4)
        self.threshold_spin.setSingleStep(0.005)
        self.threshold_spin.setValue(self.settings.threshold)
        self.frames_spin = QSpinBox()
        self.frames_spin.setRange(2, 10)
        self.frames_spin.setValue(self.settings.consecutive_frames)
        self.stable_spin = QDoubleSpinBox()
        self.stable_spin.setRange(0.2, 5.0)
        self.stable_spin.setSuffix(" 秒")
        self.stable_spin.setValue(self.settings.stable_seconds)
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(5, 3600)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setValue(self.settings.round_timeout)
        self.hotkey_combo = QComboBox()
        self.hotkey_combo.setEditable(True)
        self.hotkey_combo.addItems(["Ctrl+Alt+F11", "Ctrl+Shift+F11", "Ctrl+Alt+F10"])
        self.hotkey_combo.setCurrentText(self.settings.hotkey)
        self.capture_method_combo = QComboBox()
        self.capture_method_combo.addItem("自动选择", "auto")
        self.capture_method_combo.addItem("Windows Graphics Capture", "wgc")
        self.capture_method_combo.addItem("前台兼容捕获（GDI）", "gdi")
        self.sound_enabled_checkbox = QCheckBox("发现按钮时播放提示音")
        self.sound_enabled_checkbox.setChecked(self.settings.sound_enabled)
        self.threshold_spin.valueChanged.connect(self._settings_changed)
        self.frames_spin.valueChanged.connect(self._settings_changed)
        self.stable_spin.valueChanged.connect(self._settings_changed)
        self.timeout_spin.valueChanged.connect(self._settings_changed)
        self.hotkey_combo.currentTextChanged.connect(self._settings_changed)
        self.capture_method_combo.currentIndexChanged.connect(self._settings_changed)
        self.sound_enabled_checkbox.toggled.connect(self._settings_changed)
        self.hotkey_status_label = QLabel("正在注册全局停止键…")
        self.hotkey_status_label.setWordWrap(True)
        settings_form.addRow("最低匹配分数", self.threshold_spin)
        settings_form.addRow("连续帧数", self.frames_spin)
        settings_form.addRow("稳定时间", self.stable_spin)
        settings_form.addRow("本轮超时", self.timeout_spin)
        settings_form.addRow("全局停止键", self.hotkey_combo)
        settings_form.addRow("停止键状态", self.hotkey_status_label)
        settings_form.addRow("捕获方式", self.capture_method_combo)
        settings_form.addRow(self.sound_enabled_checkbox)
        right.addWidget(settings_box)

        config_box = QGroupBox("配置与提示音")
        config_layout = QVBoxLayout(config_box)
        self.current_config_label = QLabel()
        self.current_config_label.setWordWrap(True)
        self.current_config_label.setTextFormat(Qt.TextFormat.PlainText)
        self._update_current_config_label()
        self.save_button = QPushButton("明确保存配置和模板")
        self.save_button.clicked.connect(self.save_config)
        self.load_button = QPushButton("选择并载入配置…")
        self.load_button.clicked.connect(self.choose_config_to_load)
        self.save_as_button = QPushButton("另存为配置…")
        self.save_as_button.clicked.connect(self.save_config_as)
        self.import_button = QPushButton("导入模板图片…")
        self.import_button.clicked.connect(self.import_template)
        self.sound_button = QPushButton("测试系统提示音")
        self.sound_button.clicked.connect(self.test_sound)
        config_layout.addWidget(self.current_config_label)
        config_layout.addWidget(self.save_button)
        config_layout.addWidget(self.load_button)
        config_layout.addWidget(self.save_as_button)
        config_layout.addWidget(self.import_button)
        config_layout.addWidget(self.sound_button)
        right.addWidget(config_box)
        right.addStretch(1)
        workflow.addLayout(right, 2)
        outer.addLayout(workflow, 1)
        self._build_compact_dashboard(central)
        self._update_controls()

    def _build_compact_dashboard(self, advanced_content: QWidget) -> None:
        """Move the existing detailed controls behind one scrollable dialog."""
        dashboard = QWidget(self)
        root_layout = QVBoxLayout(dashboard)
        scroll = QScrollArea(dashboard)
        scroll.setWidgetResizable(True)
        content = QWidget()
        outer = QVBoxLayout(content)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("配置"))
        self.profile_combo = QComboBox()
        self.profile_combo.addItem("默认配置", None)
        self.profile_combo.currentIndexChanged.connect(self._profile_combo_changed)
        self.profile_combo.activated.connect(self._profile_combo_activated)
        profile_row.addWidget(self.profile_combo, 1)
        self.profile_status_label = QLabel()
        self.profile_status_label.setMinimumWidth(90)
        profile_row.addWidget(self.profile_status_label)
        outer.addLayout(profile_row)
        outer.addWidget(self.current_config_label)

        template_box = QGroupBox("当前配置的模板")
        template_layout = QVBoxLayout(template_box)
        template_images = QHBoxLayout()
        button_template_column = QVBoxLayout()
        self.button_template_thumbnail = TemplateThumbnail(112, 64, content)
        self.button_template_status = QLabel()
        self.button_template_status.setWordWrap(True)
        button_template_column.addWidget(self.button_template_thumbnail, 0,
                                         Qt.AlignmentFlag.AlignLeft)
        button_template_column.addWidget(self.button_template_status)
        success_template_column = QVBoxLayout()
        self.success_template_thumbnail = TemplateThumbnail(112, 64, content)
        self.success_template_status = QLabel()
        self.success_template_status.setWordWrap(True)
        success_template_column.addWidget(self.success_template_thumbnail, 0,
                                          Qt.AlignmentFlag.AlignLeft)
        success_template_column.addWidget(self.success_template_status)
        template_images.addLayout(button_template_column, 1)
        template_images.addLayout(success_template_column, 1)
        template_layout.addLayout(template_images)
        template_actions = QHBoxLayout()
        self.template_preview_button = QPushButton("查看模板…")
        self.template_preview_button.clicked.connect(self.show_template_preview)
        template_actions.addWidget(self.template_preview_button)
        template_actions.addStretch(1)
        template_layout.addLayout(template_actions)
        outer.addWidget(template_box)

        profile_actions = QHBoxLayout()
        self.rename_profile_button = QPushButton("重命名…")
        self.rename_profile_button.clicked.connect(self.rename_profile)
        self.load_profile_dashboard_button = QPushButton("载入配置…")
        self.load_profile_dashboard_button.clicked.connect(self.choose_config_to_load)
        profile_actions.addWidget(self.rename_profile_button, 1)
        profile_actions.addWidget(self.load_profile_dashboard_button, 1)
        outer.addLayout(profile_actions)
        save_as_row = QHBoxLayout()
        self.save_as_dashboard_button = QPushButton("另存为…")
        self.save_as_dashboard_button.clicked.connect(self.save_config_as)
        self.new_profile_button = QPushButton("新建配置…")
        self.new_profile_button.clicked.connect(
            lambda _checked=False: self.open_onboarding(new_profile=True)
        )
        save_as_row.addWidget(self.new_profile_button)
        save_as_row.addWidget(self.save_as_dashboard_button)
        save_as_row.addStretch(1)
        outer.addLayout(save_as_row)

        target_box = QGroupBox("目标窗口")
        target_row = QHBoxLayout(target_box)
        target_row.addWidget(self.target_combo, 1)
        target_row.addWidget(self.refresh_button)
        outer.addWidget(target_box)
        self.target_summary_label = QLabel("每次启动都需要重新确认窗口。")
        self.target_summary_label.setWordWrap(True)
        outer.addWidget(self.target_summary_label)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("运行模式"))
        self.mode_combo.setItemText(0, "仅提醒")
        self.mode_combo.setItemText(1, "自动确认")
        mode_row.addWidget(self.mode_combo)
        self.allow_activation_checkbox = QCheckBox("发现按钮后允许切回游戏")
        self.allow_activation_checkbox.setToolTip(
            "可能打断当前操作；系统拒绝切换时只提醒。仅授权当前一轮。"
        )
        self.allow_activation_checkbox.setChecked(False)
        self.allow_activation_checkbox.toggled.connect(self._allow_activation_changed)
        mode_row.addWidget(self.allow_activation_checkbox, 1)
        outer.addLayout(mode_row)

        outer.addWidget(self.state_label)
        outer.addWidget(self.status_label)

        self.start_button.setText("开始等待")
        self.start_button.clicked.disconnect()
        self.start_button.clicked.connect(self._primary_action)
        self.pause_button.setText("暂停")
        self.pause_button.clicked.disconnect()
        self.pause_button.clicked.connect(self._toggle_pause_round)
        self.resume_button.hide()
        controls = QHBoxLayout()
        controls.addWidget(self.start_button, 2)
        controls.addWidget(self.pause_button)
        controls.addWidget(self.resume_button)
        controls.addWidget(self.stop_button)

        utility = QHBoxLayout()
        self.onboarding_button = QPushButton("开始设置")
        self.onboarding_button.clicked.connect(
            lambda _checked=False: self.open_onboarding()
        )
        self.preview_button.setText("无点击检查")
        self.advanced_button = QPushButton("高级设置与诊断…")
        self.advanced_button.clicked.connect(self.show_advanced)
        self.hide_to_tray_button = QPushButton("隐藏到托盘")
        self.hide_to_tray_button.clicked.connect(self.hide_to_tray)
        utility.addWidget(self.onboarding_button)
        utility.addWidget(self.preview_button)
        utility.addWidget(self.advanced_button)
        utility.addWidget(self.hide_to_tray_button)
        outer.addLayout(utility)

        self.hotkey_summary_label = QLabel("全局停止键：正在注册…")
        outer.addWidget(self.hotkey_summary_label)
        self.capture_release_error_label = QLabel()
        self.capture_release_error_label.setWordWrap(True)
        self.capture_release_error_label.hide()
        outer.addWidget(self.capture_release_error_label)
        outer.addStretch(1)
        scroll.setWidget(content)
        root_layout.addWidget(scroll, 1)
        root_layout.addLayout(controls)
        self.setCentralWidget(dashboard)

        self.template_preview_dialog = TemplatePreviewDialog(self)

        self.advanced_dialog = QDialog(self)
        self.advanced_dialog.setWindowTitle("高级设置与诊断")
        self.advanced_dialog.resize(900, 720)
        dialog_layout = QVBoxLayout(self.advanced_dialog)
        scroll = QScrollArea(self.advanced_dialog)
        scroll.setWidgetResizable(True)
        scroll.setWidget(advanced_content)
        dialog_layout.addWidget(scroll)
        self._create_tray()
        self._saved_config_signature = self._config_signature()
        self._refresh_template_preview()

    def _primary_action(self) -> None:
        if self.engine.snapshot().state == State.PAUSED and self._manual_pause_requested:
            self.resume_round()
            return
        self.start_round()

    def show_advanced(self) -> None:
        self.advanced_dialog.show()
        self.advanced_dialog.raise_()
        self.advanced_dialog.activateWindow()

    def _create_tray(self) -> None:
        icon = QSystemTrayIcon(self)
        icon.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        menu = QMenu(self)
        restore = QAction("显示主界面", menu)
        restore.triggered.connect(self.restore_from_tray)
        hide = QAction("隐藏主界面", menu)
        hide.triggered.connect(self.hide_to_tray)
        self._tray_status_action = QAction("状态：尚未开始", menu)
        self._tray_status_action.setEnabled(False)
        stop = QAction("停止本轮", menu)
        stop.triggered.connect(self.stop_round)
        exit_action = QAction("退出", menu)
        exit_action.triggered.connect(self._tray_exit)
        menu.addAction(restore)
        menu.addAction(hide)
        menu.addAction(self._tray_status_action)
        menu.addSeparator()
        menu.addAction(stop)
        menu.addAction(exit_action)
        icon.setContextMenu(menu)
        icon.activated.connect(self._tray_activated)
        icon.messageClicked.connect(self.restore_from_tray)
        self._tray_icon = icon
        if QSystemTrayIcon.isSystemTrayAvailable():
            icon.setVisible(True)

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.restore_from_tray()

    def hide_to_tray(self) -> None:
        if self._tray_icon is None or not QSystemTrayIcon.isSystemTrayAvailable():
            self.status_label.setText("系统托盘不可用，主界面保持打开。")
            return
        self.hide()
        self._tray_icon.showMessage("Auto Ready", "已隐藏到托盘；本轮状态保持不变。")

    def restore_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_exit(self) -> None:
        self.stop_round()
        self.close()

    def _emit_notification(self, message: str = "发现按钮") -> None:
        snapshot = self.engine.snapshot()
        revision, authorization_epoch = self.worker.notification_context()
        self.notificationRequested.emit(
            message, snapshot.session_id, snapshot.state.value, revision, authorization_epoch
        )

    def _show_notification(
        self,
        message: str,
        session_id: str,
        state_value: str,
        revision: int,
        authorization_epoch: int,
    ) -> None:
        current_revision, current_epoch = self.worker.notification_context()
        snapshot = self.engine.snapshot()
        if (self._closed or self._shutdown_started
                or revision != current_revision or authorization_epoch != current_epoch
                or snapshot.session_id != session_id or snapshot.state.value != state_value):
            return
        if self.settings.sound_enabled or message == "测试提示音":
            try:
                self._sound_notifier()
            except Exception:
                pass
        if self._tray_icon is not None and self._tray_icon.isVisible():
            self._tray_icon.showMessage(
                "Auto Ready", message, QSystemTrayIcon.MessageIcon.Information, 5000
            )

    def _config_signature(self) -> tuple[Any, ...]:
        import hashlib

        def template_signature(template: Template | None) -> Any:
            if template is None:
                return None
            return (
                template.reference_size,
                template.dpi,
                template.name,
                hashlib.sha256(template.image.tobytes()).digest(),
            )

        return (
            self.settings,
            self.region,
            template_signature(self.button),
            template_signature(self.success),
        )

    def _update_dirty_state(self) -> None:
        dirty = self._profile_content_ready and self._saved_config_signature is not None and (
            self._config_signature() != self._saved_config_signature
        )
        if hasattr(self, "profile_status_label"):
            if not self._profile_content_ready:
                state = "载入失败" if self._profile_content_error else "尚未载入"
                self.profile_status_label.setText(state)
            else:
                self.profile_status_label.setText("未保存修改" if dirty else "已保存")
        self._update_current_config_label(dirty=dirty)
        self._refresh_template_preview()
        return None

    def _profile_roots(self) -> tuple[Path, ...]:
        roots = [getattr(self.store, "root", None)]
        if self._current_config_path is not None:
            roots.append(self._current_config_path.parent)
        return tuple(Path(root) for root in roots if root is not None)

    def _profile_combo_index(self, path: Path | None) -> int:
        if path is None:
            return self.profile_combo.findData(None)
        expected = Path(path).resolve()
        for index in range(self.profile_combo.count()):
            candidate = self.profile_combo.itemData(index)
            if candidate is not None and Path(candidate).resolve() == expected:
                return index
        return -1

    def _refresh_profiles(self) -> None:
        self._queue("list_profiles", self._profile_roots())

    def _populate_profile_combo(self, profiles: list[tuple[Path, ProfileMetadata]]) -> None:
        default_path_value = getattr(self.store, "config_path", None)
        default_path = (
            Path(default_path_value).resolve() if default_path_value is not None else None
        )
        self._known_profiles = {
            Path(path).resolve(): _display_profile_metadata(
                metadata, Path(path).resolve(), default_path
            )
            for path, metadata in profiles
        }
        current_path = (
            Path(self._current_config_path).resolve()
            if self._current_config_path is not None else default_path
        )

        default_metadata = (
            self._known_profiles.get(default_path)
            if default_path is not None else None
        )
        if default_metadata is None:
            default_metadata = _fallback_profile_metadata(default_path, default_path)

        rows: list[tuple[Path | None, ProfileMetadata]] = [(None, default_metadata)]
        rows.extend(
            (path, metadata)
            for path, metadata in sorted(
                self._known_profiles.items(),
                key=lambda item: (
                    item[1].game.casefold(), item[1].name.casefold(), str(item[0])
                ),
            )
            if path != default_path
        )
        selection_path = current_path
        if self._pending_profile_switch_active:
            selection_path = (
                Path(self._pending_profile_switch).resolve()
                if self._pending_profile_switch is not None else default_path
            )
        if (selection_path is not None and selection_path != default_path
                and all(path != selection_path for path, _metadata in rows)):
            rows.append(
                (
                    selection_path,
                    self._known_profiles.get(
                        selection_path,
                        _fallback_profile_metadata(selection_path, default_path),
                    ),
                )
            )

        base_labels = [self._profile_display_name(metadata) for _path, metadata in rows]
        counts: dict[str, int] = {}
        for label in base_labels:
            counts[label] = counts.get(label, 0) + 1
        labels: list[str] = []
        for (path, _metadata), label in zip(rows, base_labels, strict=True):
            source_path = path if path is not None else default_path
            if counts[label] > 1 and source_path is not None:
                label = f"{label} · {source_path.name}"
            labels.append(label)
        second_counts: dict[str, int] = {}
        for label in labels:
            second_counts[label] = second_counts.get(label, 0) + 1
        labels = [
            f"{label} · {(path if path is not None else default_path).parent}"
            if second_counts[label] > 1 and (path is not None or default_path is not None)
            else label
            for (path, _metadata), label in zip(rows, labels, strict=True)
        ]

        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for (path, _metadata), label in zip(rows, labels, strict=True):
            self.profile_combo.addItem(label, path)
            item_path = path if path is not None else default_path
            if item_path is not None:
                self.profile_combo.setItemData(
                    self.profile_combo.count() - 1, str(item_path), Qt.ItemDataRole.ToolTipRole
                )
        if selection_path is None or selection_path == default_path:
            index = self.profile_combo.findData(None)
        else:
            index = self._profile_combo_index(selection_path)
        self.profile_combo.setCurrentIndex(max(0, index))
        self.profile_combo.blockSignals(False)

    def _profile_choices(self) -> list[ProfileChoice]:
        choices = []
        for path, metadata in sorted(
            self._known_profiles.items(),
            key=lambda item: (item[1].game.casefold(), item[1].name.casefold(), str(item[0])),
        ):
            label = (
                f"{self._profile_display_name(metadata)}\n"
                f"文件：{path.name}\n路径：{path}"
            )
            choices.append(ProfileChoice(path, metadata, label))
        return choices

    def _profile_combo_changed(self, index: int) -> None:
        if index < 0 or not hasattr(self, "profile_combo"):
            return
        selected = self.profile_combo.itemData(index)
        path = Path(selected) if selected is not None else None
        default_path = getattr(self.store, "config_path", None)
        current_path = self._current_config_path
        current_is_default = current_path is None or (
            default_path is not None
            and Path(current_path).resolve() == Path(default_path).resolve()
        )
        is_current_default = path is None and current_is_default
        is_current_path = (
            path is not None and current_path is not None
            and Path(path).resolve() == Path(current_path).resolve()
        )
        if (is_current_default or is_current_path) and self._profile_content_ready:
            return
        self._request_profile_switch(path)

    def _profile_combo_activated(self, index: int) -> None:
        if index < 0 or self._profile_content_ready or self._pending_profile_request is not None:
            return
        selected = self.profile_combo.itemData(index)
        path = Path(selected) if selected is not None else None
        self._request_profile_switch(path)

    def _ask_about_dirty_edits(self) -> str:
        self._update_dirty_state()
        dirty = self._profile_content_ready and self._saved_config_signature is not None and (
            self._config_signature() != self._saved_config_signature
        )
        if not dirty:
            return "discard"
        answer = QMessageBox.question(
            self,
            "未保存的修改",
            "当前配置有未保存修改。先保存、放弃修改，还是取消切换？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return "save"
        if answer == QMessageBox.StandardButton.Discard:
            return "discard"
        return "cancel"

    def _restore_profile_combo(self) -> None:
        if not hasattr(self, "profile_combo"):
            return
        path = self._current_config_path
        index = self._profile_combo_index(path)
        default_path = getattr(self.store, "config_path", None)
        if (index < 0 and path is not None and default_path is not None
                and Path(path).resolve() == Path(default_path).resolve()):
            index = self.profile_combo.findData(None)
        self.profile_combo.blockSignals(True)
        self.profile_combo.setCurrentIndex(max(0, index))
        self.profile_combo.blockSignals(False)

    def _request_profile_switch(
        self, path: Path | None, *, from_wizard: bool = False
    ) -> None:
        if self._profile_action_in_progress() or self._import_action_in_progress():
            self._restore_profile_combo()
            return
        choice = self._ask_about_dirty_edits()
        if choice == "cancel":
            self._restore_profile_combo()
            return
        if choice == "save":
            self._pending_profile_switch = path
            self._pending_profile_switch_active = True
            self._pending_profile_switch_from_wizard = from_wizard
            self.save_config()
            if self._pending_profile_request is None:
                self._pending_profile_switch = None
                self._pending_profile_switch_active = False
                self._pending_profile_switch_from_wizard = False
                self._restore_profile_combo()
            return
        self._queue_config_load(path, from_wizard=from_wizard)

    def _load_existing_from_wizard(self) -> None:
        self.choose_config_to_load(from_wizard=True)

    def open_onboarding(self, *, new_profile: bool = False) -> None:
        from .onboarding import OnboardingDraft, OnboardingWizard

        if self._wizard is not None and self._wizard.isVisible():
            self._wizard.raise_()
            self._wizard.activateWindow()
            return
        if not new_profile and not self._profile_content_ready:
            self.status_label.setText(
                "当前配置尚未载入；请先载入配置，或新建配置。"
            )
            return
        self._stop_before_profile_dialog("首次设置向导")
        if new_profile:
            draft = OnboardingDraft(
                self.identity,
                self.geometry,
                Settings(),
                Region(),
                None,
                None,
                profile_name="新建配置",
                game_label="",
                edit_current=False,
            )
        else:
            draft = OnboardingDraft(
                self.identity,
                self.geometry,
                self.settings,
                self.region,
                self.button,
                self.success,
                original_region=self.region,
                original_button=self.button,
                original_success=self.success,
                profile_name=self._current_profile_metadata.name,
                game_label=self._current_profile_metadata.game,
                edit_current=True,
            )
        self._wizard = OnboardingWizard(draft, self)
        self._wizard.setModal(True)
        self._wizard.targetsRequested.connect(self._wizard_refresh_targets)
        self._wizard.targetChanged.connect(self._wizard_target_changed)
        self._wizard.draftChanged.connect(self._wizard_draft_changed)
        self._wizard.captureRequested.connect(self._wizard_capture)
        self._wizard.recognitionRequested.connect(self._wizard_recognize)
        self._wizard.finishRequested.connect(self._wizard_finish)
        self._wizard.loadProfileRequested.connect(self._load_existing_from_wizard)
        self._wizard.rejected.connect(self._wizard_cancelled)
        self._wizard.show()
        self._wizard_refresh_targets()

    def _wizard_cancelled(self) -> None:
        self._wizard = None
        self._wizard_request = None
        self._wizard_action = None
        self._wizard_pending_identity = None
        self._update_controls()

    def _queue_wizard(self, action: str, payload: Any, label: str) -> None:
        if self._wizard_request is not None:
            if action == "wizard_save" and self._wizard is not None:
                self._wizard.save_failed("后台检查仍在处理，请完成后重试保存。")
            return
        request_id = self.worker.submit(action, payload)
        if request_id is None:
            if self._wizard is not None:
                message = "后台任务繁忙，请稍后重试。"
                if action == "wizard_save":
                    self._wizard.save_failed(message)
                elif action == "wizard_recognize":
                    self._wizard.check_button.setEnabled(True)
                    self._wizard.recognition_feedback.setText(message)
                else:
                    self._wizard.capture_failed(message)
            return
        self._wizard_request = request_id
        self._wizard_action = action
        self.status_label.setText(label)

    def _wizard_refresh_targets(self) -> None:
        self._queue_wizard("wizard_targets", int(self.winId()), "正在查找可用窗口…")

    def _wizard_target_changed(self, identity: Any) -> None:
        if identity is None:
            return
        self._wizard_revision += 1
        self._wizard_pending_identity = identity
        self._queue_pending_wizard_inspect()

    def _queue_pending_wizard_inspect(self) -> None:
        if (self._wizard is None or self._wizard_request is not None
                or self._wizard_pending_identity is None):
            return
        identity = self._wizard_pending_identity
        self._wizard_pending_identity = None
        if identity == self._wizard.draft.identity:
            self._queue_wizard("wizard_inspect", identity, "正在检查所选窗口…")

    def _wizard_draft_changed(self) -> None:
        self._wizard_revision += 1

    def _capture_delay_for(self, *, wizard: bool = False) -> float | None:
        explicit_wgc = self.settings.capture_method == "wgc"
        explicit_front = self.settings.capture_method == "gdi"
        known_front = self._capture_backend_name.casefold() in {"gdi", "foreground", "front"}
        delay = self._capture_delay if explicit_front or known_front else 0.0
        if not explicit_wgc and (explicit_front or known_front):
            message = (
                "当前使用前台兼容捕获。请切回游戏，确认画面静止后开始倒计时采图。"
                if wizard else "当前使用前台兼容捕获。请切回游戏后再开始倒计时采图。"
            )
            answer = QMessageBox.question(
                self,
                "需要前台兼容捕获",
                message,
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                return None
        return delay

    def _wizard_capture(self, identity: Identity) -> None:
        if self._wizard is None or identity != self._wizard.draft.identity:
            return
        delay = self._capture_delay_for(wizard=True)
        if delay is None:
            self._wizard.capture_failed("已取消采图；原有模板仍保留。")
            return
        backend = self._capture_backend_name
        self._wizard.set_capture_guidance(backend, delay)
        self._queue_wizard(
            "wizard_capture", (identity, delay), "正在后台获取静态画面…"
        )

    def _wizard_recognize(self, draft: Any) -> None:
        if self._wizard is None or draft is not self._wizard.draft:
            return
        self._queue_wizard(
            "wizard_recognize",
            (
                draft.identity,
                draft.button,
                draft.region,
                draft.settings,
                self._semantic_revision,
                self._wizard_revision,
            ),
            "正在获取新画面并运行生产识别…",
        )

    def _wizard_finish(self, draft: Any, metadata: ProfileMetadata) -> None:
        if self._wizard is None or draft is not self._wizard.draft:
            return
        if self._wizard_request is not None:
            self._wizard.save_failed("后台检查仍在处理，请完成后重试保存。")
            return
        try:
            metadata.validate()
            if not metadata.name.strip():
                raise ValueError("请输入配置名称。")
            if draft.edit_current and not self._profile_content_ready:
                raise ValueError("当前配置尚未载入，无法编辑保存。")
            draft.settings.validate()
            draft.region.validate()
            if draft.button is None:
                raise ValueError("请先在画面中框出按钮模板。")
            if draft.geometry is None or not draft.button.compatible(draft.geometry):
                raise ValueError(
                    "按钮模板与所选窗口尺寸或 DPI 不匹配；恢复对应窗口设置或修改模板。"
                )
            if draft.success is not None and not draft.success.compatible(draft.geometry):
                raise ValueError(
                    "成功标志与所选窗口尺寸或 DPI 不匹配；恢复对应窗口设置或修改模板。"
                )
        except (AttributeError, ValueError) as exc:
            self._wizard.save_failed(f"草稿无效：{exc}")
            return
        if draft.edit_current:
            path = self._current_config_path or getattr(self.store, "config_path", None)
            if path is None:
                self._wizard.save_failed("当前配置路径不可用，无法编辑保存。")
                return
            path = Path(path)
            intent = "edit"
        else:
            root = getattr(self.store, "root", None)
            if root is None:
                self._wizard.save_failed("配置存储目录不可用，无法新建配置。")
                return
            safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", metadata.name).strip(" .")
            if not safe_name:
                safe_name = "配置"
            path = Path(root) / f"{safe_name}.json"
            intent = "new"
        self._wizard_save_path = path
        submitted_draft = _snapshot_onboarding_draft(draft)
        self._queue_wizard(
            "wizard_save",
            (intent, path, submitted_draft, metadata),
            "正在保存当前配置…" if intent == "edit" else "正在新建配置…",
        )

    def _commit_wizard_draft(
        self, path: Path | None, draft: Any, metadata: ProfileMetadata
    ) -> None:
        if self._wizard is None:
            return
        wizard = self._wizard
        self._invalidate_semantics()
        self.engine.stop("首次设置已保存；本轮未开始")
        previous_hotkey = self.settings.hotkey
        self.settings = draft.settings
        self.region = draft.region
        self.button = draft.button
        self.success = draft.success
        self.identity = draft.identity
        self.geometry = draft.geometry
        self._static_frame = draft.frame
        self._profile_content_ready = True
        self._profile_content_error = ""
        self.mode = Mode.REMIND
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.blockSignals(False)
        self._apply_settings_widgets()
        if self.settings.hotkey != previous_hotkey:
            self._install_hotkey()
        if self.identity is not None:
            index = self.target_combo.findData(self.identity)
            if index >= 0:
                self.target_combo.blockSignals(True)
                self.target_combo.setCurrentIndex(index)
                self.target_combo.blockSignals(False)
            self._update_target_info(self.geometry)
            self.target_summary_label.setText(
                f"当前目标：{self.identity.title or Path(self.identity.executable).name}。"
                "配置文件不会保存窗口绑定。"
            )
        if self._static_frame is not None:
            self.preview.set_frame(self._static_frame)
        self._configure_engine("首次设置已保存；请手动开始等待")
        default_path = getattr(self.store, "config_path", None)
        self._set_current_config_path(
            Path(path) if path is not None else Path(default_path) if default_path else None
        )
        self._set_current_profile_metadata(metadata)
        self._saved_config_signature = self._config_signature()
        self._refresh_template_preview()
        self._wizard_save_path = None
        wizard.save_succeeded(path)
        self._wizard = None
        self._wizard_pending_identity = None
        self.status_label.setText(
            "配置已保存；识别已确认。你需要手动开始本轮。"
            if draft.confirmed
            else "配置已保存，但识别尚未确认；本轮未开始。"
        )
        self._refresh_profiles()
        self._update_dirty_state()
        self._update_controls()

    def _queue(self, action: str, payload: Any = None, label: str = "") -> int | None:
        request_id = self.worker.submit(action, payload)
        if request_id is None:
            self.status_label.setText("后台任务繁忙，请稍后重试。")
            return None
        if label:
            self.status_label.setText(label)
        return request_id

    def _load_startup_default_profile(self) -> None:
        if self._startup_default_load_started or self._closed or self._shutdown_started:
            return
        self._startup_load_retry_scheduled = False
        if self._queue_config_load(None, startup=True):
            self._startup_default_load_started = True
            return
        if not self._startup_load_retry_scheduled:
            self._startup_load_retry_scheduled = True
            QTimer.singleShot(50, self._load_startup_default_profile)

    def show_template_preview(self) -> None:
        self._refresh_template_preview()
        self.template_preview_dialog.show()
        self.template_preview_dialog.raise_()
        self.template_preview_dialog.activateWindow()

    def _refresh_template_preview(self) -> None:
        if not hasattr(self, "button_template_thumbnail"):
            return
        dirty = self._profile_content_ready and self._saved_config_signature is not None and (
            self._config_signature() != self._saved_config_signature
        )
        self.button_template_thumbnail.set_template(self.button)
        self.success_template_thumbnail.set_template(self.success)
        if not self._profile_content_ready:
            state = "载入失败" if self._profile_content_error else "尚未载入"
            self.button_template_status.setText(f"按钮模板：{state}")
            self.success_template_status.setText(f"成功标志：{state}")
        else:
            self.button_template_status.setText(
                "按钮模板：已设置" if self.button is not None else "按钮模板：未设置"
            )
            self.success_template_status.setText(
                "成功标志：已设置" if self.success is not None else "成功标志：未设置（可选）"
            )
        self.button_template_status.setToolTip(
            "当前配置有未保存修改" if dirty else "当前配置已保存"
        )
        self.success_template_status.setToolTip(
            "当前配置有未保存修改" if dirty else "当前配置已保存"
        )
        self.template_preview_dialog.set_templates(
            self.button,
            self.success,
            self.geometry,
            loaded=self._profile_content_ready,
            dirty=dirty,
            load_error=self._profile_content_error,
        )

    def _update_current_config_label(self, *, dirty: bool | None = None) -> None:
        if not hasattr(self, "current_config_label"):
            return
        path = self._current_config_path
        if dirty is None:
            dirty = self._profile_content_ready and self._saved_config_signature is not None and (
                self._config_signature() != self._saved_config_signature
            )
        metadata = self._current_profile_metadata
        label = f"当前配置：{metadata.name}\n对应游戏：{metadata.game}"
        if dirty:
            label += " · 未保存修改"
        self.current_config_label.setText(label)
        self.current_config_label.setTextFormat(Qt.TextFormat.PlainText)
        default_path = getattr(self.store, "config_path", None)
        tooltip_path = path if path is not None else default_path
        self.current_config_label.setToolTip(
            str(tooltip_path) if tooltip_path is not None else "使用应用默认配置"
        )
        if hasattr(self, "profile_combo"):
            if (path is None or (default_path is not None
                    and Path(path).resolve() == Path(default_path).resolve())):
                index = self.profile_combo.findData(None)
            else:
                index = self._profile_combo_index(path)
                if index < 0:
                    self.profile_combo.addItem(self._profile_display_name(metadata), path)
                    index = self.profile_combo.count() - 1
                    self.profile_combo.setItemData(
                        index, str(path), Qt.ItemDataRole.ToolTipRole
                    )
            self.profile_combo.blockSignals(True)
            self.profile_combo.setCurrentIndex(max(0, index))
            self.profile_combo.blockSignals(False)

    @staticmethod
    def _profile_display_name(metadata: ProfileMetadata) -> str:
        return f"{metadata.game} · {metadata.name}"

    def _set_current_profile_metadata(self, metadata: ProfileMetadata) -> None:
        metadata.validate()
        self._current_profile_metadata = metadata
        self._current_profile_metadata_initialized = True
        self._update_current_config_label()

    def _set_current_config_path(self, path: Path | None) -> None:
        self._current_config_path = Path(path) if path is not None else None
        self._update_current_config_label()
        self._update_dirty_state()

    def _profile_action_in_progress(self) -> bool:
        if self._pending_profile_request is None:
            return False
        self.status_label.setText("配置操作正在进行，请稍候。")
        return True

    def _import_action_in_progress(self) -> bool:
        if self._pending_import_request is None:
            return False
        self.status_label.setText("模板导入中，请稍候。")
        return True

    def _queue_profile_action(self, action: str, payload: Any, label: str) -> None:
        request_id = self._queue(action, payload, label)
        if request_id is None:
            if action == "load_config":
                self._config_loading = False
                self.status_label.setText(
                    "后台队列繁忙，未载入所选配置；当前配置保持不变。"
                )
                self._loading_from_wizard = False
            if self._pending_profile_switch_active:
                self._pending_profile_switch = None
                self._pending_profile_switch_active = False
                self._pending_profile_switch_from_wizard = False
            self._restore_profile_combo()
            self._update_controls()
            return
        self._pending_profile_request = request_id
        self._pending_profile_action = action
        self._update_controls()

    def _stop_before_profile_dialog(self, reason: str) -> None:
        self._invalidate_semantics()
        self.engine.stop(reason)

    def _invalidate_semantics(self) -> None:
        self._start_failure_key = None
        self._start_failure_message = ""
        self.worker.invalidate_authorization()
        self._semantic_revision += 1
        self.worker.set_semantic_revision(self._semantic_revision)
        self._pending_start_request = None
        self._manual_pause_requested = False
        if hasattr(self, "allow_activation_checkbox"):
            self.allow_activation_checkbox.blockSignals(True)
            self.allow_activation_checkbox.setChecked(False)
            self.allow_activation_checkbox.blockSignals(False)

    def refresh_targets(self) -> None:
        self._refreshing_targets = True
        if self.identity is not None:
            self._invalidate_semantics()
            self.engine.stop("正在刷新窗口列表")
        if self._queue("refresh_targets", label="正在读取可见窗口…") is None:
            self._refreshing_targets = False
        self._update_controls()

    def _target_changed(self, index: int) -> None:
        identity = self.target_combo.itemData(index)
        self_target = isinstance(identity, Identity) and identity.hwnd == int(self.winId())
        self._invalidate_semantics()
        snapshot = self.engine.snapshot()
        if snapshot.frame is not None:
            self._last_displayed_key = (snapshot.frame.session_id, snapshot.frame.sequence)
        self.engine.stop("目标窗口已更改")
        self.identity = identity if isinstance(identity, Identity) else None
        if self_target:
            self.target_combo.blockSignals(True)
            self.target_combo.setCurrentIndex(0)
            self.target_combo.blockSignals(False)
            self.identity = None
        self.geometry = None
        self._static_frame = None
        self.preview.set_frame(None)
        self.selection_combo.setCurrentIndex(0)
        self._refresh_template_preview()
        if self.identity is None:
            self.target_info.setText("未选择目标；配置文件不会保存窗口绑定。")
            self.target_summary_label.setText("每次启动都需要重新确认窗口。")
            if self_target:
                self.status_label.setText("不能选择 Auto Ready 自身窗口作为目标。")
            self._update_controls()
            return
        self.target_info.setText(
            f"{self.identity.title or '（无标题）'} · {self.identity.executable} · "
            f"PID {self.identity.pid} · HWND {self.identity.hwnd} · 正在检查客户区…"
        )
        self.target_summary_label.setText(
            f"当前目标：{self.identity.title or Path(self.identity.executable).name}。"
            "配置文件不会保存窗口绑定。"
        )
        self._queue("inspect", self.identity, "正在检查窗口客户区…")
        self._update_controls()

    def capture_static(self) -> None:
        self._capture_static(clear_selections=False)

    def recollect_static(self) -> None:
        self._capture_static(clear_selections=True)

    def _capture_static(self, *, clear_selections: bool) -> None:
        if self.identity is None:
            self.status_label.setText("请先选择目标窗口。")
            return
        if self._pending_capture_request is not None:
            self.status_label.setText("静态截图采集中，请稍候。")
            return
        self._invalidate_semantics()
        snapshot = self.engine.snapshot()
        if snapshot.frame is not None:
            self._last_displayed_key = (snapshot.frame.session_id, snapshot.frame.sequence)
        self.engine.stop("重新采集截图")
        delay = self._capture_delay_for()
        if delay is None:
            return
        self._capture_clears_selections = clear_selections
        request_id = self._queue("capture", (self.identity, delay),
                                 "正在获取静态截图；原有模板仍保留…")
        self._pending_capture_request = request_id
        if request_id is None:
            self._capture_clears_selections = False
        self._update_controls()

    def _selection_mode_changed(self, index: int) -> None:
        enabled = (self._static_frame is not None
                   and self.selection_combo.itemData(index) is not None)
        self.preview.set_selection_enabled(enabled)
        if enabled:
            self.preview.set_frame(self._static_frame)

    def _selection_made(self, rect: Rect) -> None:
        frame = self._static_frame
        if frame is None:
            return
        selection_kind = self.selection_combo.currentData()
        if selection_kind is None:
            self.status_label.setText("先选择要框选的内容类型。")
            return
        self._invalidate_semantics()
        self.engine.stop("静态框选已更改，当前轮已取消")
        try:
            crop = np.ascontiguousarray(frame.image[rect.y:rect.y + rect.height,
                                                    rect.x:rect.x + rect.width]).copy()
            if crop.size == 0:
                raise ValueError("框选区域为空")
            if selection_kind in {"button", "success"}:
                from .recognition import validate_template

                validate_template(crop)
            if selection_kind == "button":
                self.button = Template(crop, (frame.geometry.width, frame.geometry.height),
                                       frame.geometry.dpi, "按钮")
            elif selection_kind == "success":
                self.success = Template(crop, (frame.geometry.width, frame.geometry.height),
                                        frame.geometry.dpi, "成功标志")
            elif selection_kind == "region":
                self.region = Region.from_rect(rect, frame.geometry.width, frame.geometry.height)
            configured = self._configure_engine("识别设置已更改，当前轮已取消")
            if self.button is not None and not configured:
                self._update_controls()
                return
            self.preview.set_frame(frame)
            self.status_label.setText(self._template_summary())
            self._update_dirty_state()
            self._update_controls()
        except (ValueError, IndexError) as exc:
            if selection_kind == "button":
                self.button = None
            elif selection_kind == "success":
                self.success = None
            self._configure_engine(f"框选无效：{exc}")
            self.status_label.setText(f"框选无效：{exc}")
            self._refresh_template_preview()
            self._update_controls()

    def _template_summary(self) -> str:
        pieces = [f"按钮模板：{'已设置' if self.button else '未设置'}",
                  f"搜索区域：{self.region.x:.2f}, {self.region.y:.2f}, "
                  f"{self.region.width:.2f} × {self.region.height:.2f}",
                  f"成功标志：{'已设置' if self.success else '未设置'}"]
        return "；".join(pieces)

    def _templates_match_current_target(self) -> bool:
        return (
            self.identity is not None
            and self.geometry is not None
            and self.button is not None
            and self.button.compatible(self.geometry)
            and (self.success is None or self.success.compatible(self.geometry))
        )

    def _template_compatibility_message(self) -> str:
        if self.button is None:
            return "先设置按钮模板。"
        if self.identity is None or self.geometry is None:
            return "模板已载入，无需重复框选；请选择本次目标窗口。"
        return "模板保留；恢复窗口尺寸/DPI或修改模板。"

    def _configure_engine(self, reason: str = "设置已更改") -> bool:
        try:
            self.engine.configure(self.identity, self.button, self.success, self.region,
                                  self.settings, self.mode)
            if self.identity is None or self.button is None:
                self.engine.stop(reason)
                return False
            return True
        except Exception as exc:
            self.engine.stop(f"设置无效：{exc}")
            self.status_label.setText(f"设置无效：{exc}")
            return False

    def _settings_changed(self, *_args: Any) -> None:
        previous_capture_method = self.settings.capture_method
        self._invalidate_semantics()
        self.engine.stop("参数或停止键已更改，当前轮已取消")
        self.settings = replace(
            self.settings,
            threshold=self.threshold_spin.value(),
            consecutive_frames=self.frames_spin.value(),
            stable_seconds=self.stable_spin.value(),
            round_timeout=self.timeout_spin.value(),
            hotkey=self.hotkey_combo.currentText(),
            capture_method=str(self.capture_method_combo.currentData() or "auto"),
            sound_enabled=self.sound_enabled_checkbox.isChecked(),
        )
        if self.settings.capture_method != previous_capture_method:
            self._static_frame = None
            self.preview.set_frame(None)
            self._queue("set_capture_method", self.settings.capture_method,
                        "正在切换捕获方式；旧画面与候选已作废…")
        self._install_hotkey()
        self._configure_engine("参数或停止键已更改，当前轮已取消")
        self._update_dirty_state()

    def _mode_changed(self, index: int) -> None:
        mode = self.mode_combo.itemData(index)
        if mode is None:
            return
        self._invalidate_semantics()
        self.mode = Mode(mode)
        self._configure_engine("运行模式已更改，当前轮已取消")
        self.status_label.setText(f"模式：{self.mode.value}；当前轮已取消，请手动预览或开始。")
        self._update_controls()

    def _allow_activation_changed(self, checked: bool) -> None:
        if self.mode != Mode.AUTO and checked:
            self.allow_activation_checkbox.blockSignals(True)
            self.allow_activation_checkbox.setChecked(False)
            self.allow_activation_checkbox.blockSignals(False)
        self._update_controls()

    def _install_hotkey(self) -> None:
        if self._hotkey is not None:
            try:
                self._hotkey.close()
            except Exception:
                pass
            self._hotkey = None
        self._hotkey_error = ""
        policy_error = self._hotkey_policy_reason(self.settings.hotkey)
        if policy_error is not None:
            available = False
            self._hotkey_error = policy_error
        else:
            try:
                self._hotkey = self._hotkey_factory(
                    self.settings.hotkey,
                    lambda: (self.worker.invalidate_authorization(),
                             self.engine.stop("全局停止键已触发"), self.emergencyStop.emit()),
                )
                available = bool(self._hotkey.start())
            except Exception as exc:
                available = False
                self._hotkey_error = str(exc)
        self.engine.set_hotkey_available(available)
        self.engine.set_hotkey_probe(
            lambda: bool(self._hotkey is not None and self._hotkey.available)
        )
        self._hotkey_available = available
        if available:
            self._hotkey_error = ""
            self.hotkey_status_label.setText(f"已注册：{self.settings.hotkey}")
        elif policy_error is not None:
            self.hotkey_status_label.setText(f"需要迁移：{policy_error}")
            self.status_label.setText(policy_error)
        else:
            error = getattr(self._hotkey, "error", "") if self._hotkey is not None else ""
            self._hotkey_error = error or self._hotkey_error
            self.hotkey_status_label.setText(
                f"不可用：{self._hotkey_error or '注册失败'}；自动确认已禁用，请选择其他停止键。"
            )
        self._update_controls()
        if not available and self._hotkey is not None:
            error = getattr(self._hotkey, "error", "")
            if error:
                self.status_label.setText(f"全局停止键不可用：{error}")

    def _show_emergency_stop(self) -> None:
        self.status_label.setText("已通过全局停止键取消本轮。")

    def preview_without_input(self) -> None:
        if self._pending_capture_request is not None or self._pending_import_request is not None:
            self.status_label.setText("请先等待采集或模板导入完成。")
            return
        if self.identity is None or self.button is None:
            self.status_label.setText("先选择目标窗口并框选按钮模板。")
            return
        if not self._profile_content_ready:
            self.status_label.setText("当前配置尚未载入；请先载入或新建配置。")
            return
        if not self._templates_match_current_target():
            self.status_label.setText(self._template_compatibility_message())
            return
        self._invalidate_semantics()
        if not self._configure_engine("无点击预览会取消当前轮"):
            return
        self._queue("preview", self._semantic_revision, "正在进行无点击识别预览…")

    def start_round(self) -> None:
        self._start_failure_key = None
        self._start_failure_message = ""
        if (self._pending_capture_request is not None or self._pending_import_request is not None
                or self._config_loading):
            self.status_label.setText("请先等待采集、模板导入或配置载入完成。")
            return
        if not self._profile_content_ready:
            self.status_label.setText("当前配置尚未载入；请先载入或新建配置。")
            return
        if self.identity is None or self.button is None:
            self.status_label.setText("先选择目标窗口并框选按钮模板。")
            return
        if not self._templates_match_current_target():
            self.status_label.setText(self._template_compatibility_message())
            return
        if self.selection_combo.currentData() is not None:
            self.selection_combo.setCurrentIndex(0)
        self._static_frame = None
        if not self._configure_engine():
            return
        if self._pending_start_request is not None:
            return
        allow_activation = bool(
            self.mode == Mode.AUTO and self.allow_activation_checkbox.isChecked()
        )
        request_id = self.worker.submit(
            "start", (self._semantic_revision, allow_activation)
        )
        if request_id is None:
            self.status_label.setText("后台任务繁忙，未能开始本轮，请稍后重试。")
            return
        self._pending_start_request = request_id
        self._manual_pause_requested = False
        self._allow_activation_round = allow_activation
        self.allow_activation_checkbox.blockSignals(True)
        self.allow_activation_checkbox.setChecked(False)
        self.allow_activation_checkbox.blockSignals(False)
        self.status_label.setText("正在检查目标并开始本轮等待…")
        self._update_controls()

    def pause_round(self) -> None:
        if self._pending_start_request is not None:
            self._invalidate_semantics()
            self.engine.stop("开始尚未完成，已取消")
            self.status_label.setText("本轮尚未启用，已取消待执行的开始操作。")
            self._update_controls()
            return
        if self.engine.snapshot().state not in {
            State.MONITORING, State.CONFIRMING, State.ATTEMPTED, State.VERIFYING, State.PAUSED,
            State.BACKGROUND, State.WAITING_FOREGROUND, State.ACTIVATING, State.REMINDED,
        }:
            return
        self.worker.invalidate_authorization()
        self._manual_pause_requested = True
        self.engine.pause()
        self._update_controls(self.engine.snapshot().state)

    def resume_round(self) -> None:
        if not self._manual_pause_requested:
            return
        self._manual_pause_requested = False
        self.engine.resume()
        self._update_controls(self.engine.snapshot().state)

    def stop_round(self) -> None:
        self._invalidate_semantics()
        self.engine.stop("已手动停止")
        self._allow_activation_round = False
        self.status_label.setText("本轮已停止。")
        self._update_controls()

    def _toggle_pause_round(self) -> None:
        if self.engine.snapshot().state == State.PAUSED and self._manual_pause_requested:
            self.resume_round()
        else:
            self.pause_round()

    def _edit_profile_metadata(
        self, title: str, description: str, initial: ProfileMetadata
    ) -> ProfileMetadata | None:
        dialog = ProfileMetadataDialog(initial, title, description, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.metadata()

    def rename_profile(self) -> None:
        if self._profile_action_in_progress() or self._import_action_in_progress():
            return
        if not self._profile_content_ready:
            self.status_label.setText("当前配置尚未载入，不能重命名；请先载入或新建配置。")
            return
        self._stop_before_profile_dialog("重命名配置")
        metadata = self._edit_profile_metadata(
            "重命名配置",
            "保存后会将名称、游戏标签和当前配置一起写入当前配置文件。",
            self._current_profile_metadata,
        )
        if metadata is None:
            self.status_label.setText("已取消重命名；本轮保持停止。")
            return
        self.save_config(metadata=metadata)

    def save_config(self, *, metadata: ProfileMetadata | None = None) -> None:
        if self._profile_action_in_progress():
            return
        if self._import_action_in_progress():
            return
        if not self._profile_content_ready:
            self.status_label.setText("当前配置尚未载入，不能保存；请先载入或新建配置。")
            return
        metadata = metadata or self._current_profile_metadata
        try:
            metadata.validate()
            if not metadata.name.strip():
                raise ValueError("请输入配置名称。")
            self.settings.validate()
            self.region.validate()
            self._queue_profile_action(
                "save_config",
                (self._current_config_path, self._semantic_revision, self.settings,
                 self.region, self.button, self.success, metadata),
                "正在保存当前配置…",
            )
        except ValueError as exc:
            self.status_label.setText(f"配置无效：{exc}")

    def save_config_as(self) -> None:
        if self._profile_action_in_progress():
            return
        if self._import_action_in_progress():
            return
        if not self._profile_content_ready:
            self.status_label.setText("当前配置尚未载入，不能另存；请先载入或新建配置。")
            return
        self._stop_before_profile_dialog("另存为配置")
        metadata = self._edit_profile_metadata(
            "另存为配置",
            "保存时会将名称、游戏标签和当前配置一起写入新文件。",
            self._current_profile_metadata,
        )
        if metadata is None:
            self.status_label.setText("已取消另存为；本轮保持停止。")
            return
        default_path = self._current_config_path or getattr(self.store, "config_path", None)
        default_name = str(default_path) if default_path is not None else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为配置", default_name, "JSON 配置 (*.json)"
        )
        if not path:
            self.status_label.setText("已取消另存为；本轮保持停止。")
            return
        self._save_config_to_path(Path(path), metadata)

    def _save_config_to_path(self, path: Path, metadata: ProfileMetadata) -> None:
        if not self._profile_content_ready:
            self.status_label.setText("当前配置尚未载入，不能另存；请先载入或新建配置。")
            return
        try:
            metadata.validate()
            if not metadata.name.strip():
                raise ValueError("请输入配置名称。")
            self.settings.validate()
            self.region.validate()
            self._queue_profile_action(
                "save_config_as",
                (Path(path), self._semantic_revision, self.settings, self.region,
                 self.button, self.success, metadata),
                "正在保存配置快照…",
            )
        except ValueError as exc:
            self.status_label.setText(f"配置无效：{exc}")

    def choose_config_to_load(self, *, from_wizard: bool = False) -> None:
        if self._profile_action_in_progress():
            return
        if self._import_action_in_progress():
            return
        self._stop_before_profile_dialog("选择要载入的配置")
        if self._known_profiles:
            dialog = ProfileSelectionDialog(self._profile_choices(), self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.status_label.setText("已取消载入配置选择；本轮保持停止。")
                return
            if dialog.choice is not None:
                self._request_profile_switch(dialog.choice.path, from_wizard=from_wizard)
                return
            if not dialog.browse_requested:
                return
        current_path = self._current_config_path or getattr(self.store, "config_path", None)
        start_directory = str(current_path.parent) if current_path is not None else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择并载入配置", start_directory, "JSON 配置 (*.json)"
        )
        if not path:
            self.status_label.setText("已取消载入配置选择；本轮保持停止。")
            return
        self._request_profile_switch(Path(path), from_wizard=from_wizard)

    def load_config(self, path: Path | None = None) -> None:
        """Load the default configuration or an explicitly selected profile."""
        if self._profile_action_in_progress():
            return
        if self._import_action_in_progress():
            return
        self._stop_before_profile_dialog("载入配置")
        self._request_profile_switch(Path(path) if path is not None else None)

    def _queue_config_load(
        self, path: Path | None, *, from_wizard: bool = False, startup: bool = False
    ) -> bool:
        self._stop_before_profile_dialog("载入配置")
        self._pending_profile_switch = Path(path) if path is not None else None
        self._pending_profile_switch_active = True
        self._pending_profile_switch_from_wizard = from_wizard
        self._config_loading = True
        self._update_controls()
        self._queue_profile_action(
            "load_config", (Path(path) if path is not None else None,
                            self._semantic_revision), "正在载入配置…"
        )
        accepted = self._pending_profile_request is not None
        if accepted:
            self._loading_from_wizard = from_wizard
        else:
            self._pending_profile_switch_from_wizard = False
            if startup:
                self._config_loading = False
                self._update_controls()
        return accepted

    def import_template(self) -> None:
        if self._profile_action_in_progress():
            return
        if self._pending_import_request is not None:
            self.status_label.setText("模板导入中，请稍候。")
            return
        if self.geometry is None:
            self.status_label.setText("先选择并检查目标窗口，再导入模板图片。")
            return
        template_kind = self.selection_combo.currentData()
        if template_kind not in {"button", "success"}:
            self.status_label.setText("请先选择要导入的模板类型：按钮模板或成功标志。")
            return
        self._stop_before_profile_dialog("导入模板")
        path, _ = QFileDialog.getOpenFileName(
            self, "导入模板图片", "", "PNG 图片 (*.png)"
        )
        if not path:
            self.status_label.setText("已取消导入模板；本轮保持停止。")
            self._update_controls()
            return
        payload = (path, self.geometry, self.identity, self._semantic_revision)
        self._pending_import_request = self._queue(
            "import_template", (*payload, template_kind), "正在导入模板…"
        )
        self._update_controls()

    def test_sound(self) -> None:
        self._queue("test_sound", label="正在测试系统提示音…")

    def _apply_settings_widgets(self) -> None:
        widgets = (self.threshold_spin, self.frames_spin, self.stable_spin, self.timeout_spin)
        for widget in widgets:
            widget.blockSignals(True)
        self.threshold_spin.setValue(self.settings.threshold)
        self.frames_spin.setValue(self.settings.consecutive_frames)
        self.stable_spin.setValue(self.settings.stable_seconds)
        self.timeout_spin.setValue(self.settings.round_timeout)
        self.hotkey_combo.blockSignals(True)
        self.hotkey_combo.setCurrentText(self.settings.hotkey)
        self.hotkey_combo.blockSignals(False)
        self.capture_method_combo.blockSignals(True)
        capture_index = self.capture_method_combo.findData(self.settings.capture_method)
        self.capture_method_combo.setCurrentIndex(max(0, capture_index))
        self.capture_method_combo.blockSignals(False)
        self.sound_enabled_checkbox.blockSignals(True)
        self.sound_enabled_checkbox.setChecked(self.settings.sound_enabled)
        self.sound_enabled_checkbox.blockSignals(False)
        for widget in widgets:
            widget.blockSignals(False)

    def _apply_loaded_config(self, value: Any, path: Path | None) -> None:
        settings, region, button, success, metadata = value
        default_path = getattr(self.store, "config_path", None)
        metadata_path = Path(path) if path is not None else (
            Path(default_path) if default_path is not None else None
        )
        metadata = _display_profile_metadata(
            metadata, metadata_path, Path(default_path) if default_path is not None else None
        )
        self._pending_profile_switch = None
        self._pending_profile_switch_active = False
        self._pending_profile_switch_from_wizard = False
        self._invalidate_semantics()
        self._config_loading = False
        self._profile_content_ready = True
        self._profile_content_error = ""
        self.engine.stop("配置已载入；需要手动预览或开始")
        previous_hotkey = self.settings.hotkey
        self.settings, self.region, self.button, self.success = settings, region, button, success
        self.mode = Mode.REMIND
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.blockSignals(False)
        self._apply_settings_widgets()
        if self.settings.hotkey != previous_hotkey:
            self._install_hotkey()
        # Loading never selects a target or starts a round. A target selected this launch
        # remains explicitly selected, but the loaded templates are only configured for it.
        self._configure_engine("配置已载入；请手动预览并开始")
        self._update_target_info(self.geometry)
        if path is None:
            default_path = getattr(self.store, "config_path", None)
            self._set_current_config_path(
                Path(default_path) if default_path is not None else None
            )
        else:
            self._set_current_config_path(path)
        self._set_current_profile_metadata(metadata)
        self._saved_config_signature = self._config_signature()
        self._update_dirty_state()
        self.status_label.setText("配置已载入；本轮未启用，请检查模板后手动预览并开始。")
        if self._loading_from_wizard and self._wizard is not None:
            wizard = self._wizard
            self._loading_from_wizard = False
            wizard.reject()
            self.status_label.setText("已载入所选配置；向导草稿已关闭，本轮未启用。")
        else:
            self._loading_from_wizard = False
        self._update_controls()

    def _handle_result(self, result: _WorkerResult) -> None:
        if result.error:
            if result.action in {"save_config", "save_config_as", "load_config"}:
                if result.request_id != self._pending_profile_request:
                    return
                action = self._pending_profile_action
                self._pending_profile_request = None
                self._pending_profile_action = None
                self._pending_profile_switch = None
                self._pending_profile_switch_active = False
                self._pending_profile_switch_from_wizard = False
                if action == "load_config":
                    self._config_loading = False
                    self._loading_from_wizard = False
                    if not self._profile_content_ready:
                        self._profile_content_error = result.error
                    self._refresh_profiles()
                self.status_label.setText(f"后台操作失败：{result.error}")
                self._restore_profile_combo()
                self._update_dirty_state()
                self._update_controls()
                return
            if result.action.startswith("wizard_"):
                if result.request_id != self._wizard_request:
                    return
                action = self._wizard_action
                self._wizard_request = None
                self._wizard_action = None
                if self._wizard is not None:
                    if action == "wizard_save":
                        self._wizard_save_path = None
                        self._wizard.save_failed(result.error)
                    elif action == "wizard_capture":
                        self._wizard.capture_failed(result.error)
                    elif action == "wizard_recognize":
                        self._wizard.check_button.setEnabled(True)
                        self._wizard.recognition_feedback.setText(
                            f"检查失败：{result.error}。可以调整后重试或保存为未确认配置。"
                        )
                    elif action == "wizard_inspect":
                        self._wizard.target_feedback.setText(
                            f"无法检查所选窗口：{result.error}"
                        )
                    else:
                        self._wizard.target_feedback.setText(
                            f"无法读取窗口列表：{result.error}"
                        )
                self._queue_pending_wizard_inspect()
                return
            if result.action == "capture" and result.request_id == self._pending_capture_request:
                self._pending_capture_request = None
                self._capture_clears_selections = False
                self._update_controls()
            if (result.action == "import_template"
                    and result.request_id == self._pending_import_request):
                self._pending_import_request = None
                self._update_controls()
            if result.action == "refresh_targets":
                self._refreshing_targets = False
                self._update_controls()
            if result.action == "list_profiles":
                self.status_label.setText(f"配置列表读取失败：{result.error}")
                return
            if result.action == "start" and result.request_id == self._pending_start_request:
                self._pending_start_request = None
                snapshot = self.engine.snapshot()
                self._start_failure_key = (snapshot.state, snapshot.reason)
                self._start_failure_message = f"无法开始：{result.error}"
                self.status_label.setText(self._start_failure_message)
                self._update_controls()
                return
            if result.action != "tick":
                self.status_label.setText(f"后台操作失败：{result.error}")
            return
        value = result.value
        if result.action == "list_profiles":
            self._populate_profile_combo(value)
        elif result.action == "wizard_targets":
            if self._wizard is None or result.request_id != self._wizard_request:
                return
            self._wizard_request = None
            self._wizard_action = None
            self._wizard.set_targets(value)
            self._queue_pending_wizard_inspect()
        elif result.action == "wizard_inspect":
            if self._wizard is None or result.request_id != self._wizard_request:
                return
            self._wizard_request = None
            self._wizard_action = None
            identity, geometry = value
            if identity == self._wizard.draft.identity:
                self._wizard.set_target_geometry(identity, geometry)
            elif self._wizard.draft.identity is not None:
                self._wizard_pending_identity = self._wizard.draft.identity
            self._queue_pending_wizard_inspect()
        elif result.action == "wizard_capture":
            if self._wizard is None or result.request_id != self._wizard_request:
                return
            self._wizard_request = None
            self._wizard_action = None
            identity, frame, geometry = value
            if identity == self._wizard.draft.identity and frame.identity == identity:
                self._wizard.set_target_geometry(identity, geometry)
                self._wizard.set_frame(frame)
            elif self._wizard.draft.identity is not None:
                self._wizard_pending_identity = self._wizard.draft.identity
            self._queue_pending_wizard_inspect()
        elif result.action == "wizard_recognize":
            if self._wizard is None or result.request_id != self._wizard_request:
                return
            self._wizard_request = None
            self._wizard_action = None
            identity, draft_revision, detection = value
            if (identity == self._wizard.draft.identity
                    and draft_revision == self._wizard_revision):
                self._wizard.recognition_completed(detection)
            elif self._wizard.draft.identity is not None:
                self._wizard_pending_identity = self._wizard.draft.identity
            self._queue_pending_wizard_inspect()
        elif result.action == "wizard_save":
            if self._wizard is None or result.request_id != self._wizard_request:
                return
            self._wizard_request = None
            self._wizard_action = None
            _intent, path, draft, metadata = value
            self._commit_wizard_draft(
                Path(path) if path is not None else None, draft, metadata
            )
        elif result.action == "refresh_targets":
            self._refreshing_targets = False
            current = self.identity
            self.target_combo.blockSignals(True)
            self.target_combo.clear()
            self.target_combo.addItem("请选择目标窗口", None)
            own_hwnd = int(self.winId())
            available = [identity for identity in value if identity.hwnd != own_hwnd]
            for number, identity in enumerate(available, start=1):
                label = (
                    f"窗口 {number} · {identity.title or '（无标题）'} · "
                    f"{Path(identity.executable).name}"
                )
                self.target_combo.addItem(label, identity)
            self.target_combo.setCurrentIndex(0)
            self.target_combo.blockSignals(False)
            # Never implicitly rebind a previous target after refreshing the inventory.
            if current is not None:
                self.status_label.setText("窗口列表已刷新；请重新明确选择目标窗口。")
                self._invalidate_semantics()
                self.identity = None
                self.geometry = None
                self._static_frame = None
                snapshot = self.engine.snapshot()
                self._last_displayed_key = (
                    (snapshot.frame.session_id, snapshot.frame.sequence)
                    if snapshot.frame is not None else None
                )
                self.preview.set_frame(None)
                self.target_info.setText("未选择目标；请重新明确选择窗口。")
                self._refresh_template_preview()
                self.engine.stop("窗口列表已刷新")
                self._update_controls()
            elif available:
                self.status_label.setText(f"已找到 {len(available)} 个可见窗口，请选择目标。")
            else:
                self.status_label.setText("未发现可用窗口；请打开目标应用后刷新。")
        elif result.action == "inspect":
            identity, geometry = value
            if identity != self.identity:
                return
            self.geometry = geometry
            self._update_target_info(geometry)
            self._refresh_template_preview()
            self._update_controls()
        elif result.action == "capture":
            if result.request_id != self._pending_capture_request:
                return
            self._pending_capture_request = None
            frame, geometry = value
            if frame.identity != self.identity:
                self._capture_clears_selections = False
                return
            previous = self.geometry
            if self._capture_clears_selections:
                self.button = None
                self.success = None
                self.region = Region()
            self._capture_clears_selections = False
            if previous is not None and previous.template_key != geometry.template_key:
                self.region = Region()
            self._static_frame = frame
            self.geometry = geometry
            self.preview.set_frame(frame)
            self._update_target_info(geometry)
            if self.button is not None and not self.button.compatible(geometry):
                self.status_label.setText(
                    "静态截图已更新；模板保留但与当前窗口尺寸或 DPI 不匹配。"
                    "恢复对应窗口设置或修改模板。"
                )
            elif self.button is not None:
                self.status_label.setText(
                    "静态截图已更新；现有按钮模板与当前窗口匹配，无需重复框选。"
                )
            else:
                self.status_label.setText(
                    "截图仅保存在内存；请选择按钮模板、搜索区域或成功标志进行框选。"
                )
            self._update_dirty_state()
            self._update_controls()
        elif result.action == "preview":
            if value is None:
                return
            revision, snapshot = value
            if revision != self._semantic_revision or snapshot is None:
                return
            if snapshot.frame is not None and not self.preview.is_selection_enabled:
                self.preview.set_frame(snapshot.frame, snapshot.detection.candidates
                                       if snapshot.detection else ())
            self.status_label.setText(f"无点击预览：{snapshot.state.value} · {snapshot.reason}")
        elif result.action == "start":
            if result.request_id != self._pending_start_request:
                return
            self._pending_start_request = None
            _revision, session_id, allow_activation = value
            self._allow_activation_round = bool(allow_activation)
            mode_text = "仅提醒，不会点击" if self.mode == Mode.REMIND else "自动确认已由你手动开始"
            if allow_activation:
                mode_text += "，本轮允许请求切回"
            self.status_label.setText(f"本轮已手动开始（{session_id}）；{mode_text}。")
            self._update_controls()
        elif result.action in {"save_config", "save_config_as", "load_config"}:
            if result.request_id != self._pending_profile_request:
                return
            action = self._pending_profile_action
            self._pending_profile_request = None
            self._pending_profile_action = None
            if action == "load_config":
                revision, path, config = value
                if revision == self._semantic_revision:
                    self._apply_loaded_config(config, path)
                    self._refresh_profiles()
                else:
                    self._pending_profile_switch = None
                    self._pending_profile_switch_active = False
                    self._pending_profile_switch_from_wizard = False
                    self._config_loading = False
                    self._loading_from_wizard = False
                    self.status_label.setText("载入结果已作废，因为你随后更改了设置。")
                    self._restore_profile_combo()
                    self._update_dirty_state()
                    self._refresh_profiles()
                    self._update_controls()
            elif action == "save_config_as":
                path, revision, metadata = value
                if revision == self._semantic_revision:
                    self._set_current_config_path(path)
                    self._set_current_profile_metadata(metadata)
                    self._saved_config_signature = self._config_signature()
                    self._update_dirty_state()
                    self.status_label.setText("配置和模板已另存；本轮保持停止。")
                    if self._pending_profile_switch_active:
                        switch_path = self._pending_profile_switch
                        from_wizard = self._pending_profile_switch_from_wizard
                        self._pending_profile_switch = None
                        self._pending_profile_switch_active = False
                        self._pending_profile_switch_from_wizard = False
                        self._queue_config_load(switch_path, from_wizard=from_wizard)
                    else:
                        self._refresh_profiles()
                else:
                    self.status_label.setText(
                        "已保存选择时的配置快照；设置随后已更改，当前配置位置未切换。"
                    )
                    if self._pending_profile_switch_active:
                        self._pending_profile_switch = None
                        self._pending_profile_switch_active = False
                        self._pending_profile_switch_from_wizard = False
                        self._restore_profile_combo()
                self._update_controls()
            else:
                _path, revision, metadata = value
                if revision == self._semantic_revision:
                    self._set_current_profile_metadata(metadata)
                    self._saved_config_signature = self._config_signature()
                    self._update_dirty_state()
                    self.status_label.setText("当前配置和模板已保存。")
                    if self._pending_profile_switch_active:
                        switch_path = self._pending_profile_switch
                        from_wizard = self._pending_profile_switch_from_wizard
                        self._pending_profile_switch = None
                        self._pending_profile_switch_active = False
                        self._pending_profile_switch_from_wizard = False
                        self._queue_config_load(switch_path, from_wizard=from_wizard)
                    else:
                        self._refresh_profiles()
                else:
                    self.status_label.setText("已保存配置快照；设置随后已更改。")
                    if self._pending_profile_switch_active:
                        self._pending_profile_switch = None
                        self._pending_profile_switch_active = False
                        self._pending_profile_switch_from_wizard = False
                        self._restore_profile_combo()
                self._update_controls()
        elif result.action == "import_template":
            if result.request_id != self._pending_import_request:
                return
            self._pending_import_request = None
            identity, revision, template_kind, template = value
            if identity != self.identity or revision != self._semantic_revision:
                return
            self._invalidate_semantics()
            if template_kind == "button":
                self.button = template
            else:
                self.success = template
            self._configure_engine("模板已导入，当前轮已取消")
            self.status_label.setText(f"已导入模板：{template.name}；请先进行无点击预览。")
            self._update_dirty_state()
            self._update_controls()
        else:
            self.status_label.setText(str(value))

    def _update_target_info(self, geometry: Geometry | None = None) -> None:
        if self.identity is None:
            self.target_info.setText("未选择目标；配置文件不会保存窗口绑定。")
            return
        geometry = geometry or self.geometry
        base = (f"{self.identity.title or '（无标题）'} · {self.identity.executable} · "
                f"PID {self.identity.pid} · HWND {self.identity.hwnd}")
        if geometry is None:
            self.target_info.setText(base + " · 客户区信息未知")
        else:
            self.target_info.setText(
                f"{base} · 客户区物理坐标 ({geometry.left}, {geometry.top}) "
                f"{geometry.width}×{geometry.height} · DPI {geometry.dpi}"
            )
            incompatible = any(
                template is not None and not template.compatible(geometry)
                for template in (self.button, self.success)
            )
            if incompatible:
                self.target_info.setText(
                    self.target_info.text()
                    + " · 模板保留；恢复窗口尺寸/DPI或修改模板"
                )

    def _refresh_worker_state(self) -> None:
        for result in self.worker.take_results():
            self._handle_result(result)
        hotkey_available = bool(self._hotkey is not None and self._hotkey.available)
        if hotkey_available != self._hotkey_available:
            self._hotkey_available = hotkey_available
            if not hotkey_available:
                self._hotkey_error = getattr(self._hotkey, "error", "") or "停止键线程已退出"
                self.hotkey_status_label.setText(
                    f"不可用：{self._hotkey_error}；自动确认已禁用。"
                )
                self.status_label.setText("全局停止键已失效，自动确认已禁用；请检查停止键设置。")
            else:
                self.hotkey_status_label.setText(f"已注册：{self.settings.hotkey}")
            self._update_controls()
        self.hotkey_summary_label.setText(
            f"全局停止键：{'已注册' if self._hotkey_available else '不可用'}"
            f"（{self.settings.hotkey}）"
        )
        snapshot = self.worker.latest_snapshot()
        if snapshot is None:
            self._show_capture_release_errors()
            return
        if snapshot.frame is not None:
            key = (snapshot.frame.session_id, snapshot.frame.sequence)
            if key != self._last_displayed_key and not self.preview.is_selection_enabled:
                detection = snapshot.detection
                self.preview.set_frame(snapshot.frame,
                                       detection.candidates if detection is not None else ())
                self._last_displayed_key = key
                self.geometry = snapshot.frame.geometry
                self._update_target_info(self.geometry)
        elapsed_label = "已用（含暂停）"
        if not snapshot.session_id:
            elapsed_label = "已用"
        elif snapshot.state in TERMINAL:
            elapsed_label = ("上轮用时（已结束）" if snapshot.state == State.DISARMED
                             else "本轮用时（已结束）")
        details = [f"状态：{snapshot.state.value}", f"原因：{snapshot.reason}",
                   f"轮次：{snapshot.session_id or '无'}",
                   f"{elapsed_label}：{snapshot.elapsed:.1f} 秒"]
        state_text = self._state_summary(snapshot)
        self.state_label.setText(state_text)
        status_key = (snapshot.state, snapshot.reason)
        if self._start_failure_message:
            self._last_dashboard_status = status_key
            if status_key == self._start_failure_key:
                self.status_label.setText(self._start_failure_message)
                self._start_failure_key = None
                self._start_failure_message = ""
        elif status_key != self._last_dashboard_status:
            self.status_label.setText(self._reason_and_next_step(snapshot))
            self._last_dashboard_status = status_key
        if snapshot.capture_status is not None:
            capture = snapshot.capture_status
            self._capture_backend_name = capture.backend
            self.capture_status_label.setText(
                f"捕获：{capture.backend}；支持={capture.supported}；"
                f"当前画面有效={capture.valid_frame}；后台识别={capture.background_supported}；"
                f"前台={capture.foreground}；遮挡={capture.obscured}；最小化={capture.minimized}；"
                f"新帧={capture.fresh}。{capture.reason}"
            )
        elif snapshot.foreground is not None:
            self._capture_backend_name = getattr(
                snapshot.frame, "backend", self._capture_backend_name
            )
            self.capture_status_label.setText(
                f"捕获：{self._capture_backend_name}；前台={snapshot.foreground}；"
                "详细能力状态由当前目标检查结果提供。"
            )
        self._show_capture_release_errors()
        if self._tray_status_action is not None:
            self._tray_status_action.setText(f"状态：{state_text} · {snapshot.reason}")
        if snapshot.detection is not None:
            detection = snapshot.detection
            details.append(f"最高原始分数：{detection.best_score:.6f}")
            details.append(f"拒绝 / 匹配原因：{detection.reason or '候选已通过当前识别条件'}")
            if detection.candidates:
                details.extend(
                    f"候选 {index}: 分数={candidate.score:.6f}，区域="
                    f"({candidate.rect.x},{candidate.rect.y}) "
                    f"{candidate.rect.width}×{candidate.rect.height}"
                    for index, candidate in enumerate(detection.candidates, start=1)
                )
        else:
            details.append("尚无识别结果")
        self.candidates_text.setPlainText("\n".join(details))
        self._update_controls(snapshot.state)

    def _show_capture_release_errors(self) -> None:
        errors = self.worker.capture_release_errors()
        if errors:
            message = "捕获资源释放失败（保留错误记录）：" + "；".join(errors)
            self.capture_status_label.setText(message)
            self.capture_release_error_label.setText(message)
            self.capture_release_error_label.show()

    def _state_summary(self, snapshot: Snapshot) -> str:
        state = snapshot.state
        if state == State.DISARMED:
            if self.identity is None:
                return "未配置目标；尚未开始"
            if self.button is None:
                return "配置未完成；尚未开始"
            return "已就绪；尚未开始"
        if state == State.MONITORING:
            return "正在前台等待"
        if state == State.BACKGROUND:
            return "正在后台等待"
        if state == State.WAITING_FOREGROUND:
            return "已发现按钮，等待你切回"
        if state == State.ACTIVATING:
            return "正在尝试切回"
        if state == State.REMINDED:
            return "已提醒"
        if state == State.CONFIRMING:
            return "正在确认按钮"
        if state == State.ATTEMPTED:
            return "已尝试点击，结果尚未确认"
        if state == State.VERIFYING:
            return "正在验证结果"
        if state == State.SUCCESS:
            return "已验证成功"
        if state == State.UNCONFIRMED:
            return "本轮结束；结果未确认"
        if state == State.PAUSED:
            return "已暂停"
        if state == State.ERROR:
            return "本轮失败并已停止"
        return state.value

    def _reason_and_next_step(self, snapshot: Snapshot) -> str:
        next_step = {
            State.DISARMED: (
                "点击‘开始设置’或载入已有配置。"
                if self.button is None else "准备完成；点击‘开始等待’后才会启动本轮。"
            ),
            State.MONITORING: "可以切换到其他窗口；当前捕获状态见高级诊断。",
            State.BACKGROUND: "后台识别仍需当前目标提供有效新画面。",
            State.WAITING_FOREGROUND: "请手动切回目标窗口，程序会重新识别。",
            State.ACTIVATING: "系统可能拒绝切换；此时程序只会提醒。",
            State.REMINDED: "请查看通知，并按需要手动处理。",
            State.ATTEMPTED: "已点击不代表准备完成；等待成功证据。",
            State.VERIFYING: "正在等待成功标志模板的证据。",
            State.SUCCESS: "本轮已结束。",
            State.UNCONFIRMED: "本轮已结束；没有足够证据确认结果。",
            State.PAUSED: "点击‘继续’后恢复本轮。",
            State.ERROR: "查看高级诊断，再决定是否重新开始。",
        }.get(snapshot.state, "")
        reason = snapshot.reason.strip()
        return f"{reason} 下一步：{next_step}" if next_step else reason

    def _update_controls(self, state: State | None = None) -> None:
        if state is None:
            state = self.engine.snapshot().state
        has_target = self.identity is not None
        compatible = (self.geometry is not None and self.button is not None
                      and self.button.compatible(self.geometry)
                      and (self.success is None or self.success.compatible(self.geometry)))
        ready = (has_target and compatible and not self._config_loading
                 and self._profile_content_ready
                 and self._pending_capture_request is None
                 and self._pending_import_request is None
                 and not self._refreshing_targets)
        self.mode_combo.model().item(1).setEnabled(self._hotkey_available)
        start_allowed = self.mode != Mode.AUTO or self._hotkey_available
        self.refresh_button.setEnabled(not self._refreshing_targets)
        capture_allowed = has_target and self._pending_capture_request is None
        self.capture_button.setEnabled(capture_allowed)
        self.recollect_button.setEnabled(capture_allowed)
        self.preview_button.setEnabled(ready)
        active_states = {
            State.MONITORING, State.CONFIRMING, State.ATTEMPTED, State.VERIFYING,
            State.BACKGROUND, State.WAITING_FOREGROUND, State.ACTIVATING,
        }
        paused = state == State.PAUSED and self._manual_pause_requested
        active = state in active_states
        self.start_button.setText("继续等待" if paused else "开始等待")
        self.start_button.setEnabled(
            (ready and start_allowed and self._pending_start_request is None
             and not active) or paused
        )
        self.pause_button.setText("暂停")
        self.pause_button.setVisible(not paused)
        self.pause_button.setEnabled(
            self._pending_start_request is not None or active or paused
        )
        self.resume_button.setEnabled(
            self._manual_pause_requested and state == State.PAUSED
        )
        self.stop_button.setEnabled(
            active or paused or self._pending_start_request is not None
        )
        self.allow_activation_checkbox.setVisible(self.mode == Mode.AUTO)
        self.allow_activation_checkbox.setEnabled(
            self.mode == Mode.AUTO and not active and not paused
            and self._pending_start_request is None
        )
        self.onboarding_button.setText(
            "修改当前配置…" if self.button is not None else "设置当前配置"
        )
        self.onboarding_button.setEnabled(
            self._profile_content_ready and not self._config_loading
            and self._pending_profile_request is None
            and self._pending_capture_request is None
        )
        profile_available = (self._pending_profile_request is None
                             and self._pending_import_request is None
                             and not self._config_loading)
        current_write_allowed = self._profile_content_ready and profile_available
        self.save_button.setEnabled(current_write_allowed)
        self.load_button.setEnabled(profile_available)
        self.save_as_button.setEnabled(current_write_allowed)
        self.rename_profile_button.setEnabled(current_write_allowed)
        self.load_profile_dashboard_button.setEnabled(profile_available)
        self.save_as_dashboard_button.setEnabled(current_write_allowed)
        self.new_profile_button.setEnabled(profile_available)
        self.profile_combo.setEnabled(profile_available)
        self.import_button.setEnabled(self._profile_content_ready and self.geometry is not None and
                                      self._pending_import_request is None and
                                      self._pending_profile_request is None)

    def closeEvent(self, event: Any) -> None:
        if not self._shutdown_started:
            self._begin_shutdown()
        if self.worker.isRunning():
            event.ignore()
            self._ensure_shutdown_polling()
            return
        self._finish_close()
        event.accept()

    def _begin_shutdown(self) -> None:
        self._shutdown_started = True
        self.setEnabled(False)
        if hasattr(self, "_timer"):
            self._timer.stop()
        self.worker.invalidate_authorization()
        try:
            self.engine.close()
        finally:
            self.worker.shutdown()
            if self._hotkey is not None:
                self._hotkey.close()
                self._hotkey = None

    def _ensure_shutdown_polling(self) -> None:
        if self._shutdown_poll_timer is None:
            self._shutdown_poll_timer = QTimer(self)
            self._shutdown_poll_timer.setInterval(20)
            self._shutdown_poll_timer.timeout.connect(self._worker_finished_for_close)
        if not self._shutdown_poll_timer.isActive():
            self._shutdown_poll_timer.start()

    def _worker_finished_for_close(self) -> None:
        if not self._shutdown_started:
            return
        if self.worker.isRunning():
            self._ensure_shutdown_polling()
            return
        self.close()

    def _finish_close(self) -> None:
        self._closed = True
        if self._tray_icon is not None:
            self._tray_icon.setVisible(False)
        if self._shutdown_poll_timer is not None:
            self._shutdown_poll_timer.stop()


def _process_gui_until_thread_stops(thread: QThread) -> None:
    """Keep GUI dispatch alive while joining a worker that may be waiting on it."""
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("Cannot safely join the UI worker without a Qt application")
    while thread.isRunning():
        app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 25)
        time.sleep(0.001)


def close_and_wait(window: MainWindow) -> None:
    """Close a window and dispatch Qt events until its worker has actually exited."""
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("Cannot safely close the UI without a Qt application")
    window.close()
    while not window._closed:
        app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 25)
        if not window.worker.isRunning():
            window.close()
        if not window._closed:
            time.sleep(0.001)
    if window.worker.isRunning():
        raise RuntimeError("UI close completed while the worker was still running")
