from __future__ import annotations

import ctypes
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from auto_ready import app, capture, capture_check
from auto_ready.simulator import (
    CaptureMarkerWidget,
    CaptureOccluderWindow,
    CaptureProbeWindow,
)


def _marker(counter: int = 0, *, width: int = 640, height: int = 360) -> np.ndarray:
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = (24, 32, 16)
    side = max(8, int(min(width, height) * 0.42))
    margin_x = max(3, int(width * 0.025))
    margin_y = max(3, int(height * 0.08))
    corners = (
        ((margin_x, margin_y), (36, 36, 255)),
        ((width - margin_x - side, margin_y), (112, 224, 22)),
        ((margin_x, height - margin_y - side), (255, 125, 40)),
        ((width - margin_x - side, height - margin_y - side), (40, 219, 255)),
    )
    for (x, y), color in corners:
        image[y : y + side, x : x + side] = color
    cell_w = max(2, int(width * 0.031))
    gap = max(1, int(width * 0.009))
    start_x = (width - (16 * cell_w + 15 * gap)) // 2
    cell_h = max(6, int(height * 0.25))
    start_y = (height - cell_h) // 2
    for index in range(16):
        bit = (counter >> (15 - index)) & 1
        color = (208, 241, 244) if bit else (61, 51, 40)
        x = start_x + index * (cell_w + gap)
        image[start_y : start_y + cell_h, x : x + cell_w] = color
    return image


def _sample(
    *,
    counter: int = 23,
    sequence: int = 1,
    generation: int = 4,
    foreground: bool | None = False,
    obscured: bool | None = False,
) -> dict:
    return {
        "valid": True,
        "frame_received": True,
        "source_sequence": sequence,
        "source_time_100ns": sequence * 100,
        "generation": generation,
        "marker": {"valid": True, "counter": counter},
        "status": {
            "valid_frame": True,
            "fresh": True,
            "foreground": foreground,
            "obscured": obscured,
        },
    }


def test_pixel_decoder_reads_four_corners_and_binary_counter() -> None:
    decoded = capture_check.decode_marker(_marker(0x5A31))

    assert decoded["valid"] is True
    assert decoded["counter"] == 0x5A31
    assert all(decoded["corners"].values())

    obscured = _marker(3)
    obscured[:] = (255, 0, 255)
    assert capture_check.decode_marker(obscured)["valid"] is False


def test_pixel_decoder_matches_the_rendered_simulator_marker(qtbot) -> None:
    widget = CaptureMarkerWidget(frozen=True)
    qtbot.addWidget(widget)
    widget.resize(640, 360)
    widget.show()
    qtbot.wait(20)

    rendered = widget.grab().toImage().convertToFormat(QImage.Format.Format_BGR888)
    raw = np.frombuffer(rendered.bits(), dtype=np.uint8)
    image = raw.reshape(rendered.height(), rendered.bytesPerLine())[
        :, : rendered.width() * 3
    ].reshape(rendered.height(), rendered.width(), 3)

    decoded = capture_check.decode_marker(image)
    assert decoded["valid"] is True
    assert decoded["counter"] == widget.counter


def test_probe_windows_cannot_take_focus_or_intercept_mouse(qtbot) -> None:
    target = CaptureProbeWindow()
    occluder = CaptureOccluderWindow()
    qtbot.addWidget(target)
    qtbot.addWidget(occluder)

    assert target.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert not target.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert target.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert target.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert occluder.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert occluder.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert occluder.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


@pytest.mark.parametrize(
    ("condition", "samples", "extra", "expected"),
    [
        ("non_foreground", [_sample()], {}, "pass"),
        ("occluded", [_sample(obscured=True)], {}, "pass"),
        ("static_pixels", [_sample(sequence=1), _sample(sequence=2)], {}, "pass"),
        (
            "minimized",
            [
                {
                    "frame_received": False,
                    "valid": False,
                    "status": {"valid_frame": False, "minimized": True},
                }
            ],
            {},
            "pass",
        ),
        (
            "target_closed",
            [
                {
                    "frame_received": False,
                    "valid": False,
                    "target_exists": False,
                    "status": {"valid_frame": False},
                }
            ],
            {},
            "pass",
        ),
        ("release_rebuild", [_sample(generation=5)], {"generation_before_release": 4}, "pass"),
    ],
)
def test_phase_evidence_requires_the_expected_condition(
    condition: str, samples: list[dict], extra: dict, expected: str
) -> None:
    assert capture_check.evaluate_phase(condition, samples, **extra)["status"] == expected


def test_static_phase_accepts_new_sequences_with_unchanged_pixels() -> None:
    result = capture_check.evaluate_phase(
        "static_pixels", [_sample(sequence=7), _sample(sequence=8)]
    )

    assert result["status"] == "pass"
    assert result["result_subtype"] == "fresh_frames_unchanged_marker"
    assert result["fresh_frame_demonstration"] == "pass"
    assert "same frozen marker pixels" in result["reason"]


def test_static_phase_accepts_explicit_source_suppression_without_claiming_fresh_frames() -> None:
    fresh = _sample(sequence=1)
    fresh["new_source_frame"] = True
    suppressed = {
        "frame_received": False,
        "valid": False,
        "new_source_frame": False,
        "error_type": "CapturePendingError",
        "status": {
            "backend": "wgc",
            "valid_frame": False,
            "fresh": False,
            "minimized": False,
            "source_sequence": None,
        },
    }

    result = capture_check.evaluate_phase("static_pixels", [fresh, suppressed])

    assert result["status"] == "pass"
    assert result["result_subtype"] == "source_updates_suppressed_stale_cache_withheld"
    assert result["fresh_frame_demonstration"] == "unverified"
    assert "withheld stale frames" in result["reason"]
    fresh["captured_in_phase"] = False
    queued_then_silent = capture_check.evaluate_phase("static_pixels", [fresh, suppressed])
    assert queued_then_silent["status"] == "pass"
    assert queued_then_silent["fresh_frame_demonstration"] == "unverified"
    assert queued_then_silent["valid_frame_count"] == 0


