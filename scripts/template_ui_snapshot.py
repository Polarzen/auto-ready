"""Capture focused saved-template views using Qt offscreen only.

The harness writes a synthetic default profile to a temporary ConfigStore,
waits for MainWindow's asynchronous load to finish, and then captures the
profile dashboard, template viewer, and current-profile edit wizard. It never
uses native windows or input APIs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

os.environ["QT_QPA_PLATFORM"] = "offscreen"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import ui_snapshot as shared  # noqa: E402
from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QGroupBox,
    QPushButton,
    QScrollArea,
    QWidget,
)

from auto_ready.config import ConfigStore, ProfileMetadata  # noqa: E402
from auto_ready.models import Region, Settings, Template  # noqa: E402
from auto_ready.onboarding import OnboardingWizard  # noqa: E402
from auto_ready.ui import MainWindow  # noqa: E402

SCALES = ("1.0", "1.5", "2.0")
FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")
MAIN_LOADED_SIZE = (760, 660)
MAIN_COMPACT_SIZE = (640, 420)
VIEWER_SIZE = (760, 520)
WIZARD_SIZE = (760, 560)
OUTPUT_DEFAULT = ROOT / "artifacts" / "templates-0.4.2" / "ui-qa"

PROFILE_NAME = "星海远征自动加载视觉检查配置"
GAME_LABEL = "云海边境合成窗口（不会绑定目标）"
TEMPLATE_SIZE = (640, 360)
TEMPLATE_DPI = 96
BUTTON_RECT = (224, 150, 192, 60)


class NoWindowFakeBackend(shared.FakeBackend):
    """Keep MainWindow on the no-target path without consulting the desktop."""

    def list_windows(self) -> list[Any]:
        return []


def _source_hashes() -> dict[str, str]:
    names = (
        "src/auto_ready/ui.py",
        "src/auto_ready/onboarding.py",
        "src/auto_ready/template_preview.py",
        "src/auto_ready/config.py",
    )
    return {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in names
    }


def _make_success_image() -> np.ndarray:
    """Return a distinct synthetic success-mark PNG payload."""
    image = np.full((60, 192, 3), (32, 68, 28), dtype=np.uint8)
    cv2.rectangle(image, (2, 2), (189, 57), (40, 210, 90), 3)
    cv2.line(image, (8, 50), (184, 10), (70, 130, 245), 2)
    cv2.putText(image, "SUCCESS", (27, 39), cv2.FONT_HERSHEY_SIMPLEX,
                0.75, (250, 250, 250), 2, cv2.LINE_AA)
    cv2.circle(image, (174, 15), 5, (0, 255, 255), -1)
    return image


def _make_templates(backend: NoWindowFakeBackend) -> tuple[Template, Template]:
    x, y, width, height = BUTTON_RECT
    button_image = backend.image[y:y + height, x:x + width].copy()
    success_image = _make_success_image()
    return (
        Template(button_image, TEMPLATE_SIZE, TEMPLATE_DPI, "synthetic READY button"),
        Template(success_image, TEMPLATE_SIZE, TEMPLATE_DPI, "synthetic SUCCESS mark"),
    )


def _wait_for(app: QApplication, predicate: Any, description: str,
              timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            shared._pump(app, 0.08)
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {description}")


def _control_record(root: QWidget, widget: QWidget) -> dict[str, Any]:
    point = widget.mapTo(root, QPoint(0, 0))
    bounds = QRect(point, widget.size())
    return {
        "visible": widget.isVisibleTo(root),
        "contained_in_capture": root.rect().contains(bounds),
        "bounds_logical": [bounds.x(), bounds.y(), bounds.width(), bounds.height()],
    }


def _capture(
    root: QWidget,
    output: Path,
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
        root,
        output,
        filename,
        scale=scale,
        scenario=scenario,
        state="synthetic-saved-template-profile",
        requested_size=size,
        required_controls=controls,
        scroll=scroll,
    )
    if details:
        result["fixture_details"] = details
    return result


def _find_button(root: QWidget, *, attrs: tuple[str, ...], text_tokens: tuple[str, ...]
                 ) -> QPushButton:
    for name in attrs:
        value = getattr(root, name, None)
        if isinstance(value, QPushButton) and value.isVisibleTo(root):
            return value
    for button in root.findChildren(QPushButton):
        label = button.text().replace("&", "")
        if button.isVisibleTo(root) and all(token in label for token in text_tokens):
            return button
    raise RuntimeError(f"could not find visible button for {text_tokens!r}")


def _find_template_group(window: MainWindow) -> QWidget:
    for name in (
        "template_box",
        "current_templates_group",
        "current_template_group",
        "template_preview_group",
        "templates_preview_group",
    ):
        value = getattr(window, name, None)
        if isinstance(value, QWidget) and value.isVisibleTo(window):
            return value
    groups = window.findChildren(QGroupBox)
    for group in groups:
        title = group.title()
        if "模板" in title and ("当前" in title or "预览" in title):
            return group
    raise RuntimeError("could not find the visible current-template thumbnail group")


def _scroll_record(scroll: QScrollArea, controls: dict[str, QWidget]) -> dict[str, Any]:
    horizontal = scroll.horizontalScrollBar()
    vertical = scroll.verticalScrollBar()
    viewport = scroll.viewport()
    start = (horizontal.value(), vertical.value())
    reachable: dict[str, bool] = {}
    for name, widget in controls.items():
        content = scroll.widget()
        if content is None:
            reachable[name] = False
            continue
        point = widget.mapTo(content, QPoint(0, 0))
        x = max(0, min(point.x(), horizontal.maximum()))
        y = max(0, min(point.y(), vertical.maximum()))
        horizontal.setValue(x)
        vertical.setValue(y)
        shared._pump(QApplication.instance(), 0.02)  # type: ignore[arg-type]
        shown = widget.mapTo(viewport, QPoint(0, 0))
        reachable[name] = viewport.rect().contains(QRect(shown, widget.size()))
    horizontal.setValue(start[0])
    vertical.setValue(start[1])
    return {
        "horizontal_range": [horizontal.minimum(), horizontal.maximum()],
        "vertical_range": [vertical.minimum(), vertical.maximum()],
        "required_controls_reachable_by_scrolling": reachable,
    }


def _capture_visible_controls(
    root: QWidget, scroll: QScrollArea, controls: dict[str, QWidget]
) -> dict[str, QWidget]:
    """Filter controls clipped by the scroll viewport from a screenshot's checks."""
    content = scroll.widget()
    result: dict[str, QWidget] = {}
    for name, widget in controls.items():
        if content is not None and content.isAncestorOf(widget):
            point = widget.mapTo(scroll.viewport(), QPoint(0, 0))
            if scroll.viewport().rect().contains(QRect(point, widget.size())):
                result[name] = widget
        else:
            point = widget.mapTo(root, QPoint(0, 0))
            if root.rect().contains(QRect(point, widget.size())):
                result[name] = widget
    return result


