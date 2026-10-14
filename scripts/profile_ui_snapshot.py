"""Capture focused profile naming and selection widgets using Qt offscreen."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

os.environ["QT_QPA_PLATFORM"] = "offscreen"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import ui_snapshot as shared  # noqa: E402
from PySide6.QtCore import QModelIndex, Qt  # noqa: E402
from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QListWidget,
    QWidget,
)

from auto_ready.config import ConfigStore, ProfileMetadata  # noqa: E402
from auto_ready.models import Region, Settings  # noqa: E402
from auto_ready.onboarding import OnboardingDraft, OnboardingWizard  # noqa: E402
from auto_ready.profile_dialogs import (  # noqa: E402
    ProfileChoice,
    ProfileMetadataDialog,
    ProfileSelectionDialog,
)
from auto_ready.ui import MainWindow, close_and_wait  # noqa: E402

SCALES = ("1.0", "1.5", "2.0")
MAIN_SIZES = ((640, 420), (760, 560))
FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")
OUTPUT_DEFAULT = ROOT / "artifacts" / "profiles-0.4.1" / "ui-qa"

LONG_NAME = "星河远征配置名称视觉验收专用长标签重复名称重复名称"
LONG_GAME = "云海边境与苍穹竞技场中文长游戏名称视觉验收"
DUPLICATE_NAME = "重复名称"
DUPLICATE_GAME = "同一游戏标签"


def _source_hashes() -> dict[str, str]:
    names = (
        "src/auto_ready/ui.py",
        "src/auto_ready/onboarding.py",
        "src/auto_ready/profile_dialogs.py",
        "src/auto_ready/config.py",
    )
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in names
    }


def _visible_attrs(root: Any, attrs: tuple[str, ...]) -> dict[str, QWidget]:
    result: dict[str, QWidget] = {}
    for name in attrs:
        widget = getattr(root, name, None)
        if isinstance(widget, QWidget) and widget.isVisibleTo(root):
            result[name.replace("_", " ")] = widget
    return result


def _capture_dialog(
    dialog: QWidget,
    output_dir: Path,
    filename: str,
    *,
    scale: str,
    scenario: str,
    size: tuple[int, int],
    controls: dict[str, QWidget],
    details: dict[str, Any] | None = None,
    scroll: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = shared._capture(
        dialog,
        output_dir,
        filename,
        scale=scale,
        scenario=scenario,
        state="synthetic-profile-fixture",
        requested_size=size,
        required_controls=controls,
        scroll=scroll,
    )
    if details:
        result["fixture_details"] = details
    return result


def _profile_fixtures(root: Path) -> tuple[ProfileMetadata, list[ProfileChoice]]:
    current = ProfileMetadata(LONG_NAME, LONG_GAME)
    duplicate = ProfileMetadata(DUPLICATE_NAME, DUPLICATE_GAME)
    choices: list[ProfileChoice] = []
    fixture_rows = (
        (duplicate, "重复-配置-A.json"),
        (duplicate, "重复-配置-B.json"),
        (ProfileMetadata("云端记忆·正式版", "星海远征"), "正式版-星海.json"),
        (ProfileMetadata(LONG_NAME, LONG_GAME), "长中文名称-云海.json"),
    )
    for index, (metadata, filename) in enumerate(fixture_rows):
        path = root / "profiles" / f"{index + 1}" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic profile fixture\n", encoding="utf-8")
        choices.append(ProfileChoice(path=path, metadata=metadata, label=(
            f"{metadata.name} · {metadata.game} · {path}"
        )))
    return current, choices


def _capture_main(
    app: QApplication,
    output: Path,
    scale: str,
    entries: list[dict[str, Any]],
    size: tuple[int, int],
    current: ProfileMetadata,
) -> None:
    with tempfile.TemporaryDirectory(prefix="auto-ready-profile-ui-main-") as directory:
        store = ConfigStore(Path(directory))
        current_path = Path(directory) / "named" / "当前-配置.json"
        store.save_to(current_path, Settings(), Region(), None, None, metadata=current)
        window = MainWindow(
            backend=shared.FakeBackend(),
            input_adapter=shared.FakeInput(),
            store=store,
            hotkey_factory=shared.FakeHotkey,
            notifier=lambda *_args: None,
            capture_delay=0,
        )
        metadata_setter = getattr(window, "_set_current_profile_metadata", None)
        if callable(metadata_setter):
            metadata_setter(current)
        path_setter = getattr(window, "_set_current_config_path", None)
        if callable(path_setter):
            path_setter(current_path)
        window.resize(*size)
        window.show()
        shared._pump(app, 0.35)
        controls = _visible_attrs(window, (
            "profile_combo",
            "profile_status_label",
            "current_config_label",
            "rename_profile_button",
            "load_profile_dashboard_button",
            "save_as_dashboard_button",
        ))
        if "profile_combo" in controls:
            combo = controls["profile combo"]
            current_index = combo.findData(current_path)
            if current_index >= 0:
                combo.setCurrentIndex(current_index)
        if not controls:
            raise RuntimeError("main window has no visible profile identity widgets")
        identity = getattr(window, "_current_profile_metadata", current)
        path = getattr(window, "_current_config_path", current_path)
        details = {
            "current_profile_metadata": {
                "name": getattr(identity, "name", None),
                "game": getattr(identity, "game", None),
            },
            "current_profile_path": str(path) if path is not None else None,
            "profile_combo_items": [
                {
                    "text": controls["profile combo"].itemText(index),
                    "path_data": str(controls["profile combo"].itemData(index))
                    if controls["profile combo"].itemData(index) is not None else None,
                }
                for index in range(controls["profile combo"].count())
            ] if "profile combo" in controls else [],
        }
        entries.append(_capture_dialog(
            window,
            output,
            f"main/{scale.replace('.', '')}/{size[0]}x{size[1]}.png",
            scale=scale,
            scenario=f"main-profile-{size[0]}x{size[1]}",
            size=size,
            controls=controls,
            details=details,
        ))
        close_and_wait(window)


def _capture_rename(
    app: QApplication,
    output: Path,
    scale: str,
    entries: list[dict[str, Any]],
) -> None:
    metadata = ProfileMetadata(LONG_NAME, LONG_GAME)
    dialog = ProfileMetadataDialog(
        metadata,
        "重命名配置",
        "保存后会将名称、游戏标签和当前配置一起写入当前配置文件。",
    )
    dialog.resize(560, 300)
    dialog.show()
    shared._pump(app)
    controls = _visible_attrs(dialog, (
        "name_edit", "game_edit", "error_label", "cancel_button", "save_button",
    ))
    entries.append(_capture_dialog(
        dialog,
        output,
        f"rename/{scale.replace('.', '')}.png",
        scale=scale,
        scenario="profile-rename-dialog",
        size=(560, 300),
        controls=controls,
        details={"fixture_metadata": {"name": metadata.name, "game": metadata.game}},
    ))
    dialog.reject()


def _capture_chooser(
    app: QApplication,
    output: Path,
    scale: str,
    entries: list[dict[str, Any]],
    choices: list[ProfileChoice],
) -> None:
    dialog = ProfileSelectionDialog(choices)
    size = (720, 470)
    dialog.resize(*size)
    dialog.show()
    shared._pump(app)
    controls = _visible_attrs(dialog, (
        "profile_table", "profile_list", "selected_path_label", "cancel_button",
        "browse_button", "load_button",
    ))
    profile_view = getattr(dialog, "profile_table", None)
    if not isinstance(profile_view, QAbstractItemView):
        profile_view = getattr(dialog, "profile_list", None)
    if not isinstance(profile_view, QAbstractItemView):
        views = [
            view for view in dialog.findChildren(QAbstractItemView)
            if view.isVisibleTo(dialog)
        ]
        profile_view = max(views, key=lambda view: view.width() * view.height(), default=None)
    if isinstance(profile_view, QWidget) and not any(
        widget is profile_view for widget in controls.values()
    ):
        controls["profile entries"] = profile_view
    if profile_view is None:
        raise RuntimeError("profile chooser has no recognized profile list/table")
    path_bindings: list[dict[str, Any]] = []
    for row, choice in enumerate(choices):
        path_bindings.append({
            "row": row,
            "name": choice.metadata.name,
            "game": choice.metadata.game,
            "path": str(choice.path),
        })
    actual_rows: list[dict[str, Any]] = []
    if isinstance(profile_view, QListWidget):
        for row in range(profile_view.count()):
            item = profile_view.item(row)
            choice = item.data(Qt.ItemDataRole.UserRole)
            actual_rows.append({
                "row": row,
                "text": item.text(),
                "path": str(choice.path) if isinstance(choice, ProfileChoice) else None,
                "name": choice.metadata.name if isinstance(choice, ProfileChoice) else None,
                "game": choice.metadata.game if isinstance(choice, ProfileChoice) else None,
                "tooltip": item.toolTip(),
            })
    model = profile_view.model()
    table_rows: int | None = None
    table_columns: int | None = None
    if not isinstance(profile_view, QListWidget) and model is not None:
        row_count = getattr(profile_view, "rowCount", None)
        column_count = getattr(profile_view, "columnCount", None)
        table_rows = row_count() if callable(row_count) else model.rowCount(QModelIndex())
        table_columns = (
            column_count() if callable(column_count)
            else model.columnCount(QModelIndex())
        )
        for row in range(table_rows):
            cells: list[dict[str, Any]] = []
            for column in range(table_columns):
                index = model.index(row, column)
                text = model.data(index, Qt.ItemDataRole.DisplayRole)
                tooltip = model.data(index, Qt.ItemDataRole.ToolTipRole)
                value = model.data(index, Qt.ItemDataRole.UserRole)
                cells.append({
                    "text": str(text) if text is not None else None,
                    "tooltip": str(tooltip) if tooltip is not None else None,
                    "user_data": str(value) if value is not None else None,
                })
            actual_rows.append({"row": row, "cells": cells})
    profile_table = getattr(dialog, "profile_table", None)
    if profile_table is profile_view and callable(getattr(profile_table, "item", None)):
        for row, actual_row in enumerate(actual_rows):
            if "cells" not in actual_row:
                continue
            item = profile_table.item(row, 0)
            choice = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if isinstance(choice, ProfileChoice):
                actual_row["profile_identity"] = {
                    "name": choice.metadata.name,
                    "game": choice.metadata.game,
                    "path": str(choice.path),
                }
    entries.append(_capture_dialog(
        dialog,
        output,
        f"chooser/{scale.replace('.', '')}.png",
        scale=scale,
        scenario="known-profile-chooser-duplicates-and-long-labels",
        size=size,
        controls=controls,
        details={
            "synthetic_profile_path_bindings": path_bindings,
            "actual_chooser_rows": actual_rows,
            "actual_headers": [
                str(profile_view.horizontalHeaderItem(index).text())
                if callable(getattr(profile_view, "horizontalHeaderItem", None))
                and profile_view.horizontalHeaderItem(index) is not None
                else str(model.headerData(
                    index, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
                )) if model.headerData(
                    index, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
                ) is not None else None
                for index in range(table_columns or 0)
            ],
        },
    ))
    dialog.reject()


def _capture_wizard(
    app: QApplication,
    output: Path,
    scale: str,
    entries: list[dict[str, Any]],
) -> None:
    backend = shared.FakeBackend()
    draft = OnboardingDraft(
        identity=backend.identity,
        geometry=backend.geometry,
        settings=Settings(),
        region=Region(),
        button=None,
        success=None,
        profile_name=LONG_NAME,
        game_label=LONG_GAME,
    )
    wizard = OnboardingWizard(draft)
    wizard_size = (760, 560)
    wizard.resize(*wizard_size)
    wizard._show_step(3)
    wizard.name_edit.setText(LONG_NAME)
    wizard.game_edit.setText(LONG_GAME)
    wizard.save_feedback.setText("这是仅用于视觉检查的模拟配置；不会保存或开始监控。")
    wizard.show()
    shared._pump(app)
    controls = _visible_attrs(wizard, (
        "step_label", "back_button", "cancel_button", "finish_button",
        "name_label", "name_edit", "game_edit", "game_label", "game_guidance",
        "save_feedback",
    ))
    footer_names = {"back button", "cancel button", "finish button"}
    scroll_controls = {
        name: widget for name, widget in controls.items() if name not in footer_names
    }
    scroll_controls.update({
        f"{name.removesuffix(' button')} footer button": controls[name]
        for name in footer_names if name in controls
    })
    scroll_info = shared._wizard_scroll_record(wizard, scroll_controls)
    entries.append(_capture_dialog(
        wizard,
        output,
        f"wizard/{scale.replace('.', '')}/final-page.png",
        scale=scale,
        scenario="wizard-final-page-long-chinese-profile-labels",
        size=wizard_size,
        controls=controls,
        scroll=scroll_info,
        details={
            "fixture_metadata": {"name": LONG_NAME, "game": LONG_GAME},
            "scroll_area_count": len(wizard.findChildren(shared.QScrollArea)),
        },
    ))
    wizard.reject()


def _capture_scale(scale: str, output: Path) -> dict[str, Any]:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    app = QApplication([sys.argv[0]])
    font_id = QFontDatabase.addApplicationFont(str(FONT_PATH))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    if not families:
        raise RuntimeError(f"required Microsoft YaHei font could not be loaded: {FONT_PATH}")
    app.setFont(QFont(families[0], 10))

    scale_dir = output / f"scale-{scale.replace('.', '')}"
    scale_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="auto-ready-profile-ui-fixtures-") as directory:
        current, choices = _profile_fixtures(Path(directory))
        for size in MAIN_SIZES:
            _capture_main(app, scale_dir, scale, entries, size, current)
        _capture_rename(app, scale_dir, scale, entries)
        _capture_chooser(app, scale_dir, scale, entries, choices)
        _capture_wizard(app, scale_dir, scale, entries)

    shared._save_contact_sheet(entries, scale_dir / "contact-sheet.png")
    manifest = {
        "schema_version": 1,
        "qt_scale_factor": float(scale),
        "font": {"path": str(FONT_PATH), "family": families[0]},
        "source_hashes": _source_hashes(),
        "display": (
            "Qt offscreen; synthetic profile fixtures; fake backend/hotkey; "
            "input rejects clicks"
        ),
        "input_sent": False,
        "desktop_capture": False,
        "capture_count": len(entries),
        "captures": entries,
        "contact_sheet": str((scale_dir / "contact-sheet.png").resolve()),
        "automated_layout_issue_count": sum(len(item["layout_issues"]) for item in entries),
        "manual_visual_review": {"status": "pending"},
    }
    manifest_path = output / f"manifest-{scale.replace('.', '')}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    app.quit()
    return manifest


def _run_matrix(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    initial_hashes = _source_hashes()
    manifests: list[dict[str, Any]] = []
    for scale in SCALES:
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QT_SCALE_FACTOR"] = scale
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--scale", scale,
             "--output", str(output.resolve())],
            cwd=ROOT,
            env=env,
            check=True,
        )
        manifest = json.loads(
            (output / f"manifest-{scale.replace('.', '')}.json").read_text(encoding="utf-8")
        )
        if manifest["source_hashes"] != initial_hashes:
            raise RuntimeError(f"profile UI sources changed during the {scale} scale capture")
        manifests.append(manifest)
    if _source_hashes() != initial_hashes:
        raise RuntimeError(
            "profile UI sources changed during the scale matrix; rerun after handoff"
        )

    captures = [capture for manifest in manifests for capture in manifest["captures"]]
    result = {
        "schema_version": 1,
        "scales": [float(scale) for scale in SCALES],
        "main_sizes_logical": [list(size) for size in MAIN_SIZES],
        "source_hashes": initial_hashes,
        "display": "Qt offscreen; synthetic fixtures; fake backend/hotkey; input rejects clicks",
        "input_sent": False,
        "desktop_capture": False,
        "capture_count": len(captures),
        "automated_layout_issue_count": sum(len(item["layout_issues"]) for item in captures),
        "manual_visual_review": {"status": "pending"},
        "captures": captures,
        "contact_sheets": [manifest["contact_sheet"] for manifest in manifests],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "manifest": str(manifest_path.resolve()),
        "capture_count": result["capture_count"],
        "automated_layout_issue_count": result["automated_layout_issue_count"],
    }, ensure_ascii=False))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--scale", choices=SCALES)
    args = parser.parse_args()
    if args.scale:
        manifest = _capture_scale(args.scale, args.output)
        print(json.dumps({
            "scale": args.scale,
            "capture_count": manifest["capture_count"],
            "automated_layout_issue_count": manifest["automated_layout_issue_count"],
        }, ensure_ascii=False))
    else:
        _run_matrix(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
