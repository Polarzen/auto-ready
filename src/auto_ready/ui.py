"""Chinese desktop workflow for explicit, single-round ready assistance."""

from __future__ import annotations

import queue
import threading
import time
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
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

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
            return None
        return request_id

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
            if result.action in {"load_config", "save_config", "save_config_as"}:
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
            frame = self._backend.capture(identity, str(uuid.uuid4()))
            return frame, geometry
        if action == "preview":
            revision = payload
            with self._lock:
                if revision != self._semantic_revision:
                    raise RuntimeError("识别设置已更改，已取消过期预览")
            snapshot = self._engine.preview()
            with self._lock:
                if revision != self._semantic_revision:
                    return None
            return revision, snapshot
        if action == "start":
            revision, authorization_epoch = payload
            self._refresh_hotkey_availability()
            with self._lock:
                if (revision != self._semantic_revision
                        or authorization_epoch != self._authorization_epoch):
                    raise RuntimeError("识别设置已更改，已取消排队中的开始操作")
            session_id = self._engine.start()
            with self._lock:
                stale = (revision != self._semantic_revision
                         or authorization_epoch != self._authorization_epoch)
            if stale:
                self._engine.stop("识别设置已更改，已取消排队中的开始操作")
                raise RuntimeError("识别设置已更改，已取消排队中的开始操作")
            return revision, session_id
        if action == "save_config":
            path, revision, settings, region, button, success = payload
            default_path = getattr(self._store, "config_path", None)
            if path is None or (default_path is not None and Path(path) == Path(default_path)):
                self._store.save(settings, region, button, success)
            else:
                self._store.save_to(Path(path), settings, region, button, success)
            return Path(path) if path is not None else None, revision
        if action == "save_config_as":
            path, revision, settings, region, button, success = payload
            profile_path = Path(path)
            self._store.save_to(profile_path, settings, region, button, success)
            return profile_path, revision
        if action == "load_config":
            path, revision = payload
            config = self._store.load() if path is None else self._store.load_from(Path(path))
            return revision, path, config
        if action == "import_template":
            path, geometry, identity, revision, template_kind = payload
            from .config import load_template

            return identity, revision, template_kind, load_template(Path(path), geometry)
        if action == "test_sound":
            self._notifier()
            return "已播放系统提示音"
        raise ValueError(f"未知后台操作：{action}")

    def run(self) -> None:
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
                    snapshot = self._engine.tick()
                except Exception as exc:
                    self._record_result(_WorkerResult(0, "tick", error=str(exc)))
                else:
                    with self._lock:
                        self._snapshot = snapshot
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

    def __init__(self, backend: Any = None, input_adapter: Any = None, store: Any = None,
                 hotkey_factory: Any = None, *, notifier: Any = None, capture_delay: float = 3.0):
        super().__init__()
        from .config import ConfigStore
        from .native import GlobalHotkey, WindowsBackend, WindowsInput, hotkey_policy_reason
        from .sound import notify

        self.setWindowTitle("Auto Ready · 开始前逐项确认")
        self.resize(1060, 760)
        self.backend = backend if backend is not None else WindowsBackend()
        self.store = store if store is not None else ConfigStore()
        self._notifier = notifier if notifier is not None else notify
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
        self._pending_import_request: int | None = None
        self._pending_profile_request: int | None = None
        self._pending_profile_action: str | None = None
        self._config_loading = False
        config_path = getattr(self.store, "config_path", None)
        self._current_config_path = Path(config_path) if config_path is not None else None
        self._refreshing_targets = False
        self._hotkey_available = False
        self._hotkey_error = ""
        self._hotkey: Any = None
        self._closed = False
        self._shutdown_started = False
        self._shutdown_poll_timer: QTimer | None = None
        self._manual_pause_requested = False

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
            self._build_ui()
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._refresh_worker_state)
            self._install_hotkey()
            self.worker.start()
            self._timer.start(80)
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
        self.candidates_text = QPlainTextEdit()
        self.candidates_text.setReadOnly(True)
        self.candidates_text.setMaximumBlockCount(100)
        diagnostics_layout.addWidget(self.state_label)
        diagnostics_layout.addWidget(self.status_label)
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
        self.threshold_spin.valueChanged.connect(self._settings_changed)
        self.frames_spin.valueChanged.connect(self._settings_changed)
        self.stable_spin.valueChanged.connect(self._settings_changed)
        self.timeout_spin.valueChanged.connect(self._settings_changed)
        self.hotkey_combo.currentTextChanged.connect(self._settings_changed)
        self.hotkey_status_label = QLabel("正在注册全局停止键…")
        self.hotkey_status_label.setWordWrap(True)
        settings_form.addRow("最低匹配分数", self.threshold_spin)
        settings_form.addRow("连续帧数", self.frames_spin)
        settings_form.addRow("稳定时间", self.stable_spin)
        settings_form.addRow("本轮超时", self.timeout_spin)
        settings_form.addRow("全局停止键", self.hotkey_combo)
        settings_form.addRow("停止键状态", self.hotkey_status_label)
        right.addWidget(settings_box)

        config_box = QGroupBox("配置与提示音")
        config_layout = QVBoxLayout(config_box)
        self.current_config_label = QLabel()
        self.current_config_label.setWordWrap(True)
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
        self.setCentralWidget(central)
        self._update_controls()

    def _queue(self, action: str, payload: Any = None, label: str = "") -> int | None:
        request_id = self.worker.submit(action, payload)
        if request_id is None:
            self.status_label.setText("后台任务繁忙，请稍后重试。")
            return None
        if label:
            self.status_label.setText(label)
        return request_id

    def _update_current_config_label(self) -> None:
        path = self._current_config_path
        if path is None:
            self.current_config_label.setText("当前配置：默认配置")
            self.current_config_label.setToolTip("使用应用默认配置")
            return
        self.current_config_label.setText(f"当前配置：{path.name}")
        self.current_config_label.setToolTip(str(path))

    def _set_current_config_path(self, path: Path | None) -> None:
        self._current_config_path = Path(path) if path is not None else None
        self._update_current_config_label()

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
            self._update_controls()
            return
        self._pending_profile_request = request_id
        self._pending_profile_action = action
        self._update_controls()

    def _stop_before_profile_dialog(self, reason: str) -> None:
        self._invalidate_semantics()
        self.engine.stop(reason)

    def _invalidate_semantics(self) -> None:
        self.worker.invalidate_authorization()
        self._semantic_revision += 1
        self.worker.set_semantic_revision(self._semantic_revision)
        self._pending_start_request = None
        self._manual_pause_requested = False

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
        if self.identity is None:
            self.target_info.setText("未选择目标；配置文件不会保存窗口绑定。")
            if self_target:
                self.status_label.setText("不能选择 Auto Ready 自身窗口作为目标。")
            self._update_controls()
            return
        self.target_info.setText(
            f"{self.identity.title or '（无标题）'} · {self.identity.executable} · "
            f"PID {self.identity.pid} · HWND {self.identity.hwnd} · 正在检查客户区…"
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
        if clear_selections:
            self.button = None
            self.success = None
            self.region = Region()
        self._static_frame = None
        self.selection_combo.setCurrentIndex(0)
        self.preview.set_frame(None)
        request_id = self._queue("capture", (self.identity, self._capture_delay),
                                 "正在等待后采集静态截图…")
        self._pending_capture_request = request_id
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
            self._update_controls()
        except (ValueError, IndexError) as exc:
            if selection_kind == "button":
                self.button = None
            elif selection_kind == "success":
                self.success = None
            self._configure_engine(f"框选无效：{exc}")
            self.status_label.setText(f"框选无效：{exc}")
            self._update_controls()

    def _template_summary(self) -> str:
        pieces = [f"按钮模板：{'已设置' if self.button else '未设置'}",
                  f"搜索区域：{self.region.x:.2f}, {self.region.y:.2f}, "
                  f"{self.region.width:.2f} × {self.region.height:.2f}",
                  f"成功标志：{'已设置' if self.success else '未设置'}"]
        return "；".join(pieces)

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
        self._invalidate_semantics()
        self.engine.stop("参数或停止键已更改，当前轮已取消")
        self.settings = replace(
            self.settings,
            threshold=self.threshold_spin.value(),
            consecutive_frames=self.frames_spin.value(),
            stable_seconds=self.stable_spin.value(),
            round_timeout=self.timeout_spin.value(),
            hotkey=self.hotkey_combo.currentText(),
        )
        self._install_hotkey()
        self._configure_engine("参数或停止键已更改，当前轮已取消")

    def _mode_changed(self, index: int) -> None:
        mode = self.mode_combo.itemData(index)
        if mode is None:
            return
        self._invalidate_semantics()
        self.mode = Mode(mode)
        self._configure_engine("运行模式已更改，当前轮已取消")
        self.status_label.setText(f"模式：{self.mode.value}；当前轮已取消，请手动预览或开始。")

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
        self._invalidate_semantics()
        if not self._configure_engine("无点击预览会取消当前轮"):
            return
        self._queue("preview", self._semantic_revision, "正在进行无点击识别预览…")

    def start_round(self) -> None:
        if (self._pending_capture_request is not None or self._pending_import_request is not None
                or self._config_loading):
            self.status_label.setText("请先等待采集、模板导入或配置载入完成。")
            return
        if self.identity is None or self.button is None:
            self.status_label.setText("先选择目标窗口并框选按钮模板。")
            return
        if self.selection_combo.currentData() is not None:
            self.selection_combo.setCurrentIndex(0)
        self._static_frame = None
        if not self._configure_engine():
            return
        if self._pending_start_request is not None:
            return
        request_id = self.worker.submit("start", self._semantic_revision)
        if request_id is None:
            self.status_label.setText("后台任务繁忙，未能开始本轮，请稍后重试。")
            return
        self._pending_start_request = request_id
        self._manual_pause_requested = False
        self.status_label.setText("正在后台检查目标并启用本轮…")
        self._update_controls()

    def pause_round(self) -> None:
        if self._pending_start_request is not None:
            self._invalidate_semantics()
            self.engine.stop("开始尚未完成，已取消")
            self.status_label.setText("本轮尚未启用，已取消待执行的开始操作。")
            self._update_controls()
            return
        if self.engine.snapshot().state not in {
            State.MONITORING, State.CONFIRMING, State.ATTEMPTED, State.VERIFYING, State.PAUSED
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

    def save_config(self) -> None:
        if self._profile_action_in_progress():
            return
        if self._import_action_in_progress():
            return
        try:
            self.settings.validate()
            self.region.validate()
            self._queue_profile_action(
                "save_config",
                (self._current_config_path, self._semantic_revision, self.settings,
                 self.region, self.button, self.success),
                "正在保存当前配置…",
            )
        except ValueError as exc:
            self.status_label.setText(f"配置无效：{exc}")

    def save_config_as(self) -> None:
        if self._profile_action_in_progress():
            return
        if self._import_action_in_progress():
            return
        self._stop_before_profile_dialog("另存为配置")
        default_path = self._current_config_path or getattr(self.store, "config_path", None)
        default_name = str(default_path) if default_path is not None else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为配置", default_name, "JSON 配置 (*.json)"
        )
        if not path:
            self.status_label.setText("已取消另存为；本轮保持停止。")
            return
        self._save_config_to_path(Path(path))

    def _save_config_to_path(self, path: Path) -> None:
        try:
            self.settings.validate()
            self.region.validate()
            self._queue_profile_action(
                "save_config_as",
                (Path(path), self._semantic_revision, self.settings, self.region,
                 self.button, self.success),
                "正在保存配置快照…",
            )
        except ValueError as exc:
            self.status_label.setText(f"配置无效：{exc}")

    def choose_config_to_load(self) -> None:
        if self._profile_action_in_progress():
            return
        if self._import_action_in_progress():
            return
        self._stop_before_profile_dialog("选择要载入的配置")
        current_path = self._current_config_path or getattr(self.store, "config_path", None)
        start_directory = str(current_path.parent) if current_path is not None else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择并载入配置", start_directory, "JSON 配置 (*.json)"
        )
        if not path:
            self.status_label.setText("已取消载入配置选择；本轮保持停止。")
            return
        self._queue_config_load(Path(path))

    def load_config(self, path: Path | None = None) -> None:
        """Load the default configuration or an explicitly selected profile."""
        if self._profile_action_in_progress():
            return
        if self._import_action_in_progress():
            return
        self._stop_before_profile_dialog("载入配置")
        self._queue_config_load(path)

    def _queue_config_load(self, path: Path | None) -> None:
        self._config_loading = True
        self._update_controls()
        self._queue_profile_action(
            "load_config", (Path(path) if path is not None else None,
                            self._semantic_revision), "正在载入配置…"
        )

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
        for widget in widgets:
            widget.blockSignals(False)

    def _apply_loaded_config(self, value: Any, path: Path | None) -> None:
        self._invalidate_semantics()
        self._config_loading = False
        settings, region, button, success = value
        self.engine.stop("配置已载入；需要手动预览或开始")
        self.settings, self.region, self.button, self.success = settings, region, button, success
        self.mode = Mode.REMIND
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.blockSignals(False)
        self._apply_settings_widgets()
        self._install_hotkey()
        # Loading never selects a target or starts a round. A target selected this launch
        # remains explicitly selected, but the loaded templates are only configured for it.
        self._configure_engine("配置已载入；请手动预览并开始")
        if path is None:
            default_path = getattr(self.store, "config_path", None)
            self._set_current_config_path(
                Path(default_path) if default_path is not None else None
            )
        else:
            self._set_current_config_path(path)
        self.status_label.setText("配置已载入；本轮未启用，请检查模板后手动预览并开始。")
        self._update_controls()

    def _handle_result(self, result: _WorkerResult) -> None:
        if result.error:
            if result.action in {"save_config", "save_config_as", "load_config"}:
                if result.request_id != self._pending_profile_request:
                    return
                action = self._pending_profile_action
                self._pending_profile_request = None
                self._pending_profile_action = None
                if action == "load_config":
                    self._config_loading = False
                self.status_label.setText(f"后台操作失败：{result.error}")
                self._update_controls()
                return
            if result.action == "capture" and result.request_id == self._pending_capture_request:
                self._pending_capture_request = None
                self._update_controls()
            if (result.action == "import_template"
                    and result.request_id == self._pending_import_request):
                self._pending_import_request = None
                self._update_controls()
            if result.action == "refresh_targets":
                self._refreshing_targets = False
                self._update_controls()
            if result.action == "start" and result.request_id == self._pending_start_request:
                self._pending_start_request = None
                self.status_label.setText(f"无法开始：{result.error}")
                self._update_controls()
                return
            if result.action != "tick":
                self.status_label.setText(f"后台操作失败：{result.error}")
            return
        value = result.value
        if result.action == "refresh_targets":
            self._refreshing_targets = False
            current = self.identity
            self.target_combo.blockSignals(True)
            self.target_combo.clear()
            self.target_combo.addItem("请选择目标窗口", None)
            own_hwnd = int(self.winId())
            available = [identity for identity in value if identity.hwnd != own_hwnd]
            for identity in available:
                label = (f"{identity.title or '（无标题）'} — {identity.executable} "
                         f"[PID {identity.pid}]")
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
            self._update_controls()
        elif result.action == "capture":
            if result.request_id != self._pending_capture_request:
                return
            self._pending_capture_request = None
            frame, geometry = value
            if frame.identity != self.identity:
                return
            previous = self.geometry
            if previous is not None and previous.template_key != geometry.template_key:
                self.region = Region()
            if self.button is not None and not self.button.compatible(geometry):
                self.button = None
            if self.success is not None and not self.success.compatible(geometry):
                self.success = None
            self._static_frame = frame
            self.geometry = geometry
            self.preview.set_frame(frame)
            self._update_target_info(geometry)
            self.status_label.setText("截图仅保存在内存；请选择按钮模板、搜索区域或成功标志进行框选。")
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
            _revision, session_id = value
            mode_text = "仅提醒，不会点击" if self.mode == Mode.REMIND else "自动确认已由你手动开始"
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
                else:
                    self._config_loading = False
                    self.status_label.setText("载入结果已作废，因为你随后更改了设置。")
                    self._update_controls()
            elif action == "save_config_as":
                path, revision = value
                if revision == self._semantic_revision:
                    self._set_current_config_path(path)
                    self.status_label.setText("配置和模板已另存；本轮保持停止。")
                else:
                    self.status_label.setText(
                        "已保存选择时的配置快照；设置随后已更改，当前配置位置未切换。"
                    )
                self._update_controls()
            else:
                _path, revision = value
                if revision == self._semantic_revision:
                    self.status_label.setText("当前配置和模板已保存。")
                else:
                    self.status_label.setText("已保存配置快照；设置随后已更改。")
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
                    self.target_info.text() + " · 已载入模板与此窗口尺寸或 DPI 不匹配，请重新采集"
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
        snapshot = self.worker.latest_snapshot()
        if snapshot is None:
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
        self.state_label.setText(f"{snapshot.state.value} · {snapshot.reason}")
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

    def _update_controls(self, state: State | None = None) -> None:
        if state is None:
            state = self.engine.snapshot().state
        has_target = self.identity is not None
        compatible = (self.geometry is not None and self.button is not None
                      and self.button.compatible(self.geometry)
                      and (self.success is None or self.success.compatible(self.geometry)))
        ready = (has_target and compatible and not self._config_loading
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
        self.start_button.setEnabled(ready and start_allowed and
                                     self._pending_start_request is None and
                                     state not in {State.MONITORING, State.CONFIRMING,
                                                   State.ATTEMPTED, State.VERIFYING,
                                                   State.PAUSED})
        self.pause_button.setEnabled(self._pending_start_request is not None or
                                     state in {State.MONITORING, State.CONFIRMING,
                                               State.ATTEMPTED, State.VERIFYING,
                                               State.PAUSED})
        self.resume_button.setEnabled(
            self._manual_pause_requested and state == State.PAUSED
        )
        self.stop_button.setEnabled(self._pending_start_request is not None or state not in {
            None, State.DISARMED, State.SUCCESS, State.UNCONFIRMED, State.ERROR
        })
        profile_available = (self._pending_profile_request is None
                             and self._pending_import_request is None)
        self.save_button.setEnabled(profile_available)
        self.load_button.setEnabled(profile_available)
        self.save_as_button.setEnabled(profile_available)
        self.import_button.setEnabled(self.geometry is not None and
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