def _wizard_scroll_record(
    wizard: OnboardingWizard, controls: dict[str, QWidget]
) -> dict[str, Any]:
    scroll = wizard.findChild(QScrollArea)
    if scroll is None:
        raise RuntimeError("wizard page scroll area is missing")
    page_controls = {
        name: widget for name, widget in controls.items()
        if "footer" not in name and name != "step label"
    }
    result = shared._scroll_record(scroll, page_controls)
    result["footer_controls_remain_fixed"] = all(
        wizard.rect().contains(
            QRect(widget.mapTo(wizard, QPoint(0, 0)), widget.size())
        )
        for name, widget in controls.items()
        if "footer" in name
    )
    return result


def _make_main(store: ConfigStore) -> MainWindow:
    return MainWindow(
        backend=NoWindowFakeBackend(),
        input_adapter=shared.FakeInput(),
        store=store,
        hotkey_factory=shared.FakeHotkey,
        notifier=lambda *_args: None,
        capture_delay=0,
    )


def _wait_for_default_templates(
    app: QApplication,
    window: MainWindow,
    expected_button: Template,
    expected_success: Template,
) -> None:
    shared._wait_for(
        app,
        lambda: (
            window.button is not None
            and window.success is not None
            and getattr(window, "_profile_content_ready", False)
            and not getattr(window, "_config_loading", False)
        ),
        "asynchronous default-profile template load",
        timeout=8.0,
    )
    if getattr(window, "identity", None) is not None:
        raise RuntimeError("synthetic main window unexpectedly selected a target")
    if not np.array_equal(window.button.image, expected_button.image):
        raise RuntimeError("loaded button template differs from the synthetic PNG fixture")
    if not np.array_equal(window.success.image, expected_success.image):
        raise RuntimeError("loaded success template differs from the synthetic PNG fixture")
    if window._current_profile_metadata.name != PROFILE_NAME:
        raise RuntimeError("startup load did not restore the synthetic profile name")
    if window._current_profile_metadata.game != GAME_LABEL:
        raise RuntimeError("startup load did not restore the synthetic game label")
    if window.target_combo.currentData() is not None:
        raise RuntimeError("default profile loading unexpectedly bound a target window")
    for name in ("button_template_thumbnail", "success_template_thumbnail"):
        widget = getattr(window, name, None)
        if widget is None or widget.pixmap() is None or widget.pixmap().isNull():
            raise RuntimeError(f"saved template thumbnail is blank: {name}")


