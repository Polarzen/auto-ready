"""Read-only previews for saved template crops."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .models import Geometry, Template


def _template_pixmap(template: Template, size: tuple[int, int]) -> QPixmap:
    rgb = np.ascontiguousarray(template.image[:, :, ::-1])
    height, width = rgb.shape[:2]
    image = QImage(
        rgb.data, width, height, rgb.strides[0], QImage.Format.Format_RGB888
    ).copy()
    return QPixmap.fromImage(image).scaled(
        size[0], size[1], Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class TemplateThumbnail(QLabel):
    """Small, non-interactive thumbnail of one template crop."""

    def __init__(self, width: int = 132, height: int = 72, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(width, height)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "QLabel { background: #171b22; color: #aeb8c6; border: 1px solid #596273; }"
        )
        self.set_template(None)

    def set_template(self, template: Template | None) -> None:
        self.setPixmap(QPixmap())
        if template is None:
            self.setText("未设置")
            self.setToolTip("没有模板")
            return
        self.setText("")
        self.setPixmap(_template_pixmap(template, (self.width() - 8, self.height() - 8)))
        width, height = template.reference_size
        self.setToolTip(
            f"{template.name} · 参考客户区 {width}×{height} · DPI {template.dpi}"
        )


class _TemplateCard(QGroupBox):
    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(title, parent)
        layout = QVBoxLayout(self)
        self.image = QLabel("未设置")
        self.image.setMinimumSize(260, 170)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setStyleSheet(
            "QLabel { background: #171b22; color: #aeb8c6; border: 1px solid #596273; }"
        )
        self.details = QLabel("没有模板")
        self.details.setWordWrap(True)
        layout.addWidget(self.image, 1)
        layout.addWidget(self.details)

    def set_template(self, template: Template | None) -> None:
        self.image.setPixmap(QPixmap())
        if template is None:
            self.image.setText("未设置")
            self.details.setText("没有保存模板")
            return
        self.image.setText("")
        self.image.setPixmap(_template_pixmap(template, (500, 300)))
        width, height = template.reference_size
        self.details.setText(
            f"模板：{template.name}；裁剪尺寸 {template.image.shape[1]}×"
            f"{template.image.shape[0]} 像素；参考客户区 {width}×{height}；DPI {template.dpi}。"
        )


class TemplatePreviewDialog(QDialog):
    """Static view of the current in-memory crop images and their geometry."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("当前配置的模板")
        self.resize(700, 430)
        layout = QVBoxLayout(self)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        cards = QHBoxLayout()
        self.button_card = _TemplateCard("按钮模板", self)
        self.success_card = _TemplateCard("成功标志（可选）", self)
        cards.addWidget(self.button_card, 1)
        cards.addWidget(self.success_card, 1)
        layout.addLayout(cards, 1)
        self.compatibility_label = QLabel()
        self.compatibility_label.setWordWrap(True)
        layout.addWidget(self.compatibility_label)

    def set_templates(
        self,
        button: Template | None,
        success: Template | None,
        geometry: Geometry | None,
        *,
        loaded: bool,
        dirty: bool,
        load_error: str = "",
    ) -> None:
        self.button_card.set_template(button)
        self.success_card.set_template(success)
        if load_error and not loaded:
            self.state_label.setText(f"当前配置尚未载入：{load_error}")
        elif not loaded:
            self.state_label.setText("当前配置尚未载入；请载入配置或新建配置。")
        else:
            self.state_label.setText(
                "当前内存内容 · 有未保存修改" if dirty else "当前内存内容 · 已保存"
            )
        if button is None:
            self.compatibility_label.setText("尚未设置按钮模板。")
        elif geometry is None:
            self.compatibility_label.setText(
                "模板已载入，无需重复框选；请选择本次目标窗口。"
            )
        elif not button.compatible(geometry) or (
            success is not None and not success.compatible(geometry)
        ):
            self.compatibility_label.setText(
                "模板保留；恢复窗口尺寸/DPI或修改模板。"
            )
        else:
            self.compatibility_label.setText(
                "当前目标窗口尺寸和 DPI 与模板匹配；无需重复框选。"
            )