def test_static_phase_rejects_a_repeated_cached_frame() -> None:
    cached = _sample(sequence=1)
    cached["valid"] = False
    cached["new_source_frame"] = False

    result = capture_check.evaluate_phase("static_pixels", [cached, cached.copy()])

    assert result["status"] == "fail"
    assert result["result_subtype"] == "not_demonstrated"


def test_static_phase_ignores_frames_captured_before_freeze() -> None:
    queued = _sample(sequence=1)
    queued["captured_in_phase"] = False
    queued["marker"]["counter"] = 99
    frozen = [_sample(sequence=2), _sample(sequence=3)]
    result = capture_check.evaluate_phase("static_pixels", [queued, *frozen])
    assert result["status"] == "pass"
    assert result["valid_frame_count"] == 2
    assert capture_check.evaluate_phase("static_pixels", [queued])["status"] != "pass"


def test_phase_evidence_rejects_old_frames_and_unconfirmed_occlusion() -> None:
    old = _sample(sequence=1)
    old["valid"] = False
    assert capture_check.evaluate_phase("restored", [old])["status"] == "fail"
    assert capture_check.evaluate_phase("occluded", [_sample()])["status"] == "fail"
    assert (
        capture_check.evaluate_phase(
            "target_closed",
            [{"frame_received": False, "status": {"valid_frame": True}}],
        )["status"]
        == "fail"
    )


def test_invalidation_phases_require_native_state_evidence() -> None:
    no_frame = {"frame_received": False, "valid": False, "status": {"valid_frame": False}}

    minimized = capture_check.evaluate_phase("minimized", [no_frame])
    closed = capture_check.evaluate_phase("target_closed", [no_frame])

    assert minimized["status"] == "unverified"
    assert closed["status"] == "unverified"


@pytest.mark.parametrize(
    "args",
    [
        ["--capture-check", "--simulator"],
        ["--capture-check", "--verify-simulator"],
        ["--capture-check", "--smoke-test"],
        ["--capture-check", "--allow-real-input"],
        ["--capture-check-output", "evidence.json"],
        ["--capture-backend-check", "available"],
        ["--capture-check", "--capture-backend-check", "available"],
    ],
)
def test_capture_cli_rejects_conflicting_or_unscoped_flags(args: list[str]) -> None:
    with pytest.raises(SystemExit):
        app._arguments(args)


def test_main_routes_capture_check_without_starting_the_desktop_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    output = tmp_path / "probe.json"
    calls = []
    monkeypatch.setattr(
        capture_check, "main", lambda argv: calls.append(argv) or 17
    )

    assert app.main(["--capture-check", "--capture-check-output", str(output)]) == 17
    assert calls == [["--output", str(output)]]


def test_capture_backend_smoke_probe_never_creates_target_or_attempts_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = []

    def probe(*, force_unavailable: bool = False) -> tuple[bool, str]:
        calls.append(force_unavailable)
        return (not force_unavailable, "test probe result")

    monkeypatch.setattr(capture, "probe_wgc_support", probe)

    assert app._check_capture_backend("available") is True
    available = capsys.readouterr().out
    assert '"target_created": false' in available
    assert '"capture_attempted": false' in available
    assert '"input_attempted": false' in available

    assert app._check_capture_backend("unavailable") is True
    unavailable = capsys.readouterr().out
    assert '"available": false' in unavailable
    assert calls == [False, True]


def test_capture_check_keeps_an_unavailable_capability_result_without_opening_a_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    output = tmp_path / "capture-check.json"
    monkeypatch.setattr(
        capture_check, "probe_wgc_support", lambda: (False, "test capability unavailable")
    )

    assert capture_check.main(["--output", str(output)]) == 1

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["result"] == "unverified"
    assert report["clean_teardown"] is True
    assert report["capability"] == {
        "available": False,
        "reason": "test capability unavailable",
    }
    assert all(phase["status"] == "unverified" for phase in report["phases"])


def test_restored_phase_unfreezes_marker_and_uses_nonactivating_restore(qtbot, monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "auto_ready.simulator._show_window",
        lambda hwnd, command: calls.append((int(hwnd), command)),
    )
    app_instance = capture_check.QApplication.instance()
    runner = capture_check.CaptureCheckRunner(app_instance, None)
    target = runner.target
    assert target is not None
    qtbot.addWidget(target)
    target.show()
    target.marker.set_frozen(True)
    starting_counter = target.marker.counter
    monkeypatch.setattr(runner, "_schedule_next_capture", lambda delay_ms=180: None)

    runner._start_phase(4)

    assert target.marker._frozen is False
    assert calls and calls[-1][1] == 4
    qtbot.waitUntil(lambda: target.marker.counter > starting_counter, timeout=1000)


def test_process_rss_uses_builtin_windows_working_set_counter(monkeypatch) -> None:
    class FakeProcedure:
        def __init__(self, result=None):
            self.result = result

        def __call__(self, *args):
            if self.result is not None:
                return self.result
            counters = args[1]._obj
            counters.WorkingSetSize = 123456
            return 1

    kernel32 = type("FakeKernel32", (), {"GetCurrentProcess": FakeProcedure(0x44)})()
    psapi = type("FakePsapi", (), {"GetProcessMemoryInfo": FakeProcedure()})()
    monkeypatch.setattr(capture_check.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **_kwargs: kernel32 if name == "kernel32" else psapi,
        raising=False,
    )

    assert capture_check._process_rss_bytes() == 123456
