"""Bounded local evidence recording for the opt-in simulator verifier."""

from __future__ import annotations

import dataclasses
import json
import math
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

_MAX_EVENT_BYTES = 32 * 1024
_MAX_SUMMARY_ITEMS = 100


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _safe(value: Any, depth: int = 0) -> Any:
    """Convert observer values without serializing image buffers or arbitrary objects."""
    if depth > 8:
        return "<maximum depth omitted>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:2048]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _safe(value.value, depth + 1)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _safe(getattr(value, field.name), depth + 1)
            for field in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 128:
                result["<omitted>"] = "additional mapping entries omitted"
                break
            result[str(key)[:128]] = _safe(item, depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
        result = [_safe(item, depth + 1) for item in values[:128]]
        if len(values) > 128:
            result.append(f"<omitted {len(values) - 128} items>")
        return result
    if hasattr(value, "shape") and hasattr(value, "dtype"):
        return {"omitted": type(value).__name__, "shape": _safe(getattr(value, "shape", None))}
    return f"<{type(value).__name__} omitted>"


def _new_session(session_id: str, started_at: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "started_at": started_at,
        "mode": fields.get("mode"),
        "success_template": fields.get("success_template"),
        "identity": fields.get("identity"),
        "geometry": fields.get("geometry"),
        "terminal_outcome": None,
        "engine_states": [],
        "engine_input_attempts": [],
        "engine_input_attempt_count": 0,
        "engine_input_results": [],
        "engine_input_result_count": 0,
        "system_input_results": [],
        "system_input_result_count": 0,
        "system_requested_count": 0,
        "system_sent_count": 0,
        "image_verdicts": [],
        "widget_events": {
            "mouse_press": 0,
            "mouse_release": 0,
            "clicked_signal": 0,
            "late_or_ambiguous_count": 0,
            "sources": {},
            "buttons": {},
        },
    }


class VerificationRecorder:
    """Append ordered JSONL observer events and finalize a non-judgmental summary."""

    def __init__(self, root: Path, *, max_events: int = 5000):
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self.run_id = uuid.uuid4().hex
        self.run_dir = Path(root) / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.events_path = self.run_dir / "events.jsonl"
        self.summary_path = self.run_dir / "summary.json"
        self._max_events = max_events
        self._lock = threading.RLock()
        self._closed = False
        self._sequence = 0
        self._event_count = 0
        self._dropped_count = 0
        self._write_error_count = 0
        self._run_metadata: dict[str, Any] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._active_session: str | None = None
        self._active_session_terminal = False
        self._created_at = _now()

    @property
    def observer(self):
        """Callable alias suitable for Engine.set_observer and WindowsInput observers."""
        return self.record

    def __call__(self, event: str, **fields: Any) -> bool:
        return self.record(event, **fields)

    def record(self, event: str, **fields: Any) -> bool:
        """Write one bounded event; evidence failures never escape into input paths."""
        with self._lock:
            if self._closed:
                return False
            if self._event_count >= self._max_events:
                self._dropped_count += 1
                return False
            if not isinstance(event, str) or not event or len(event) > 128:
                self._dropped_count += 1
                return False

            event_fields = dict(fields)
            session_value = event_fields.get("session_id")
            if event == "round_started" and isinstance(session_value, str):
                self._active_session = session_value
                self._active_session_terminal = False
            elif session_value is None and (
                event.startswith("simulator_button_")
                or event in {"system_input_result", "input_result"}
            ):
                session_value = self._active_session
                if session_value is not None:
                    event_fields["session_id"] = session_value
                    if event.startswith("simulator_button_"):
                        event_fields["session_assignment"] = (
                            "late_after_terminal_ambiguous"
                            if self._active_session_terminal
                            else "active_session_temporal_only"
                        )
            safe_fields = _safe(event_fields)
            if not isinstance(safe_fields, dict):
                self._dropped_count += 1
                return False
            timestamp = _now()
            record = {
                **safe_fields,
                "sequence": self._sequence,
                "timestamp_utc": timestamp,
                "monotonic_ns": time.monotonic_ns(),
                "run_id": self.run_id,
                "event": event,
            }
            try:
                line = json.dumps(
                    record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                encoded = (line + "\n").encode("utf-8")
                if len(encoded) > _MAX_EVENT_BYTES:
                    self._dropped_count += 1
                    return False
                with self.events_path.open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
            except Exception:
                self._write_error_count += 1
                return False

            self._sequence += 1
            self._event_count += 1
            self._update_summary(event, safe_fields, timestamp)
            if event == "run_started":
                self._run_metadata = safe_fields
            if (
                event == "state"
                and safe_fields.get("state") in {"成功", "结果未确认", "错误", "未启用"}
                and safe_fields.get("session_id") == self._active_session
            ):
                self._active_session_terminal = True
            return True

    def _update_summary(self, event: str, fields: dict[str, Any], timestamp: str) -> None:
        session_id = fields.get("session_id")
        if event == "round_started" and isinstance(session_id, str):
            previous_states = self._sessions.get(session_id, {}).get("engine_states", [])
            session = _new_session(session_id, timestamp, fields)
            session["engine_states"].extend(previous_states)
            self._sessions[session_id] = session
        if not isinstance(session_id, str) or not session_id:
            return
        session = self._sessions.setdefault(session_id, _new_session(session_id, timestamp, fields))
        if event == "state":
            state = fields.get("state")
            if state in {"成功", "结果未确认", "错误"}:
                session["terminal_outcome"] = state
            _bounded_append(
                session["engine_states"],
                {
                    "timestamp_utc": timestamp,
                    "state": fields.get("state"),
                    "reason": fields.get("reason"),
                },
            )
        elif event == "input_attempt":
            session["engine_input_attempt_count"] += 1
            _bounded_append(
                session["engine_input_attempts"],
                {
                    key: fields.get(key)
                    for key in ("point", "geometry", "frame_sequence", "captured_at")
                },
            )
        elif event == "input_result":
            session["engine_input_result_count"] += 1
            _bounded_append(
                session["engine_input_results"],
                {
                    "ok": fields.get("ok"),
                    "error": fields.get("error"),
                },
            )
        elif event == "system_input_result":
            session["system_input_result_count"] += 1
            session["system_requested_count"] += _number(fields.get("requested"))
            session["system_sent_count"] += _number(fields.get("sent"))
            _bounded_append(
                session["system_input_results"],
                {key: fields.get(key) for key in ("requested", "sent", "error", "cleanup")},
            )
        elif event == "success_detection":
            _bounded_append(
                session["image_verdicts"],
                {
                    key: fields.get(key)
                    for key in (
                        "stage",
                        "candidates",
                        "score",
                        "reason",
                        "frame_sequence",
                        "captured_at",
                        "geometry",
                    )
                },
            )
        elif event.startswith("simulator_button_mouse_") or event == "simulator_button_clicked":
            receipt = session["widget_events"]
            phase = fields.get("phase")
            if event == "simulator_button_clicked":
                phase = "clicked_signal"
            elif phase not in {"mouse_press", "mouse_release"}:
                phase = "mouse_press" if event.endswith("press") else "mouse_release"
            receipt[phase] += 1
            if fields.get("session_assignment") == "late_after_terminal_ambiguous":
                receipt["late_or_ambiguous_count"] += 1
            source = str(fields.get("source", "unknown"))[:128]
            button_id = str(fields.get("button_id", "unknown"))[:128]
            receipt["sources"][source] = receipt["sources"].get(source, 0) + 1
            receipt["buttons"][button_id] = receipt["buttons"].get(button_id, 0) + 1

    def close(self) -> None:
        """Stop accepting events and atomically write the current evidence summary."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            summary = self._make_summary(_now())
            temporary = self.summary_path.with_suffix(".json.tmp")
            try:
                data = json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(data)
                    stream.flush()
                os.replace(temporary, self.summary_path)
            except Exception:
                self._write_error_count += 1
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _make_summary(self, closed_at: str) -> dict[str, Any]:
        sessions = []
        all_system_results = []
        authorization = bool(self._run_metadata.get("allow_real_input", False))
        for session in self._sessions.values():
            results = session["system_input_results"]
            all_system_results.extend(results)
            widget = session["widget_events"]
            if widget["clicked_signal"]:
                receipt_status = "WIDGET_CLICK_SIGNAL_OBSERVED_CAUSATION_UNCLAIMED"
            elif widget["mouse_press"] or widget["mouse_release"]:
                receipt_status = "WIDGET_MOUSE_EVENT_OBSERVED_NO_CLICK_SIGNAL"
            else:
                receipt_status = "UNVERIFIED_NO_WIDGET_RECEIPT"
            latest_state = (
                session["engine_states"][-1]["state"] if session["engine_states"] else None
            )
            outcome = session["terminal_outcome"]
            if outcome == "成功":
                image_status = "ENGINE_SUCCESS_STATE_OBSERVED"
            elif outcome == "结果未确认":
                image_status = "ENGINE_RESULT_UNCONFIRMED"
            elif outcome == "错误":
                image_status = "ENGINE_TERMINAL_ERROR_OBSERVED"
            elif session["image_verdicts"]:
                image_status = "IMAGE_DETECTIONS_RECORDED_NO_TERMINAL_VERDICT"
            else:
                image_status = "UNVERIFIED_NO_IMAGE_VERDICT"
            sessions.append(
                {
                    **session,
                    "system_input_status": (
                        "OS_INPUT_RESULT_RECORDED"
                        if session["system_input_result_count"]
                        else (
                            "UNVERIFIED_NO_OS_RESULT" if authorization else "NOT_EXECUTED"
                        )
                    ),
                    "widget_receipt_status": receipt_status,
                    "widget_correlation": (
                        "Late receipt timing is ambiguous after a terminal state; "
                        "all assignments are temporal and causation is not inferred."
                        if widget["late_or_ambiguous_count"]
                        else "Temporal assignment to the active session; "
                        "input causation is not inferred."
                    ),
                    "engine_latest_state": latest_state,
                    "image_verdict_state": outcome,
                    "image_verdict_status": image_status,
                    "assessment": "EVIDENCE_ONLY",
                }
            )
        execution_status = (
            "OS_INPUT_RESULT_RECORDED"
            if all_system_results
            else ("UNVERIFIED_NO_OS_RESULT" if authorization else "NOT_EXECUTED")
        )
        run_metadata = dict(self._run_metadata)
        if run_metadata.get("system_input_status") == "NOT_EXECUTED":
            run_metadata["system_input_status"] = execution_status
        requested = sum(session["system_requested_count"] for session in self._sessions.values())
        sent = sum(session["system_sent_count"] for session in self._sessions.values())
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "created_at_utc": self._created_at,
            "closed_at_utc": closed_at,
            "events_path": str(self.events_path),
            "event_count": self._event_count,
            "dropped_event_count": self._dropped_count,
            "write_error_count": self._write_error_count,
            "run": run_metadata,
            "real_input": {
                "explicitly_allowed": authorization,
                "execution_status": execution_status,
                "requested_event_count": requested,
                "sent_event_count": sent,
                "system_input_results": all_system_results,
                "correlation": (
                    "Windows input API returns and simulator widget events are independent "
                    "observations; neither is inferred from the other."
                ),
            },
            "sessions": sessions,
            "assessment": "EVIDENCE_ONLY_UNVERIFIED; counts do not establish success",
        }


def _bounded_append(values: list, item: Any) -> None:
    if len(values) < _MAX_SUMMARY_ITEMS:
        values.append(item)


def _number(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value):
        return 0
    return max(0, int(value))
