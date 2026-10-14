"""Transactional first-run setup dialog.

The dialog owns a temporary draft only. Capture, recognition, and saving are
requested from the main window so those operations stay on its worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .models import Detection, Geometry, Identity, Region, Settings, Template


@dataclass
class OnboardingDraft:
    identity: Identity | None
    geometry: Geometry | None
    settings: Settings
    region: Region
    button: Template | None
    success: Template | None
    frame: Any = None
    confirmed: bool = False
    recognition_text: str = "尚未检查识别；可以继续保存为未确认配置。"
    original_region: Region | None = None
    original_button: Template | None = None
    original_success: Template | None = None


class OnboardingWizard(QDialog):
    """Four-step, cancellable setup flow with an explicit save commit."""

    targetsRequested = Signal()
    targetChanged = Signal(object)
    draftChanged = Signal()
    captureRequested = Signal(object)
    recognitionRequested = Signal(object)
    finishRequested = Signal(object, str)
    loadProfileRequested = Signal()

    def __init__(self, draft: OnboardingDraft, parent: QWidget | None = None):
        super().__init__(parent)
        self.draft = draft
        self._saving = False
        self.setWindowTitle("首次设置")
        self.setMinimumSize(520, 360)

        outer = QVBoxLayout(self)
        self.step_label = QLabel()
        self.step_label.setStyleSheet("font-weight: 700; font-size: 16px")
        outer.addWidget(self.step_label)
        self.pages = QStackedWidget(self)
        pages_scroll = QScrollArea(self)
        pages_scroll.setWidgetResizable(True)
        pages_scroll.setWidget(self.pages)
        outer.addWidget(pages_scroll, 1)

        self._build_target_page()
        self._build_button_page()
        self._build_check_page()
        self._build_save_page()

        controls = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.back_button.clicked.connect(self.back)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        self.next_button = QPushButton("下一步")
        self.next_button.clicked.connect(self.next)
        self.finish_button = QPushButton("保存配置并完成")
        self.finish_button.clicked.connect(self._request_finish)
        controls.addWidget(self.back_button)
        controls.addStretch(1)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.next_button)
        controls.addWidget(self.finish_button)
        outer.addLayout(controls)
        self._show_step(0)

    def _build_target_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("选择这次要配置的游戏窗口。每次启动仍需重新确认目标。"))
        row = QHBoxLayout()
        self.target_combo = QComboBox()
        self.target_combo.addItem("请选择窗口…", None)
        self.target_combo.currentIndexChanged.connect(self._target_changed)
        self.refresh_targets_button = QPushButton("刷新窗口")
        self.refresh_targets_button.clicked.connect(self.targetsRequested.emit)
        row.addWidget(self.target_combo, 1)
        row.addWidget(self.refresh_targets_button)
        layout.addLayout(row)
        self.target_feedback = QLabel("未选择窗口。")
        self.target_feedback.setWordWrap(True)
        layout.addWidget(self.target_feedback)
        self.load_profile_button = QPushButton("载入已有配置…")
        self.load_profile_button.clicked.connect(self.loadProfileRequested.emit)
        layout.addWidget(self.load_profile_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _build_button_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.capture_guidance = QLabel()
        self.capture_guidance.setWordWrap(True)
        layout.addWidget(self.capture_guidance)
        row = QHBoxLayout()
        self.capture_button = QPushButton("获取静态画面")
        self.capture_button.clicked.connect(self._request_capture)
        self.undo_button = QPushButton("撤销本次框选")
        self.undo_button.clicked.connect(self.undo_selection)
        self.advanced_button = QPushButton("显示可选搜索区域 / 成功标志")
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        row.addWidget(self.capture_button)
        row.addWidget(self.undo_button)
        row.addWidget(self.advanced_button)
        layout.addLayout(row)
        self.selection_combo = QComboBox()
        self.selection_combo.addItem("在画面中框出准备 / 接受按钮", "button")
        self.selection_combo.addItem("可选：设置搜索区域", "region")
        self.selection_combo.addItem("可选：设置成功标志", "success")
        self.selection_combo.currentIndexChanged.connect(self._selection_mode_changed)
        self.selection_combo.hide()
        layout.addWidget(self.selection_combo)
        from .ui import ImagePreview

        self.preview = ImagePreview(self)
        self.preview.setMinimumSize(480, 280)
        self.preview.selectionMade.connect(self._selection_made)
        layout.addWidget(self.preview, 1)
        self.button_feedback = QLabel("尚未框选按钮。")
        self.button_feedback.setWordWrap(True)
        layout.addWidget(self.button_feedback)
        self.pages.addWidget(page)

    def _build_check_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("使用与运行时相同的识别逻辑做一次无点击检查。"))
        self.check_button = QPushButton("检查识别（不会点击）")
        self.check_button.clicked.connect(self._request_recognition)
        layout.addWidget(self.check_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.recognition_feedback = QLabel(self.draft.recognition_text)
        self.recognition_feedback.setWordWrap(True)
        layout.addWidget(self.recognition_feedback)
        self.pages.addWidget(page)

    def _build_save_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("命名配置，然后明确保存。完成设置不会开始监控。"))
        self.name_edit = QLineEdit("我的配置")
        self.name_edit.setPlaceholderText("配置名称")
        layout.addWidget(self.name_edit)
        self.save_feedback = QLabel(
            "默认模式为仅提醒。没有成功标志时，只能报告已点击，不能确认结果。"
        )
        self.save_feedback.setWordWrap(True)
        layout.addWidget(self.save_feedback)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def set_targets(self, targets: list[Identity]) -> None:
        selected = self.draft.identity
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem("请选择窗口…", None)
        for number, identity in enumerate(targets, start=1):
            label = (
                f"窗口 {number} · {identity.title or '（无标题）'} · "
                f"{Path(identity.executable).name}"
            )
            self.target_combo.addItem(label, identity)
        index = self.target_combo.findData(selected)
        self.target_combo.setCurrentIndex(index if index > 0 else 0)
        self.target_combo.blockSignals(False)
        self._target_changed(self.target_combo.currentIndex())

    def set_target_geometry(self, identity: Identity, geometry: Geometry) -> None:
        if self.draft.identity != identity:
            return
        self.draft.geometry = geometry
        index = self.target_combo.findData(identity)
        if index >= 0:
            self.target_combo.setItemText(
                index,
                f"窗口 {index} · {identity.title or '（无标题）'} · "
                f"{Path(identity.executable).name} · 客户区 {geometry.width}×{geometry.height}",
            )
        self.target_feedback.setText(
            f"已选：{identity.title or Path(identity.executable).name}。"
            f"客户区 {geometry.width}×{geometry.height}，DPI {geometry.dpi}。"
        )

    def set_capture_guidance(self, backend: str, delay_seconds: float) -> None:
        if backend.casefold() in {"gdi", "foreground", "front"}:
            delay = max(0, round(delay_seconds))
            self.capture_guidance.setText(
                f"当前使用前台兼容捕获。请切回游戏；点击后等待 {delay} 秒再采图。"
            )
        else:
            self.capture_guidance.setText(
                "请在静止画面中框出准备 / 接受按钮。支持直接捕获时无需切回游戏。"
            )

    def set_frame(self, frame: Any) -> None:
        self.draft.frame = frame
        self.draft.geometry = frame.geometry
        self.preview.set_frame(frame)
        self.preview.set_selection_enabled(True)
        self.button_feedback.setText("静态画面已就绪；拖动框选完整按钮。")
        self.capture_button.setEnabled(True)
        self.draftChanged.emit()

    def capture_failed(self, message: str) -> None:
        self.capture_button.setEnabled(True)
        self.button_feedback.setText(f"未更换当前画面或模板：{message}")

    def recognition_completed(self, detection: Detection) -> None:
        if detection.unique is not None:
            self.draft.confirmed = True
            self.draft.recognition_text = "找到一个按钮；仍需你手动开始本轮。"
        elif detection.candidates:
            self.draft.confirmed = False
            self.draft.recognition_text = "找到多个相似区域；请调整按钮框选或搜索区域。"
        else:
            self.draft.confirmed = False
            self.draft.recognition_text = (
                f"暂未找到按钮：{detection.reason or '当前画面未匹配'}。"
                "可以返回调整，或继续保存为识别未确认配置。"
            )
        self.recognition_feedback.setText(self.draft.recognition_text)
        self.check_button.setEnabled(True)

    def save_failed(self, message: str) -> None:
        self._saving = False
        self.save_feedback.setText(f"保存失败，草稿仍保留：{message}")
        self._update_controls()

    def save_succeeded(self, path: Path | None) -> None:
        self.save_feedback.setText(f"配置已保存：{path or '默认配置'}。本轮仍未开始。")
        self.accept()

    def reject(self) -> None:
        if not self._saving:
            super().reject()

    def closeEvent(self, event: Any) -> None:
        if self._saving:
            event.ignore()
            return
        super().closeEvent(event)

    def _target_changed(self, index: int) -> None:
        identity = self.target_combo.itemData(index)
        self.draft.identity = identity if isinstance(identity, Identity) else None
        self.draft.geometry = None
        self.target_feedback.setText(
            f"已选：{self.draft.identity.title or Path(self.draft.identity.executable).name}。"
            "正在检查客户区…" if self.draft.identity is not None else "未选择窗口。"
        )
        self.targetChanged.emit(self.draft.identity)
        self.draftChanged.emit()
        self._update_controls()

    def _request_capture(self) -> None:
        if self.draft.identity is None:
            self.button_feedback.setText("请先选择游戏窗口。")
            return
        self.capture_button.setEnabled(False)
        self.captureRequested.emit(self.draft.identity)

    def _selection_mode_changed(self, index: int) -> None:
        self.preview.set_selection_enabled(self.draft.frame is not None)
        self.preview.set_frame(self.draft.frame)
        del index

    def _selection_made(self, rect: Any) -> None:
        frame = self.draft.frame
        if frame is None:
            return
        try:
            from .recognition import validate_template

            crop = frame.image[rect.y : rect.y + rect.height,
                               rect.x : rect.x + rect.width].copy()
            kind = self.selection_combo.currentData() or "button"
            if kind in {"button", "success"}:
                validate_template(crop)
                template = Template(
                    crop,
                    (frame.geometry.width, frame.geometry.height),
                    frame.geometry.dpi,
                    "按钮" if kind == "button" else "成功标志",
                )
                if kind == "button":
                    self.draft.button = template
                    self.button_feedback.setText("按钮模板已更新；可以无点击检查识别。")
                else:
                    self.draft.success = template
                    self.button_feedback.setText("可选成功标志已更新。")
            else:
                self.draft.region = Region.from_rect(
                    rect, frame.geometry.width, frame.geometry.height
                )
                self.button_feedback.setText("搜索区域已更新。")
            self.draftChanged.emit()
        except (ValueError, IndexError) as exc:
            self.button_feedback.setText(f"框选无效，原模板保留：{exc}")
        self.preview.set_frame(frame)
        self._update_controls()

    def undo_selection(self) -> None:
        self.draft.button = self.draft.original_button
        self.draft.success = self.draft.original_success
        self.draft.region = self.draft.original_region or Region()
        self.button_feedback.setText("已撤销本次框选；请重新框出按钮。")
        if self.draft.frame is not None:
            self.preview.set_frame(self.draft.frame)
        self.draftChanged.emit()
        self._update_controls()

    def _toggle_advanced(self, shown: bool) -> None:
        self.selection_combo.setVisible(shown)

    def _request_recognition(self) -> None:
        if self.draft.identity is None or self.draft.button is None:
            self.recognition_feedback.setText("请先选择窗口并框选按钮。")
            return
        self.check_button.setEnabled(False)
        self.recognition_feedback.setText("正在获取新画面并检查识别；不会发送输入…")
        self.recognitionRequested.emit(self.draft)

    def _request_finish(self) -> None:
        if self.draft.identity is None or self.draft.button is None:
            self.save_feedback.setText("请先选择窗口并框选按钮。")
            return
        name = self.name_edit.text().strip()
        if not name:
            self.save_feedback.setText("请输入配置名称。")
            return
        self._saving = True
        self.save_feedback.setText("正在保存配置和模板…")
        self._update_controls()
        self.finishRequested.emit(self.draft, name)

    def next(self) -> None:
        index = self.pages.currentIndex()
        if index == 0 and (self.draft.identity is None or self.draft.geometry is None):
            self.target_feedback.setText("请先选择并检查一个窗口。")
            return
        if index == 1 and self.draft.button is None:
            self.button_feedback.setText("请在静态画面中框出按钮；旧模板仍保留在草稿中。")
            return
        self._show_step(min(3, index + 1))

    def back(self) -> None:
        if not self._saving:
            self._show_step(max(0, self.pages.currentIndex() - 1))

    def _show_step(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.step_label.setText(f"第 {index + 1} 步，共 4 步")
        self.back_button.setVisible(index > 0)
        self.next_button.setVisible(index < 3)
        self.finish_button.setVisible(index == 3)
        if index == 1:
            self._update_controls()

    def _update_controls(self) -> None:
        self.next_button.setEnabled(not self._saving)
        self.back_button.setEnabled(not self._saving)
        self.cancel_button.setEnabled(not self._saving)
        self.finish_button.setEnabled(not self._saving)
        self.undo_button.setEnabled(self.draft.button is not None or self.draft.success is not None)
        self.capture_button.setEnabled(self.draft.identity is not None and not self._saving)
        self.check_button.setEnabled(self.draft.button is not None and not self._saving)
        self.load_profile_button.setEnabled(not self._saving)
        self.refresh_targets_button.setEnabled(not self._saving)
