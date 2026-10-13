from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from auto_ready.models import Geometry, Identity, State
from auto_ready.verification_record import VerificationRecorder


def _read_summary(recorder: VerificationRecorder) -> dict:
    recorder.close()
    return json.loads(recorder.summary_path.read_text(encoding="utf-8"))


def test_receipts_native_results_and_image_verdict_remain_separate(tmp_path):
    recorder = VerificationRecorder(tmp_path)
    identity = Identity(11, 22, 33, r"C:\sim\python.exe", "Auto Ready 模拟器")
    geometry = Geometry(100, 200, 640, 480, 144)
    recorder.record(
        "run_started", allow_real_input=True, system_input_status="NOT_EXECUTED"
    )

    # Preparation clicks stay run-level and cannot become round receipts.
    recorder.record(
        "simulator_button_clicked",
        button_id="ready",
        source="test_fake",
        spontaneous=False,
    )
    recorder.record("state", session_id="round-1", state=State.MONITORING, reason="armed")
    recorder.record(
        "round_started",
        session_id="round-1",
        identity=identity,
        geometry=geometry,
        mode="自动确认",
        success_template=True,
    )
    recorder.record(
        "input_attempt",
        session_id="round-1",
        point=(250, 300),
        geometry=geometry,
        frame_sequence=4,
        captured_at=1.25,
    )
    recorder.record("system_input_result", requested=3, sent=3, error=None, cleanup=False)
    recorder.record("input_result", session_id="round-1", ok=True, error="")
    recorder.record(
        "success_detection",
        session_id="round-1",
        stage="after_input",
        candidates=0,
        score=0.0,
        reason="无匹配",
        frame_sequence=5,
        captured_at=1.4,
        geometry=geometry,
    )
    recorder.record("state", session_id="round-1", state=State.UNCONFIRMED, reason="timeout")
    recorder.record(
        "state", session_id="round-1", state=State.DISARMED, reason="application closed"
    )
    recorder.record(
        "simulator_button_clicked",
        button_id="ready",
        source="auto",
        spontaneous=True,
    )

    # A widget event in a later session is evidence in that session only.
    recorder.record("state", session_id="round-2", state=State.MONITORING, reason="armed")
    recorder.record(
        "round_started",
        session_id="round-2",
        identity=identity,
        geometry=geometry,
        mode="自动确认",
        success_template=False,
    )
    recorder.record(
        "simulator_button_mouse_press",
        phase="mouse_press",
        button_id="ready",
        source="test_fake",
        spontaneous=True,
    )
    recorder.record(
        "simulator_button_clicked",
        button_id="ready",
        source="test_fake",
        spontaneous=True,
    )

    summary = _read_summary(recorder)
    first, second = summary["sessions"]
    assert first["session_id"] == "round-1"
    assert first["engine_input_attempt_count"] == 1
    assert first["system_input_result_count"] == 1
    assert first["system_input_status"] == "OS_INPUT_RESULT_RECORDED"
    assert first["system_input_results"][0]["sent"] == 3
    assert first["widget_events"]["clicked_signal"] == 1
    assert first["widget_events"]["late_or_ambiguous_count"] == 1
    assert first["widget_receipt_status"] == "WIDGET_CLICK_SIGNAL_OBSERVED_CAUSATION_UNCLAIMED"
    assert first["image_verdict_state"] == State.UNCONFIRMED.value
    assert first["image_verdict_status"] == "ENGINE_RESULT_UNCONFIRMED"
    assert first["engine_latest_state"] == State.DISARMED.value
    assert "ambiguous" in first["widget_correlation"]

    assert second["session_id"] == "round-2"
    assert second["engine_input_attempt_count"] == 0
    assert second["system_input_status"] == "UNVERIFIED_NO_OS_RESULT"
    assert second["widget_events"]["mouse_press"] == 1
    assert second["widget_events"]["clicked_signal"] == 1
    assert second["widget_events"]["sources"] == {"test_fake": 2}
    assert second["widget_receipt_status"] == "WIDGET_CLICK_SIGNAL_OBSERVED_CAUSATION_UNCLAIMED"
    assert second["widget_correlation"].startswith("Temporal assignment")
    assert second["image_verdict_status"] == "UNVERIFIED_NO_IMAGE_VERDICT"

    assert summary["assessment"] == "EVIDENCE_ONLY_UNVERIFIED; counts do not establish success"
    assert summary["real_input"]["execution_status"] == "OS_INPUT_RESULT_RECORDED"
    assert summary["run"]["system_input_status"] == "OS_INPUT_RESULT_RECORDED"
    assert "PASS" not in json.dumps(summary)

    events = [
        json.loads(line) for line in recorder.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["sequence"] for event in events] == list(range(len(events)))
    assert all("monotonic_ns" in event for event in events)
    assert events[0].get("session_id") is None