def _visible_dialog(app: QApplication, parent: QWidget) -> QDialog:
    dialogs = [
        widget for widget in app.topLevelWidgets()
        if isinstance(widget, QDialog) and widget is not parent and widget.isVisible()
    ]
    if not dialogs:
        raise RuntimeError("template-view action did not open a visible dialog")
    return dialogs[-1]


def _open_current_edit_wizard(app: QApplication, window: MainWindow) -> OnboardingWizard:
    open_wizard = getattr(window, "open_onboarding", None)
    if not callable(open_wizard):
        raise RuntimeError("current-profile setup action is missing")
    open_wizard(new_profile=False)
    _wait_for(
        app,
        lambda: isinstance(getattr(window, "_wizard", None), OnboardingWizard)
        and window._wizard.isVisible(),
        "current-profile edit wizard",
    )
    return window._wizard


def _main_controls(window: MainWindow, view_button: QPushButton) -> dict[str, QWidget]:
    result = {
        "view templates button": view_button,
        "button template thumbnail": window.button_template_thumbnail,
        "success template thumbnail": window.success_template_thumbnail,
        "button template status": window.button_template_status,
        "success template status": window.success_template_status,
    }
    for name in ("current_config_label", "new_profile_button", "onboarding_button"):
        widget = getattr(window, name, None)
        if isinstance(widget, QWidget) and widget.isVisibleTo(window):
            result[name.replace("_", " ")] = widget
    return result


