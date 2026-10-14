"""Small dialogs for naming and choosing saved profiles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import ProfileMetadata


class ProfileMetadataDialog(QDialog):
    """Edit display-only profile labels before a snapshot is saved."""

    def __init__(
        self,
        metadata: ProfileMetadata,
        title: str,
        save_description: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        explanation = QLabel(
            "游戏名称只是方便识别配置的标签，不会选择或绑定游戏窗口。"
        )
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(explanation)

        form = QFormLayout()
        self.name_edit = QLineEdit(metadata.name)
        self.name_edit.setMaxLength(80)
        self.name_edit.setPlaceholderText("配置名称（必填）")
        self.game_edit = QLineEdit(metadata.game)
        self.game_edit.setMaxLength(80)
        self.game_edit.setPlaceholderText("游戏名称（可选）")
        form.addRow("配置名称", self.name_edit)
        form.addRow("对应游戏", self.game_edit)
        layout.addLayout(form)

        description = QLabel(save_description)
        description.setWordWrap(True)
        description.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(description)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b3261e")
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("保存名称和当前配置")
        self.save_button.clicked.connect(self._accept_metadata)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def metadata(self) -> ProfileMetadata:
        return ProfileMetadata(self.name_edit.text().strip(), self.game_edit.text().strip())

    def _accept_metadata(self) -> None:
        metadata = self.metadata()
        if not metadata.name:
            self.error_label.setText("请输入配置名称。")
            return
        try:
            metadata.validate()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.accept()


@dataclass(frozen=True)
class ProfileChoice:
    path: Path
    metadata: ProfileMetadata
    label: str


class ProfileSelectionDialog(QDialog):
    """Choose a known profile or request browsing for another JSON file."""

    BROWSE_SENTINEL = object()

    def __init__(self, profiles: list[ProfileChoice], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("载入配置")
        self.setMinimumSize(460, 320)
        self.choice: ProfileChoice | None = None
        self.browse_requested = False

        layout = QVBoxLayout(self)
        heading = QLabel("选择已发现的配置；也可以浏览其他配置文件。")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.profile_table = QTableWidget(0, 3)
        self.profile_table.setHorizontalHeaderLabels(["配置名称", "对应游戏", "文件"])
        self.profile_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.profile_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.profile_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.profile_table.setWordWrap(True)
        header = self.profile_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.profile_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.profile_table.setColumnWidth(0, 150)
        self.profile_table.setColumnWidth(1, 150)
        self.profile_table.setColumnWidth(2, 160)
        for row, profile in enumerate(profiles):
            self.profile_table.insertRow(row)
            cells = (profile.metadata.name, profile.metadata.game, profile.path.name)
            for column, value in enumerate(cells):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, profile)
                item.setToolTip(str(profile.path) if column == 2 else value)
                self.profile_table.setItem(row, column, item)
        self.profile_table.resizeRowsToContents()
        self.profile_table.currentCellChanged.connect(self._selection_changed)
        self.profile_table.cellDoubleClicked.connect(self._choose_current)
        layout.addWidget(self.profile_table, 1)
        self.selected_path_label = QLabel("请选择配置。")
        self.selected_path_label.setWordWrap(True)
        self.selected_path_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.selected_path_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        self.browse_button = QPushButton("浏览其他配置文件…")
        self.browse_button.clicked.connect(self._browse)
        self.load_button = QPushButton("载入所选配置")
        self.load_button.clicked.connect(self._choose_current)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.browse_button)
        buttons.addWidget(self.load_button)
        layout.addLayout(buttons)
        self.load_button.setEnabled(bool(profiles))
        if profiles:
            self.profile_table.setCurrentCell(0, 0)
            self._selection_changed(0, 0, -1, -1)

    def _selection_changed(self, row: int, column: int, _old_row: int, _old_column: int) -> None:
        if row < 0:
            self.selected_path_label.setText("请选择配置。")
            return
        item = self.profile_table.item(row, 0)
        if item is None:
            return
        choice = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(choice, ProfileChoice):
            self.selected_path_label.setText(f"所选配置完整路径：{choice.path}")
            self.selected_path_label.setToolTip(str(choice.path))

    def _choose_current(self, *_args: object) -> None:
        row = self.profile_table.currentRow()
        item = self.profile_table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        self.choice = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(self.choice, ProfileChoice):
            self.choice = None
            return
        self.accept()

    def _browse(self) -> None:
        self.browse_requested = True
        self.accept()
