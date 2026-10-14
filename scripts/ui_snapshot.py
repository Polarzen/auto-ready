"""Capture Auto Ready's own Qt widgets across high-DPI and compact layouts.

Every run uses an offscreen QApplication, an in-memory fake target, a fake
hotkey, a rejecting input adapter, and a temporary configuration directory.
No user window or desktop input is inspected or controlled.
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

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPainter, QPen, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget  # noqa: E402

from auto_ready.config import ConfigStore  # noqa: E402
from auto_ready.models import (  # noqa: E402
    Candidate,
    CaptureStatus,
    Detection,
    Frame,
    Geometry,
    Identity,
    Rect,
    Region,
    Settings,
    Template,
)
from auto_ready.onboarding import OnboardingDraft, OnboardingWizard  # noqa: E402
from auto_ready.ui import MainWindow, close_and_wait  # noqa: E402

SCALES = ("1.0", "1.25", "1.5", "2.0")
MAIN_SIZES = ((760, 560), (640, 420))
WIZARD_SIZES = ((800, 620), (640, 420))
ADVANCED_SIZES = ((900, 720), (640, 420))
FONT_PATH = Path("C:/Windows/Fonts/msyh.ttc")
BUTTON_RECT = Rect(224, 150, 192, 60)


class FakeBackend:
    """Synthetic window data with no connection to native window APIs."""

    def __init__(self) -> None:
        self.identity = Identity(0x7FFF0001, 987654, 123456789, "mock-game.exe", "模拟游戏窗口")
        self.geometry = Geometry(0, 0, 640, 360, 96)
        self.image = self._sample_image()
        self.sequence = 0

    @staticmethod
    def _sample_image() -> np.ndarray:
        y, x = np.indices((360, 640))
        image = np.empty((360, 640, 3), dtype=np.uint8)
        image[:, :, 0] = (24 + (x // 8) % 20).astype(np.uint8)
        image[:, :, 1] = (30 + (y // 5) % 24).astype(np.uint8)
        image[:, :, 2] = 42
        cv2.rectangle(image, (BUTTON_RECT.x, BUTTON_RECT.y),
                      (BUTTON_RECT.x + BUTTON_RECT.width,
                       BUTTON_RECT.y + BUTTON_RECT.height), (16, 24, 33), -1)
        cv2.rectangle(image, (BUTTON_RECT.x + 4, BUTTON_RECT.y + 4),
                      (BUTTON_RECT.x + BUTTON_RECT.width - 4,
                       BUTTON_RECT.y + BUTTON_RECT.height - 4), (50, 177, 142), -1)
        cv2.putText(image, "READY", (BUTTON_RECT.x + 38, BUTTON_RECT.y + 42),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.05, (250, 255, 250), 2, cv2.LINE_AA)
        return image

    def list_windows(self) -> list[Identity]:
        return [self.identity]

    def inspect(self, identity: Identity) -> Geometry:
        if identity != self.identity:
            raise RuntimeError("unknown synthetic identity")
        return self.geometry

    def capture(self, identity: Identity, session_id: str) -> Frame:
        self.inspect(identity)
        self.sequence += 1
        return Frame(self.image.copy(), self.geometry, identity, time.monotonic(),
                     session_id, self.sequence, backend="synthetic")

    def foreground(self, _identity: Identity) -> bool:
        return True

    def moving(self, _identity: Identity) -> bool:
        return False

    def unobscured(self, _identity: Identity, _rect: Rect, _geometry: Geometry) -> bool:
        return True

    def capture_status(self) -> CaptureStatus:
        return CaptureStatus(backend="synthetic", supported=True, valid_frame=True,
                             background_supported=True, fresh=True)

    def release_capture(self) -> None:
        pass


class FakeInput:
    def click(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("visual QA must never send input")


class FakeHotkey:
    available = True
    error = ""

    def __init__(self, _spec: str, _callback: Any) -> None:
        pass

    def start(self) -> bool:
        return True

    def close(self) -> None:
        pass


def _pump(app: QApplication, seconds: float = 0.12) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def _wait_for(app: QApplication, predicate: Any, description: str, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            _pump(app, 0.08)
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {description}")


def _contained(root: QWidget, widget: QWidget) -> tuple[bool, list[int]]:
    point = widget.mapTo(root, QPoint(0, 0))
    rect = QRect(point, widget.size())
    return root.rect().contains(rect), [rect.x(), rect.y(), rect.width(), rect.height()]


def _intersection(left: QRect, right: QRect) -> bool:
    return left.intersects(right) and left.width() > 0 and left.height() > 0


def _control_record(root: QWidget, widget: QWidget) -> dict[str, Any]:
    inside, bounds = _contained(root, widget)
    return {
        "visible": widget.isVisible(),
        "contained_in_capture": inside,
        "bounds_logical": bounds,
        "size_logical": [widget.width(), widget.height()],
    }


def _capture(
    root: QWidget,
    output_dir: Path,
    filename: str,
    *,
    scale: str,
    scenario: str,
    state: str,
    requested_size: tuple[int, int],
    required_controls: dict[str, QWidget],
    scroll: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _pump(QApplication.instance(), 0.12)  # type: ignore[arg-type]
    pixmap = root.grab()
    path = output_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if not pixmap.save(str(path)):
        raise OSError(f"could not save screenshot: {path}")

    controls = {name: _control_record(root, widget)
                for name, widget in required_controls.items()}
    issues: list[str] = []
    for name, record in controls.items():
        if not record["visible"]:
            issues.append(f"required control is hidden: {name}")
        elif not record["contained_in_capture"]:
            issues.append(f"required control extends outside the captured window: {name}")

    names = list(required_controls)
    for index, first_name in enumerate(names):
        first = required_controls[first_name]
        if not first.isVisible():
            continue
        first_pos = first.mapTo(root, QPoint(0, 0))
        first_rect = QRect(first_pos, first.size())
        for second_name in names[index + 1:]:
            second = required_controls[second_name]
            if not second.isVisible():
                continue
            second_pos = second.mapTo(root, QPoint(0, 0))
            if _intersection(first_rect, QRect(second_pos, second.size())):
                issues.append(f"required controls overlap: {first_name} / {second_name}")

    image = pixmap.toImage()
    dpr = float(root.devicePixelRatioF())
    entry: dict[str, Any] = {
        "scenario": scenario,
        "state": state,
        "qt_scale_factor": float(scale),
        "requested_window_size_logical": list(requested_size),
        "actual_window_size_logical": [root.width(), root.height()],
        "screenshot_size_physical": [image.width(), image.height()],
        "device_pixel_ratio": dpr,
        "screenshot": str(path.resolve()),
        "required_controls": controls,
        "layout_issues": issues,
    }
    if scroll is not None:
        entry["scroll"] = scroll
        unreachable = [
            name for name, reachable in scroll.get(
                "required_controls_reachable_by_scrolling", {}
            ).items() if not reachable
        ]
        if unreachable:
            issues.extend(
                f"required control is unreachable by scrolling: {name}"
                for name in unreachable
            )
        if scroll.get("footer_controls_remain_fixed") is False:
            issues.append("wizard footer controls do not remain inside the window")
        entry["layout_issues"] = issues
    return entry


def _save_contact_sheet(entries: list[dict[str, Any]], path: Path) -> None:
    """Compose a labeled two-column overview with Qt's image classes only."""
    tile_width, tile_height = 500, 335
    columns = 2
    rows = (len(entries) + columns - 1) // columns
    sheet = QImage(columns * tile_width, rows * tile_height,
                   QImage.Format.Format_RGB32)
    sheet.fill(Qt.GlobalColor.white)
    painter = QPainter(sheet)
    painter.setPen(QPen(Qt.GlobalColor.black))
    for index, entry in enumerate(entries):
        x = (index % columns) * tile_width
        y = (index // columns) * tile_height
        painter.drawText(x + 8, y + 18, entry["scenario"])
        pixmap = QPixmap(entry["screenshot"])
        thumbnail = pixmap.scaled(tile_width - 16, tile_height - 34,
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap(x + 8, y + 25, thumbnail)
        if entry["layout_issues"]:
            painter.setPen(QPen(Qt.GlobalColor.red))
            painter.drawText(x + 8, y + tile_height - 5,
                             f"LAYOUT ISSUE: {len(entry['layout_issues'])}")
            painter.setPen(QPen(Qt.GlobalColor.black))
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not sheet.save(str(path)):
        raise OSError(f"could not save contact sheet: {path}")


def _source_hashes() -> dict[str, str]:
    paths = (ROOT / "src" / "auto_ready" / "ui.py",
             ROOT / "src" / "auto_ready" / "onboarding.py")
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def _main_required(window: MainWindow) -> dict[str, QWidget]:
    return {
        "profile selector": window.profile_combo,
        "target selector": window.target_combo,
        "start button": window.start_button,
        "stop button": window.stop_button,
        "onboarding button": window.onboarding_button,
        "advanced settings button": window.advanced_button,
    }


def _dashboard_scroll(window: MainWindow) -> QScrollArea:
    scroll = window.centralWidget().findChild(QScrollArea)  # type: ignore[union-attr]
    if scroll is None:
        raise RuntimeError("main dashboard scroll area is missing")
    return scroll


def _scroll_record(scroll: QScrollArea, required: dict[str, QWidget]) -> dict[str, Any]:
    vertical = scroll.verticalScrollBar()
    horizontal = scroll.horizontalScrollBar()
    viewport = scroll.viewport()
    reachable: dict[str, bool] = {}
    original = (horizontal.value(), vertical.value())
    for name, widget in required.items():
        content_pos = widget.mapTo(scroll.widget(), QPoint(0, 0))
        visible_height = min(widget.height(), viewport.height())
        visible_width = min(widget.width(), viewport.width())
        target_x = max(0, min(content_pos.x() - (viewport.width() - visible_width) // 2,
                              horizontal.maximum()))
        target_y = max(0, min(content_pos.y() - (viewport.height() - visible_height) // 2,
                              vertical.maximum()))
        horizontal.setValue(target_x)
        vertical.setValue(target_y)
        _pump(QApplication.instance(), 0.02)  # type: ignore[arg-type]
        point = widget.mapTo(viewport, QPoint(0, 0))
        reachable[name] = viewport.rect().contains(QRect(point, widget.size()))
    horizontal.setValue(original[0])
    vertical.setValue(original[1])
    return {
        "horizontal_range": [horizontal.minimum(), horizontal.maximum()],
        "horizontal_value": horizontal.value(),
        "vertical_range": [vertical.minimum(), vertical.maximum()],
        "vertical_value": vertical.value(),
        "viewport_size_logical": [viewport.width(), viewport.height()],
        "required_controls_reachable_by_scrolling": reachable,
    }


def _prepare_ready_dashboard(app: QApplication, window: MainWindow) -> None:
    _wait_for(app, lambda: window.target_combo.count() >= 2, "synthetic target inventory")
    window.target_combo.setCurrentIndex(1)
    _wait_for(app, lambda: window.geometry is not None, "synthetic target geometry")
    window.capture_static()
    _wait_for(app, lambda: window._static_frame is not None, "synthetic still frame")
    window.selection_combo.setCurrentIndex(1)
    window._selection_made(BUTTON_RECT)
    _wait_for(app, lambda: window.button is not None, "synthetic button template")
    window.store.save(window.settings, window.region, window.button, window.success)
    window._saved_config_signature = window._config_signature()
    window._update_dirty_state()
    window._update_current_config_label()
    _pump(app, 0.2)


def _capture_main_scenarios(
    app: QApplication, base_output: Path, scale: str, entries: list[dict[str, Any]]
) -> None:
    for width, height in MAIN_SIZES:
        size = (width, height)
        with tempfile.TemporaryDirectory(prefix="auto-ready-ui-qa-") as directory:
            window = MainWindow(
                backend=FakeBackend(), input_adapter=FakeInput(),
                store=ConfigStore(Path(directory)), hotkey_factory=FakeHotkey,
                notifier=lambda *_args: None, capture_delay=0,
            )
            window.resize(*size)
            window.show()
            _pump(app, 0.35)
            scroll = _dashboard_scroll(window)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            size_name = f"{width}x{height}"
            req = _main_required(window)
            scroll_details = _scroll_record(scroll, {
                "target selector": window.target_combo,
                "onboarding button": window.onboarding_button,
                "advanced settings button": window.advanced_button,
            })
            scroll.verticalScrollBar().setValue(0)
            entries.append(_capture(
                window, base_output,
                f"dashboard/{scale.replace('.', '')}/{size_name}/no-config.png",
                scale=scale, scenario=f"dashboard-no-config-{size_name}", state="no-config",
                requested_size=size, required_controls=req, scroll=scroll_details,
            ))
            if scroll.verticalScrollBar().maximum() > 0:
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                _pump(app)
                entries.append(_capture(
                    window, base_output,
                    f"dashboard/{scale.replace('.', '')}/{size_name}/no-config-scroll-bottom.png",
                    scale=scale, scenario=f"dashboard-no-config-scroll-bottom-{size_name}",
                    state="no-config", requested_size=size, required_controls=req,
                    scroll=scroll_details,
                ))
                scroll.verticalScrollBar().setValue(0)

            _prepare_ready_dashboard(app, window)
            req = _main_required(window)
            ready_scroll_details = _scroll_record(scroll, {
                "target selector": window.target_combo,
                "onboarding button": window.onboarding_button,
                "advanced settings button": window.advanced_button,
            })
            scroll.verticalScrollBar().setValue(0)
            entries.append(_capture(
                window, base_output, f"dashboard/{scale.replace('.', '')}/{size_name}/ready.png",
                scale=scale, scenario=f"dashboard-ready-{size_name}", state="ready",
                requested_size=size, required_controls=req, scroll=ready_scroll_details,
            ))
            if scroll.verticalScrollBar().maximum() > 0:
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                _pump(app)
                entries.append(_capture(
                    window, base_output,
                    f"dashboard/{scale.replace('.', '')}/{size_name}/ready-scroll-bottom.png",
                    scale=scale, scenario=f"dashboard-ready-scroll-bottom-{size_name}",
                    state="ready", requested_size=size, required_controls=req,
                    scroll=ready_scroll_details,
                ))
            close_and_wait(window)


def _wizard_required(wizard: OnboardingWizard, step: int) -> dict[str, QWidget]:
    controls: dict[str, QWidget] = {
        "step label": wizard.step_label,
        "cancel footer button": wizard.cancel_button,
        "active next footer button": wizard.finish_button if step == 3 else wizard.next_button,
    }
    if step > 0:
        controls["back footer button"] = wizard.back_button
    page_controls: dict[int, dict[str, QWidget]] = {
        0: {
            "target selector": wizard.target_combo,
            "refresh targets button": wizard.refresh_targets_button,
            "load profile button": wizard.load_profile_button,
        },
        1: {
            "capture button": wizard.capture_button,
            "undo selection button": wizard.undo_button,
            "advanced selection toggle": wizard.advanced_button,
            "selection type selector": wizard.selection_combo,
            "synthetic sample preview": wizard.preview,
            "selection feedback": wizard.button_feedback,
        },
        2: {
            "recognition check button": wizard.check_button,
            "recognition feedback": wizard.recognition_feedback,
        },
        3: {
            "profile name field": wizard.name_edit,
            "save feedback": wizard.save_feedback,
        },
    }
    controls.update(page_controls[step])
    return controls


def _wizard_scroll_record(
    wizard: OnboardingWizard, controls: dict[str, QWidget]
) -> dict[str, Any]:
    scroll = wizard.findChild(QScrollArea)
    if scroll is None:
        raise RuntimeError("wizard page scroll area is missing")
    page_controls = {name: widget for name, widget in controls.items()
                     if "footer" not in name and name != "step label"}
    result = _scroll_record(scroll, page_controls)
    result["footer_controls_remain_fixed"] = all(
        _contained(wizard, controls[name])[0]
        for name in controls if "footer" in name
    )
    return result


def _wizard_screenshot_controls(
    wizard: OnboardingWizard, step: int, *, bottom: bool = False
) -> dict[str, QWidget]:
    controls = _wizard_required(wizard, step)
    if step == 1:
        names = (
            {"selection type selector", "synthetic sample preview", "selection feedback"}
            if bottom else
            {"capture button", "undo selection button", "advanced selection toggle",
             "selection type selector"}
        )
    elif step == 2 and bottom:
        names = {"recognition feedback"}
    else:
        names = set(controls) - {"synthetic sample preview", "selection feedback",
                                 "recognition feedback"}
    selected = {name: widget for name, widget in controls.items() if name in names}
    if step == 1 or step == 2:
        selected.update({name: widget for name, widget in controls.items()
                         if "footer" in name or name == "step label"})
    return selected


def _capture_wizard_scenarios(
    app: QApplication, base_output: Path, scale: str, entries: list[dict[str, Any]]
) -> None:
    backend = FakeBackend()
    frame = backend.capture(backend.identity, "ui-qa-sample")
    crop = frame.image[BUTTON_RECT.y:BUTTON_RECT.y + BUTTON_RECT.height,
                       BUTTON_RECT.x:BUTTON_RECT.x + BUTTON_RECT.width].copy()
    template = Template(crop, (backend.geometry.width, backend.geometry.height),
                        backend.geometry.dpi, "synthetic READY button")
    candidate = Candidate(BUTTON_RECT, 0.987)
    detection = Detection((candidate,), 0.987, "")

    for width, height in WIZARD_SIZES:
        size = (width, height)
        size_name = f"{width}x{height}"
        draft = OnboardingDraft(
            backend.identity, backend.geometry, Settings(), Region(), template, None,
            frame=frame, confirmed=True,
            recognition_text="找到一个合适按钮（模拟画面）；仍需手动开始本轮。",
            original_region=Region(), original_button=template, original_success=None,
        )
        wizard = OnboardingWizard(draft)
        wizard.resize(*size)
        wizard.set_targets([backend.identity])
        wizard.set_target_geometry(backend.identity, backend.geometry)
        wizard.set_capture_guidance("synthetic", 0)
        wizard.set_frame(frame)
        wizard.advanced_button.setChecked(True)
        wizard.recognition_completed(detection)
        wizard.name_edit.setText("模拟 READY 配置")
        wizard.save_feedback.setText("模拟配置已准备；此截图流程不会保存或启动一轮。")
        wizard.show()
        _pump(app, 0.25)

        scroll = wizard.findChild(QScrollArea)
        if scroll is None:
            raise RuntimeError("wizard page scroll area is missing")
        for step in range(4):
            wizard._show_step(step)
            _pump(app, 0.12)
            scroll.verticalScrollBar().setValue(0)
            all_controls = _wizard_required(wizard, step)
            scroll_info = _wizard_scroll_record(wizard, all_controls)
            controls = _wizard_screenshot_controls(wizard, step)
            entries.append(_capture(
                wizard, base_output,
                f"wizard/{scale.replace('.', '')}/{size_name}/step-{step + 1}.png",
                scale=scale, scenario=f"wizard-step-{step + 1}-{size_name}",
                state=f"step-{step + 1}", requested_size=size,
                required_controls=controls, scroll=scroll_info,
            ))
            if step in {1, 2} and scroll.verticalScrollBar().maximum() > 0:
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                _pump(app)
                bottom_controls = _wizard_screenshot_controls(wizard, step, bottom=True)
                bottom_scroll = _wizard_scroll_record(wizard, all_controls)
                scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                _pump(app)
                entries.append(_capture(
                    wizard, base_output,
                    f"wizard/{scale.replace('.', '')}/{size_name}/"
                    f"step-{step + 1}-scroll-bottom.png",
                    scale=scale,
                    scenario=f"wizard-step-{step + 1}-scroll-bottom-{size_name}",
                    state=f"step-{step + 1}", requested_size=size,
                    required_controls=bottom_controls, scroll=bottom_scroll,
                ))
        wizard.reject()
        _pump(app, 0.12)


def _capture_advanced_scenarios(
    app: QApplication, base_output: Path, scale: str, entries: list[dict[str, Any]]
) -> None:
    with tempfile.TemporaryDirectory(prefix="auto-ready-ui-qa-advanced-") as directory:
        window = MainWindow(
            backend=FakeBackend(), input_adapter=FakeInput(),
            store=ConfigStore(Path(directory)), hotkey_factory=FakeHotkey,
            notifier=lambda *_args: None, capture_delay=0,
        )
        window.resize(*MAIN_SIZES[0])
        window.show()
        _pump(app, 0.3)
        dialog = window.advanced_dialog
        scroll = dialog.findChild(QScrollArea)
        if scroll is None:
            raise RuntimeError("advanced settings scroll area is missing")
        window.show_advanced()
        for width, height in ADVANCED_SIZES:
            dialog.resize(width, height)
            _pump(app, 0.18)
            size_name = f"{width}x{height}"
            horizontal = scroll.horizontalScrollBar()
            vertical = scroll.verticalScrollBar()
            regions = (
                ("top-left", horizontal.minimum(), vertical.minimum(), {
                    "static preview": window.preview,
                }),
                ("top-right", horizontal.maximum(), vertical.minimum(), {
                    "threshold field": window.threshold_spin,
                    "consecutive frame field": window.frames_spin,
                    "hotkey field": window.hotkey_combo,
                }),
                ("bottom-left", horizontal.minimum(), vertical.maximum(), {
                    "selection type selector": window.selection_combo,
                    "candidate and status log": window.candidates_text,
                }),
                ("bottom-right", horizontal.maximum(), vertical.maximum(), {
                    "capture method selector": window.capture_method_combo,
                    "sound setting": window.sound_enabled_checkbox,
                    "test sound button": window.sound_button,
                }),
            )
            for region_name, x_value, y_value, controls in regions:
                horizontal.setValue(x_value)
                vertical.setValue(y_value)
                _pump(app, 0.08)
                scroll_info = _scroll_record(scroll, controls)
                horizontal.setValue(x_value)
                vertical.setValue(y_value)
                _pump(app, 0.04)
                entries.append(_capture(
                    dialog, base_output,
                    f"advanced/{scale.replace('.', '')}/{size_name}/{region_name}.png",
                    scale=scale, scenario=f"advanced-{region_name}-{size_name}",
                    state=f"advanced-{region_name}", requested_size=(width, height),
                    required_controls=controls, scroll=scroll_info,
                ))
        close_and_wait(window)


def _single_scale(scale: str, output: Path) -> dict[str, Any]:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = scale
    app = QApplication([sys.argv[0]])
    font_id = QFontDatabase.addApplicationFont(str(FONT_PATH))
    font_families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    if font_id < 0 or not font_families:
        raise RuntimeError(f"required Microsoft YaHei font could not be loaded: {FONT_PATH}")
    app.setFont(QFont(font_families[0], 10))
    entries: list[dict[str, Any]] = []
    scale_dir = output / f"scale-{scale.replace('.', '')}"
    _capture_main_scenarios(app, scale_dir, scale, entries)
    _capture_wizard_scenarios(app, scale_dir, scale, entries)
    _capture_advanced_scenarios(app, scale_dir, scale, entries)
    _save_contact_sheet(entries, scale_dir / "contact-sheet.png")

    manifest = {
        "schema_version": 1,
        "qt_scale_factor": float(scale),
        "font": {"path": str(FONT_PATH), "family": font_families[0],
                 "application_font_id": font_id},
        "source_hashes": _source_hashes(),
        "display": "Qt offscreen; synthetic backend, fake hotkey, input adapter rejects clicks",
        "capture_count": len(entries),
        "captures": entries,
        "contact_sheet": str((scale_dir / "contact-sheet.png").resolve()),
        "automated_layout_issue_count": sum(len(entry["layout_issues"]) for entry in entries),
        "manual_visual_review": {
            "status": "pending",
            "note": (
                "Presence and containment are machine checked; visual readability "
                "requires screenshot inspection."
            ),
        },
    }
    scale_manifest = output / f"manifest-{scale.replace('.', '')}.json"
    scale_manifest.parent.mkdir(parents=True, exist_ok=True)
    scale_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    app.quit()
    return manifest


def _matrix(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    starting_source_hashes = _source_hashes()
    manifests = []
    for scale in SCALES:
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["QT_SCALE_FACTOR"] = scale
        command = [sys.executable, str(Path(__file__).resolve()), "--scale", scale,
                   "--output", str(output.resolve())]
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        manifest_path = output / f"manifest-{scale.replace('.', '')}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source_hashes"] != starting_source_hashes:
            raise RuntimeError(f"UI source changed during the {scale} scale capture")
        manifests.append(manifest)
    if _source_hashes() != starting_source_hashes:
        raise RuntimeError("UI source changed during the scale matrix; rerun after handoff")
    result = {
        "schema_version": 1,
        "matrix": {"scales": [float(item) for item in SCALES],
                   "main_sizes_logical": [list(size) for size in MAIN_SIZES],
                   "wizard_sizes_logical": [list(size) for size in WIZARD_SIZES],
                   "advanced_dialog_sizes_logical": [list(size) for size in ADVANCED_SIZES]},
        "capture_count": sum(item["capture_count"] for item in manifests),
        "source_hashes": starting_source_hashes,
        "automated_layout_issue_count": sum(
            item["automated_layout_issue_count"] for item in manifests
        ),
        "manual_visual_review": {
            "status": "pending",
            "note": "Inspect contact sheets and representative full-size captures before sign-off.",
        },
        "scale_manifests": [str((output / f"manifest-{scale.replace('.', '')}.json").resolve())
                            for scale in SCALES],
        "captures": [capture for item in manifests for capture in item["captures"]],
        "contact_sheets": [item["contact_sheet"] for item in manifests],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path.resolve()),
                      "capture_count": result["capture_count"],
                      "automated_layout_issue_count": result["automated_layout_issue_count"]},
                     ensure_ascii=False))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "artifacts" / "candidate-0.4.0" / "ui-qa")
    parser.add_argument("--scale", choices=SCALES,
                        help="capture one scale in a separate process; omit for the full matrix")
    args = parser.parse_args()
    if args.scale is not None:
        manifest = _single_scale(args.scale, args.output)
        print(json.dumps({"scale": args.scale, "capture_count": manifest["capture_count"],
                          "automated_layout_issue_count": manifest[
                              "automated_layout_issue_count"]}, ensure_ascii=False))
        return 0
    _matrix(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