def _capture_window_pair(app: QApplication, output: Path, scale: str,
                         entries: list[dict[str, Any]], window: MainWindow) -> None:
    template_group = _find_template_group(window)
    view_button = _find_button(
        window,
        attrs=("template_preview_button", "view_templates_button", "view_template_button"),
        text_tokens=("查看", "模板"),
    )
    scroll = window.centralWidget().findChild(QScrollArea)  # type: ignore[union-attr]
    if scroll is None:
        raise RuntimeError("main dashboard scroll area is missing")
    new_profile_button = getattr(window, "new_profile_button", None)
    current_edit_button = getattr(window, "onboarding_button", None)
    if not isinstance(new_profile_button, QPushButton):
        raise RuntimeError("separate new-profile action is missing")
    if not isinstance(current_edit_button, QPushButton):
        raise RuntimeError("current-profile edit action is missing")
    if new_profile_button is current_edit_button:
        raise RuntimeError("new-profile and current-profile actions are not separate")

    window.resize(*MAIN_LOADED_SIZE)
    scroll.verticalScrollBar().setValue(0)
    shared._pump(app, 0.15)
    controls = _main_controls(window, view_button)
    visible_controls = _capture_visible_controls(window, scroll, controls)
    if not all(name in visible_controls for name in (
        "button template thumbnail", "success template thumbnail", "view templates button"
    )):
        raise RuntimeError("loaded main view does not show its saved-template preview")
    entries.append(_capture(
        window, output, f"main/{scale.replace('.', '')}/loaded-760x660.png",
        scale=scale, scenario="main-loaded-profile-without-target",
        size=MAIN_LOADED_SIZE, controls=visible_controls,
        details={"target_selected": False, "profile_name": PROFILE_NAME,
                 "game_label": GAME_LABEL,
                 "current_edit_action": current_edit_button.text(),
                 "new_profile_action": new_profile_button.text(),
                 "template_group_title": template_group.title(),
                 "template_group_visible": template_group.isVisibleTo(window)},
    ))

    window.resize(*MAIN_COMPACT_SIZE)
    scroll_controls = {
        name: widget for name, widget in controls.items()
        if scroll.widget() is not None and scroll.widget().isAncestorOf(widget)
    }
    reachability = _scroll_record(scroll, scroll_controls)
    # The saved-template group is near the top of this compact dashboard.
    scroll.verticalScrollBar().setValue(0)
    shared._pump(app, 0.12)
    compact_controls = _capture_visible_controls(window, scroll, controls)
    if not all(name in compact_controls for name in (
        "button template thumbnail", "success template thumbnail", "view templates button"
    )):
        raise RuntimeError("compact main view does not show its saved-template preview")
    entries.append(_capture(
        window, output, f"main/{scale.replace('.', '')}/compact-640x420.png",
        scale=scale, scenario="main-compact-template-preview-reachable-by-scroll",
        size=MAIN_COMPACT_SIZE, controls=compact_controls,
        details={"target_selected": False,
                 "template_group_title": template_group.title(),
                 "template_group_visible": template_group.isVisibleTo(window)},
        scroll=reachability,
    ))


def _capture_viewer(app: QApplication, output: Path, scale: str,
                    entries: list[dict[str, Any]], window: MainWindow) -> None:
    view_button = _find_button(
        window,
        attrs=("template_preview_button", "view_templates_button", "view_template_button"),
        text_tokens=("查看", "模板"),
    )
    if not view_button.isEnabled():
        raise RuntimeError("template viewer is disabled without a selected target")
    show_preview = getattr(window, "show_template_preview", None)
    if not callable(show_preview):
        raise RuntimeError("static template preview action is missing")
    show_preview()
    _wait_for(app, lambda: any(
        isinstance(widget, QDialog) and widget.isVisible()
        for widget in app.topLevelWidgets()
    ), "static template viewer")
    dialog = _visible_dialog(app, window)
    if dialog is not getattr(window, "template_preview_dialog", None):
        raise RuntimeError("template preview action opened an unexpected dialog")
    dialog.resize(*VIEWER_SIZE)
    shared._pump(app, 0.12)
    preview_controls = {
        "button template image": dialog.button_card.image,
        "button template details": dialog.button_card.details,
        "success template image": dialog.success_card.image,
        "success template details": dialog.success_card.details,
        "preview load state": dialog.state_label,
        "template compatibility guidance": dialog.compatibility_label,
    }
    state_text = dialog.state_label.text()
    compatibility_text = dialog.compatibility_label.text()
    if "尚未载入" in state_text or "尚未设置按钮" in compatibility_text:
        raise RuntimeError("static template viewer shows an unloaded or blank template state")
    if "请选择本次目标窗口" not in compatibility_text:
        raise RuntimeError("template viewer does not explain target-free template loading")
    for key in ("button template image", "success template image"):
        pixmap = preview_controls[key].pixmap()
        if pixmap is None or pixmap.isNull():
            raise RuntimeError(f"template viewer image is blank: {key}")
    entries.append(_capture(
        dialog, output, f"viewer/{scale.replace('.', '')}/static-templates.png",
        scale=scale, scenario="static-template-viewer-without-target",
        size=VIEWER_SIZE,
        controls=preview_controls,
        details={"target_selected": getattr(window, "identity", None) is not None,
                 "load_state": state_text,
                 "compatibility_guidance": compatibility_text,
                 "template_count": 2,
                 "button_sha256": hashlib.sha256(window.button.image.tobytes()).hexdigest(),
                 "success_sha256": hashlib.sha256(window.success.image.tobytes()).hexdigest()},
    ))
    dialog.accept()
    shared._pump(app, 0.08)


