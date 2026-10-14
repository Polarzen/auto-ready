"""A standalone, safe mock ready screen for trying recognition workflows."""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

_MARKER_COLORS = (
    QColor("#ff2424"),  # top-left
    QColor("#16e070"),  # top-right
    QColor("#287dff"),  # bottom-left
    QColor("#ffdb28"),  # bottom-right
)
_SW_SHOWMINNOACTIVE = 7
_SW_SHOWNOACTIVATE = 4


def _show_window(hwnd: int, command: int) -> None:
    """Apply a nonactivating native show command to one of our own windows."""
    if sys.platform != "win32":
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    show_window = user32.ShowWindow
    show_window.argtypes = [ctypes.c_void_p, ctypes.c_int]
    show_window.restype = ctypes.c_int
    show_window(ctypes.c_void_p(int(hwnd)), command)


class CaptureMarkerWidget(QWidget):
    """Visible, changing source marker shared by the simulator and WGC probe."""

    def __init__(self, parent: QWidget | None = None, *, frozen: bool = False):
        super().__init__(parent)
        self.setObjectName("captureMarker")
        self.setMinimumHeight(76)
        self._counter = 0
        self._frozen = frozen
        self._timer = QTimer(self)
        self._timer.setInterval(160)
        self._timer.timeout.connect(self._advance)
        if not frozen:
            self._timer.start()

    @property
    def counter(self) -> int:
        return self._counter

    def set_frozen(self, frozen: bool) -> None:
        self._frozen = frozen
        if frozen:
            self._timer.stop()
        else:
            self._timer.start()

    def _advance(self) -> None:
        if self._frozen:
            return
        self._counter = (self._counter + 1) & 0xFFFF
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual method spelling
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        bounds = self.rect()
        painter.fillRect(bounds, QColor("#101820"))

        side = max(8, int(min(bounds.width(), bounds.height()) * 0.42))
        margin_x = max(3, int(bounds.width() * 0.025))
        margin_y = max(3, int(bounds.height() * 0.08))
        corner_rects = (
            (margin_x, margin_y),
            (bounds.width() - margin_x - side, margin_y),
            (margin_x, bounds.height() - margin_y - side),
            (bounds.width() - margin_x - side, bounds.height() - margin_y - side),
        )
        for color, (x, y) in zip(_MARKER_COLORS, corner_rects, strict=True):
            painter.fillRect(x, y, side, side, color)

        cell_w = max(2, int(bounds.width() * 0.031))
        gap = max(1, int(bounds.width() * 0.009))
        cell_h = max(6, int(bounds.height() * 0.25))
        total_w = 16 * cell_w + 15 * gap
        start_x = (bounds.width() - total_w) // 2
        start_y = (bounds.height() - cell_h) // 2
        for index in range(16):
            bit = (self._counter >> (15 - index)) & 1
            color = QColor("#f4f1d0") if bit else QColor("#28333d")
            painter.fillRect(start_x + index * (cell_w + gap), start_y, cell_w, cell_h, color)

        if bounds.height() >= 90:
            painter.setPen(QColor("#e8edf2"))
            painter.drawText(
                bounds.adjusted(0, int(bounds.height() * 0.28), 0, 0),
                Qt.AlignmentFlag.AlignCenter,
                f"CAPTURE SOURCE  {self._counter:05d}",
            )
        pen = QPen(QColor("#ff22c8"), max(2, int(min(bounds.width(), bounds.height()) * 0.025)))
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(bounds.adjusted(2, 2, -3, -3))


