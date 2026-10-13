"""Read-only Windows integration probe against our own simulator, never sends input."""

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import cv2
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QApplication

from auto_ready import __version__
from auto_ready.models import Rect, Region, Settings, Template
from auto_ready.native import GlobalHotkey, SingleInstance, WindowsBackend, enable_dpi_awareness
from auto_ready.recognition import match
from auto_ready.simulator import SimulatorWindow


def main():
    enable_dpi_awareness()
    cv2.setNumThreads(1)
    app = QApplication(sys.argv[:1])
    window = SimulatorWindow()
    window.move(80, 80)
    window.show()
    window.manual_ready()
    backend = WindowsBackend()
    report = {"app_version": __version__, "real_mouse_input": False, "stages": [], "errors": []}
    reference = None
    original_geometry = None

    def sample(stage):
        nonlocal reference, original_geometry
        try:
            identities = backend.list_windows()
            identity = next(i for i in identities if i.hwnd == int(window.winId()))
            geometry = backend.inspect(identity)
            started = time.perf_counter()
            frame = backend.capture(identity, "desktop-probe")
            elapsed = (time.perf_counter() - started) * 1000
            point = window.ready_button.mapTo(window, QPoint(0, 0))
            logical = Rect(
                point.x(), point.y(), window.ready_button.width(), window.ready_button.height()
            )
            rect = Region.from_rect(logical, window.width(), window.height()).pixels(
                geometry.width, geometry.height
            )
            if reference is None:
                original_geometry = geometry
                reference = Template(
                    frame.image[rect.y : rect.y + rect.height, rect.x : rect.x + rect.width].copy(),
                    (geometry.width, geometry.height),
                    geometry.dpi,
                )
            detection = match(frame, reference, Region(), Settings())
            report["stages"].append(
                {
                    "stage": stage,
                    "geometry": asdict(geometry),
                    "qt_logical_client": [window.width(), window.height()],
                    "qt_device_pixel_ratio": window.devicePixelRatioF(),
                    "capture_ms": round(elapsed, 2),
                    "frame_shape": list(frame.image.shape),
                    "foreground": backend.foreground(identity),
                    "moving": backend.moving(identity),
                    "unobscured": backend.unobscured(identity, rect, geometry),
                    "score": detection.best_score,
                    "candidate_count": len(detection.candidates),
                    "reason": detection.reason,
                }
            )
            if stage == "translated":
                assert geometry != original_geometry
                assert geometry.template_key == original_geometry.template_key
                assert detection.unique is not None, detection.reason
        except Exception as exc:
            report["errors"].append(f"{stage}: {type(exc).__name__}: {exc}")

    def first():
        sample("initial")
        window.move(180, 150)

    def finish():
        sample("translated")
        keys = {}
        for spec in ("Ctrl+Alt+F11",):
            key = GlobalHotkey(spec, lambda: None)
            keys[spec] = {"registered": key.start(), "error": key.error}
            key.close()
            keys[spec]["released"] = not key.available
        report["hotkeys"] = keys
        first_lock, second_lock = SingleInstance(), SingleInstance()
        try:
            report["single_instance"] = [first_lock.acquire(), second_lock.acquire()]
        finally:
            first_lock.close()
            second_lock.close()
        report["screens"] = [
            {
                "name": s.name(),
                "logical_geometry": [
                    s.geometry().x(),
                    s.geometry().y(),
                    s.geometry().width(),
                    s.geometry().height(),
                ],
                "device_pixel_ratio": s.devicePixelRatio(),
            }
            for s in app.screens()
        ]
        output = Path("artifacts")
        output.mkdir(exist_ok=True)
        (output / f"desktop-probe-{__version__}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        window.close()
        app.quit()

    QTimer.singleShot(800, first)
    QTimer.singleShot(1500, finish)
    app.exec()
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