def test_missing_real_input_is_explicitly_not_executed_and_events_are_bounded(tmp_path):
    recorder = VerificationRecorder(tmp_path, max_events=3)
    assert recorder.record(
        "run_started", allow_real_input=False, system_input_status="NOT_EXECUTED"
    )
    assert recorder.record("round_started", session_id="round", mode="自动确认")
    assert recorder.record("state", session_id="round", state=State.MONITORING)
    assert not recorder.record("input_attempt", session_id="round", point=(1, 2))

    summary = _read_summary(recorder)
    assert summary["real_input"]["explicitly_allowed"] is False
    assert summary["real_input"]["execution_status"] == "NOT_EXECUTED"
    assert summary["run"]["system_input_status"] == "NOT_EXECUTED"
    assert summary["sessions"][0]["system_input_status"] == "NOT_EXECUTED"
    assert summary["dropped_event_count"] == 1
    assert summary["event_count"] == 3


def test_allowed_input_without_os_result_is_unverified(tmp_path):
    recorder = VerificationRecorder(tmp_path)
    recorder.record(
        "run_started", allow_real_input=True, system_input_status="NOT_EXECUTED"
    )
    recorder.record("round_started", session_id="round-1", mode="自动确认")

    summary = _read_summary(recorder)

    assert summary["real_input"]["execution_status"] == "UNVERIFIED_NO_OS_RESULT"
    assert summary["sessions"][0]["system_input_status"] == "UNVERIFIED_NO_OS_RESULT"
    assert summary["run"]["system_input_status"] == "UNVERIFIED_NO_OS_RESULT"


@pytest.mark.parametrize("loss_mode", ["event_limit", "write_failure"])
def test_allowed_input_with_missing_os_result_record_is_unverified(
    tmp_path, monkeypatch, loss_mode: str
):
    max_events = 2 if loss_mode == "event_limit" else 5
    recorder = VerificationRecorder(tmp_path, max_events=max_events)
    recorder.record(
        "run_started", allow_real_input=True, system_input_status="NOT_EXECUTED"
    )
    recorder.record("round_started", session_id="round-1", mode="自动确认")

    if loss_mode == "write_failure":
        original_open = Path.open

        def fail_result_write(path, mode="r", *args, **kwargs):
            if path == recorder.events_path and mode == "ab":
                raise OSError("simulated event-log write failure")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail_result_write)

    assert not recorder.record(
        "system_input_result", requested=1, sent=1, error=None, cleanup=False
    )
    summary = _read_summary(recorder)

    assert summary["real_input"]["execution_status"] == "UNVERIFIED_NO_OS_RESULT"
    assert summary["sessions"][0]["system_input_status"] == "UNVERIFIED_NO_OS_RESULT"
    assert summary["run"]["system_input_status"] == "UNVERIFIED_NO_OS_RESULT"
    if loss_mode == "event_limit":
        assert summary["dropped_event_count"] == 1
    else:
        assert summary["write_error_count"] == 1


def test_image_buffers_are_omitted_from_event_log(tmp_path):
    recorder = VerificationRecorder(tmp_path)
    pixels = np.full((20, 30, 3), 255, dtype=np.uint8)
    assert recorder.record("test_payload", pixels=pixels)
    recorder.close()

    event = json.loads(recorder.events_path.read_text(encoding="utf-8"))
    assert event["pixels"] == {"omitted": "ndarray", "shape": [20, 30, 3]}
    assert recorder.summary_path.exists()