class CaptureProbeWindow(QWidget):
    """Own, non-activating marker target used only by the capture acceptance probe."""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setObjectName("captureProbeWindow")
        self.setWindowTitle("Auto Ready Capture Check Marker")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(640, 360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.marker = CaptureMarkerWidget(self)
        layout.addWidget(self.marker)

    def minimize_without_activation(self) -> None:
        """Minimize this own-window target without making it foreground."""
        _show_window(self.winId(), _SW_SHOWMINNOACTIVE)
        if sys.platform != "win32":
            self.setWindowState(Qt.WindowState.WindowMinimized)
        self.update()

    def restore_without_activation(self) -> None:
        """Restore this own-window target without making it foreground."""
        _show_window(self.winId(), _SW_SHOWNOACTIVATE)
        if sys.platform != "win32":
            self.setWindowState(Qt.WindowState.WindowNoState)
        self.update()


class CaptureOccluderWindow(QWidget):
    """Magenta window placed over only the probe's own marker during one phase."""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setObjectName("captureProbeOccluder")
        self.setWindowTitle("Auto Ready Capture Check Occluder")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual method spelling
        del event
        QPainter(self).fillRect(self.rect(), QColor("#ff00ff"))


class ReadyButton(QPushButton):
    """A repeatable, shaded mock game button with a distinct visible edge."""

    def __init__(
        self,
        text: str,
        *,
        distractor: bool = False,
        parent: QWidget | None = None,
        observer: Callable[..., Any] | None = None,
        button_id: str = "ready",
        evidence_source: str = "auto",
    ):
        super().__init__(text, parent)
        self.distractor = distractor
        self.button_id = button_id
        self._observer = observer
        self._evidence_source = evidence_source
        self._last_release_source: str | None = None
        self._last_release_spontaneous = False
        self.setObjectName("distractorButton" if distractor else "readyButton")
        self.setFixedSize(190, 68)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(text)
        self.clicked.connect(self._record_clicked)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt virtual method spelling
        spontaneous = bool(event.spontaneous())
        self._record_mouse_event("mouse_press", spontaneous)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt virtual method spelling
        spontaneous = bool(event.spontaneous())
        self._last_release_source = self._source(spontaneous)
        self._last_release_spontaneous = spontaneous
        self._record_mouse_event("mouse_release", spontaneous)
        super().mouseReleaseEvent(event)
        self._last_release_source = None
        self._last_release_spontaneous = False

    def _source(self, spontaneous: bool) -> str:
        if self._evidence_source != "auto":
            return self._evidence_source
        return "qt_spontaneous" if spontaneous else "qt_synthetic"

    def _record_mouse_event(self, phase: str, spontaneous: bool) -> None:
        self._emit_evidence(
            f"simulator_button_{phase}",
            phase=phase,
            button_id=self.button_id,
            spontaneous=spontaneous,
            source=self._source(spontaneous),
        )

    def _record_clicked(self, checked: bool = False) -> None:
        source = self._last_release_source or "qt_programmatic"
        self._emit_evidence(
            "simulator_button_clicked",
            button_id=self.button_id,
            checked=bool(checked),
            spontaneous=self._last_release_spontaneous,
            source=source,
        )

    def _emit_evidence(self, event: str, **fields: Any) -> None:
        if self._observer is not None:
            try:
                self._observer(event, **fields)
            except Exception:
                # Evidence observers must never change the mock's click behavior.
                pass

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt virtual method spelling
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = self.rect().adjusted(4, 4, -4, -7)
        if self.distractor:
            top, middle, bottom = QColor("#f3be65"), QColor("#d78731"), QColor("#99511f")
            edge = QColor("#713b20")
        else:
            top, middle, bottom = QColor("#b9f27d"), QColor("#61b947"), QColor("#287c3e")
            edge = QColor("#20532e")

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 75))
        painter.drawRoundedRect(bounds.translated(0, 4), 15, 15)
        gradient = QLinearGradient(bounds.topLeft(), bounds.bottomLeft())
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(0.48, middle)
        gradient.setColorAt(1.0, bottom)
        painter.setBrush(gradient)
        painter.setPen(QPen(edge, 2))
        painter.drawRoundedRect(bounds, 15, 15)

        shine = bounds.adjusted(6, 5, -6, -bounds.height() // 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 65))
        painter.drawRoundedRect(shine, 10, 10)

        painter.setPen(QColor("#16321d") if not self.distractor else QColor("#40260d"))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(17)
        painter.setFont(font)
        painter.drawText(bounds, Qt.AlignmentFlag.AlignCenter, self.text())


class SimulatorWindow(QMainWindow):
    """Movable and resizable mock ready window with manually controllable states."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        observer: Callable[..., Any] | None = None,
        evidence_source: str = "auto",
    ):
        super().__init__(parent)
        self._evidence_observer = observer
        self._evidence_source = evidence_source
        self.setWindowTitle("Auto Ready 模拟器")
        self.resize(720, 530)
        self._state = "reset"
        self._make_ui()
        self.ready_timer = QTimer(self)
        self.ready_timer.setSingleShot(True)
        self.ready_timer.timeout.connect(self._show_ready)
        self.expiry_timer = QTimer(self)
        self.expiry_timer.setSingleShot(True)
        self.expiry_timer.timeout.connect(self._expire_ready)
        self.reset()
        self.start_round()

    @property
    def state(self) -> str:
        return self._state

    def _make_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)

        self.capture_marker = CaptureMarkerWidget(root)
        layout.addWidget(self.capture_marker)

        heading = QLabel("模拟游戏准备画面")
        heading.setObjectName("simulatorHeading")
        heading.setStyleSheet("font-size: 22px; font-weight: 700")
        layout.addWidget(heading)
        help_text = QLabel("可移动、缩放此窗口；控件用于重置就绪状态与测试相似按钮。")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        controls = QWidget(root)
        form = QFormLayout(controls)
        self.scenario_combo = QComboBox(controls)
        self.scenario_combo.setObjectName("scenarioCombo")
        self.scenario_combo.addItem("点击后模拟成功", "success")
        self.scenario_combo.addItem("点击后没有成功", "no_success")
        self.scenario_combo.addItem("准备按钮自动过期", "expiry")
        form.addRow("结果场景", self.scenario_combo)

        self.delay_spin = QSpinBox(controls)
        self.delay_spin.setObjectName("readyDelaySpin")
        self.delay_spin.setRange(0, 120)
        self.delay_spin.setSuffix(" 秒")
        self.delay_spin.setValue(2)
        form.addRow("就绪延迟", self.delay_spin)

        self.expiry_spin = QSpinBox(controls)
        self.expiry_spin.setObjectName("expiryDelaySpin")
        self.expiry_spin.setRange(1, 300)
        self.expiry_spin.setSuffix(" 秒")
        self.expiry_spin.setValue(15)
        form.addRow("过期时间", self.expiry_spin)

        self.duplicate_check = QCheckBox("显示一个完全相同的重复按钮", controls)
        self.duplicate_check.setObjectName("duplicateCheck")
        form.addRow("重复目标", self.duplicate_check)
        self.distractor_check = QCheckBox("显示一个相似干扰按钮", controls)
        self.distractor_check.setObjectName("distractorCheck")
        form.addRow("相似干扰", self.distractor_check)
        layout.addWidget(controls)

        self.state_label = QLabel()
        self.state_label.setObjectName("simulatorState")
        self.state_label.setStyleSheet("font-size: 15px; font-weight: 600")
        layout.addWidget(self.state_label)

        self.target_panel = QWidget(root)
        self.target_panel.setObjectName("targetPanel")
        self.target_panel.setMinimumHeight(180)
        self.target_panel.setStyleSheet(
            "QWidget#targetPanel { background: #263443; border: 1px solid #758292; "
            "border-radius: 8px; }"
        )
        self.target_layout = QHBoxLayout(self.target_panel)
        self.target_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ready_button = ReadyButton(
            "准备就绪",
            parent=self.target_panel,
            observer=self._evidence_observer,
            button_id="ready",
            evidence_source=self._evidence_source,
        )
        self.ready_button.clicked.connect(self._ready_clicked)
        self.duplicate_button = ReadyButton(
            "准备就绪",
            parent=self.target_panel,
            observer=self._evidence_observer,
            button_id="duplicate",
            evidence_source=self._evidence_source,
        )
        self.duplicate_button.clicked.connect(self._ready_clicked)
        self.distractor_button = ReadyButton(
            "即将准备",
            distractor=True,
            parent=self.target_panel,
            observer=self._evidence_observer,
            button_id="distractor",
            evidence_source=self._evidence_source,
        )
        self.distractor_button.clicked.connect(self._distractor_clicked)
        for button in (self.ready_button, self.duplicate_button, self.distractor_button):
            self.target_layout.addWidget(button)
            button.hide()
        layout.addWidget(self.target_panel, 1)

        actions = QHBoxLayout()
        self.start_button = QPushButton("开始倒计时", root)
        self.start_button.setObjectName("startRoundButton")
        self.start_button.clicked.connect(self.start_round)
        self.manual_ready_button = QPushButton("手动显示就绪", root)
        self.manual_ready_button.setObjectName("manualReadyButton")
        self.manual_ready_button.clicked.connect(self.manual_ready)
        self.reset_button = QPushButton("重置", root)
        self.reset_button.setObjectName("resetButton")
        self.reset_button.clicked.connect(self.reset)
        actions.addWidget(self.start_button)
        actions.addWidget(self.manual_ready_button)
        actions.addWidget(self.reset_button)
        layout.addLayout(actions)

        self.setCentralWidget(root)
        self.duplicate_check.toggled.connect(self._refresh_buttons)
        self.distractor_check.toggled.connect(self._refresh_buttons)
        self.scenario_combo.currentIndexChanged.connect(self._scenario_changed)

    def start_round(self) -> None:
        """Reset the mock round and show ready after the configured delay."""
        self._stop_timers()
        self._hide_targets()
        self._set_state("waiting", "等待就绪")
        delay_ms = self.delay_spin.value() * 1000
        if delay_ms == 0:
            self._show_ready()
        else:
            self.ready_timer.start(delay_ms)

    def manual_ready(self) -> None:
        """Show ready immediately, without changing the selected result scenario."""
        self.ready_timer.stop()
        self._show_ready()

    def reset(self) -> None:
        """Clear buttons and stop all pending ready or expiry timers."""
        self._stop_timers()
        self._hide_targets()
        self._set_state("reset", "已重置；可手动显示就绪或开始倒计时")

    def _show_ready(self) -> None:
        self._set_state("ready", "准备按钮已显示")
        self._refresh_buttons()
        if self.scenario_combo.currentData() == "expiry":
            self.expiry_timer.start(self.expiry_spin.value() * 1000)

    def _refresh_buttons(self) -> None:
        ready = self._state == "ready"
        self.ready_button.setVisible(ready)
        self.duplicate_button.setVisible(ready and self.duplicate_check.isChecked())
        self.distractor_button.setVisible(ready and self.distractor_check.isChecked())

    def _ready_clicked(self) -> None:
        scenario = self.scenario_combo.currentData()
        if scenario == "success":
            self.expiry_timer.stop()
            self._set_state("success", "模拟成功：准备按钮点击后进入下一阶段")
            self._hide_targets()
        elif scenario == "no_success":
            self._set_state("no_success", "模拟未成功：按钮仍显示，可重复测试")
        else:
            self._set_state("ready", "点击已模拟；准备按钮会在倒计时结束时过期")

    def _distractor_clicked(self) -> None:
        self.state_label.setText("相似干扰按钮已点击；就绪状态保持不变")

    def _scenario_changed(self) -> None:
        self.expiry_timer.stop()
        if self._state == "ready" and self.scenario_combo.currentData() == "expiry":
            self.expiry_timer.start(self.expiry_spin.value() * 1000)

    def _expire_ready(self) -> None:
        if self._state == "ready":
            self._hide_targets()
            self._set_state("expired", "准备按钮已过期")

    def _hide_targets(self) -> None:
        for button in (self.ready_button, self.duplicate_button, self.distractor_button):
            button.hide()

    def _stop_timers(self) -> None:
        if hasattr(self, "ready_timer"):
            self.ready_timer.stop()
        if hasattr(self, "expiry_timer"):
            self.expiry_timer.stop()

    def _set_state(self, state: str, message: str) -> None:
        self._state = state
        self.state_label.setText(message)
        self.state_label.setProperty("state", state)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = SimulatorWindow()
    window.show()
    return app.exec()


def run() -> int:
    return main()


if __name__ == "__main__":
    raise SystemExit(main())