def _wizard_controls(wizard: OnboardingWizard, *, final_page: bool) -> dict[str, QWidget]:
    if final_page:
        names = (
            ("step label", "step_label"),
            ("back footer button", "back_button"),
            ("cancel footer button", "cancel_button"),
            ("finish footer button", "finish_button"),
            ("name label", "name_label"),
            ("name edit", "name_edit"),
            ("game label", "game_label"),
            ("game edit", "game_edit"),
            ("game guidance", "game_guidance"),
        )
    else:
        names = (
            ("step label", "step_label"),
            ("back footer button", "back_button"),
            ("cancel footer button", "cancel_button"),
            ("next footer button", "next_button"),
            ("capture guidance", "capture_guidance"),
            ("retained button preview", "retained_button_preview"),
            ("retained success preview", "retained_success_preview"),
            ("button feedback", "button_feedback"),
            ("success feedback", "success_feedback"),
            ("capture button", "capture_button"),
        )
    result = {
        label: widget
        for label, attr in names
        if isinstance((widget := getattr(wizard, attr, None)), QWidget)
        and widget.isVisibleTo(wizard)
    }
    if not result:
        raise RuntimeError("wizard page has no visible required controls")
    return result


def _capture_wizard(app: QApplication, output: Path, scale: str,
                    entries: list[dict[str, Any]], window: MainWindow) -> None:
    wizard = _open_current_edit_wizard(app, window)
    wizard.resize(*WIZARD_SIZE)
    wizard._show_step(1)
    shared._pump(app, 0.12)
    button_text = getattr(getattr(wizard, "button_feedback", None), "text", lambda: "")()
    if "尚未" in button_text or "未设置" in button_text:
        raise RuntimeError("current edit wizard discarded the saved button selection")
    if not bool(getattr(wizard.draft, "edit_current", False)):
        raise RuntimeError("current-profile wizard was opened in new-profile mode")
    if getattr(wizard.draft, "identity", None) is not None:
        raise RuntimeError("current-profile edit wizard unexpectedly binds a target")
    for name in ("retained_button_preview", "retained_success_preview"):
        widget = getattr(wizard, name, None)
        if widget is None or widget.pixmap() is None or widget.pixmap().isNull():
            raise RuntimeError(f"current edit wizard retained image is blank: {name}")
    entries.append(_capture(
        wizard, output, f"wizard/{scale.replace('.', '')}/retained-button-template.png",
        scale=scale, scenario="current-edit-wizard-retains-saved-button-template",
        size=WIZARD_SIZE, controls=_wizard_controls(wizard, final_page=False),
        scroll=_wizard_scroll_record(wizard, _wizard_controls(wizard, final_page=False)),
        details={"button_feedback": button_text,
                 "success_feedback": wizard.success_feedback.text(),
                 "button_sha256": hashlib.sha256(window.button.image.tobytes()).hexdigest()},
    ))

    wizard._show_step(3)
    shared._pump(app, 0.12)
    name_edit = getattr(wizard, "name_edit", None)
    game_edit = getattr(wizard, "game_edit", None)
    if name_edit is None or game_edit is None:
        raise RuntimeError("wizard final page does not expose name and game fields")
    if name_edit.text() != PROFILE_NAME or game_edit.text() != GAME_LABEL:
        raise RuntimeError("current edit wizard did not prepopulate profile name and game")
    labels_order = [
        getattr(wizard, name).mapTo(wizard, QPoint(0, 0)).y()
        for name in ("name_label", "name_edit", "game_label", "game_edit")
    ]
    if not labels_order[0] < labels_order[1] < labels_order[2] < labels_order[3]:
        raise RuntimeError("wizard final-page profile label/input order is incorrect")
    entries.append(_capture(
        wizard, output, f"wizard/{scale.replace('.', '')}/final-metadata.png",
        scale=scale, scenario="current-edit-wizard-prefilled-profile-metadata",
        size=WIZARD_SIZE, controls=_wizard_controls(wizard, final_page=True),
        scroll=_wizard_scroll_record(wizard, _wizard_controls(wizard, final_page=True)),
        details={"profile_name": name_edit.text(), "game_label": game_edit.text(),
                 "metadata_control_y_order": labels_order},
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
    backend = NoWindowFakeBackend()
    button, success = _make_templates(backend)
    with tempfile.TemporaryDirectory(prefix="auto-ready-template-ui-qa-") as directory:
        store = ConfigStore(Path(directory))
        store.save(Settings(), Region(), button, success,
                   metadata=ProfileMetadata(PROFILE_NAME, GAME_LABEL))
        window = _make_main(store)
        window.resize(*MAIN_LOADED_SIZE)
        window.show()
        shared._wait_for(app, lambda: window.isVisible(), "main window show")
        _wait_for_default_templates(app, window, button, success)
        _capture_window_pair(app, scale_dir, scale, entries, window)
        _capture_viewer(app, scale_dir, scale, entries, window)
        _capture_wizard(app, scale_dir, scale, entries, window)
        window.close()

    shared._save_contact_sheet(entries, scale_dir / "contact-sheet.png")
    manifest = {
        "schema_version": 1,
        "qt_scale_factor": float(scale),
        "font": {"path": str(FONT_PATH), "family": families[0]},
        "source_hashes": _source_hashes(),
        "display": (
            "Qt offscreen; temporary synthetic ConfigStore; fake backend/hotkey; "
            "input rejects clicks"
        ),
        "input_sent": False,
        "desktop_capture": False,
        "target_window_accessed": False,
        "capture_count": len(entries),
        "captures": entries,
        "contact_sheet": str((scale_dir / "contact-sheet.png").resolve()),
        "automated_layout_issue_count": sum(
            len(entry["layout_issues"]) for entry in entries
        ),
        "manual_visual_review": {"status": "pending"},
    }
    path = output / f"manifest-{scale.replace('.', '')}.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
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
            cwd=ROOT, env=env, check=True,
        )
        path = output / f"manifest-{scale.replace('.', '')}.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["source_hashes"] != initial_hashes:
            raise RuntimeError(f"template UI sources changed during {scale} capture")
        manifests.append(manifest)
    if _source_hashes() != initial_hashes:
        raise RuntimeError(
            "template UI sources changed during matrix; abort and rerun after freeze"
        )

    captures = [capture for manifest in manifests for capture in manifest["captures"]]
    result = {
        "schema_version": 1,
        "scales": [float(scale) for scale in SCALES],
        "source_hashes": initial_hashes,
        "display": (
            "Qt offscreen; temporary synthetic ConfigStore; fake backend/hotkey; "
            "input rejects clicks"
        ),
        "input_sent": False,
        "desktop_capture": False,
        "target_window_accessed": False,
        "capture_count": len(captures),
        "automated_layout_issue_count": sum(
            len(capture["layout_issues"]) for capture in captures
        ),
        "manual_visual_review": {"status": "pending"},
        "captures": captures,
        "contact_sheets": [manifest["contact_sheet"] for manifest in manifests],
    }
    path = output / "manifest.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "manifest": str(path.resolve()),
        "capture_count": result["capture_count"],
        "automated_layout_issue_count": result["automated_layout_issue_count"],
    }, ensure_ascii=False))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scale", choices=SCALES)
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT)
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


if __name__ == "__main__":
    main()
