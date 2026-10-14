"""Zero-input, own-window Windows Graphics Capture acceptance probe."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from .capture import probe_wgc_support
from .simulator import CaptureOccluderWindow, CaptureProbeWindow

_PHASES = (
    ("non_foreground", 2.0),
    ("occluded", 2.0),
    ("static_pixels", 1.0),
    ("minimized", 1.0),
    ("restored", 2.0),
    ("release_rebuild", 2.0),
    ("target_closed", 1.0),
)
_POLL_INTERVAL_MS = 180
_CORNER_BGR = {
    "top_left": np.array((36, 36, 255), dtype=np.float32),
    "top_right": np.array((112, 224, 22), dtype=np.float32),
    "bottom_left": np.array((255, 125, 40), dtype=np.float32),
    "bottom_right": np.array((40, 219, 255), dtype=np.float32),
}


def decode_marker(image: np.ndarray) -> dict[str, Any]:
    """Read the simulator's corner colors and 16-bit counter from a BGR frame."""
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] < 3:
        return {"valid": False, "corners": {}, "counter": None, "reason": "invalid image shape"}
    height, width = image.shape[:2]
    if width < 80 or height < 50:
        return {"valid": False, "corners": {}, "counter": None, "reason": "image too small"}
    pixels = image[:, :, :3].astype(np.float32, copy=False)
    side = max(8, int(min(width, height) * 0.42))
    margin_x = max(3, int(width * 0.025))
    margin_y = max(3, int(height * 0.08))
    points = {
        "top_left": (margin_x + side // 2, margin_y + side // 2),
        "top_right": (width - margin_x - side // 2, margin_y + side // 2),
        "bottom_left": (margin_x + side // 2, height - margin_y - side // 2),
        "bottom_right": (width - margin_x - side // 2, height - margin_y - side // 2),
    }
    corners: dict[str, bool] = {}
    for name, (x, y) in points.items():
        sample = pixels[max(0, y - 3) : min(height, y + 4), max(0, x - 3) : min(width, x + 4)]
        mean = sample.mean(axis=(0, 1))
        corners[name] = bool(np.max(np.abs(mean - _CORNER_BGR[name])) <= 72)

    cell_w = max(2, int(width * 0.031))
    gap = max(1, int(width * 0.009))
    total_w = 16 * cell_w + 15 * gap
    start_x = (width - total_w) // 2
    counter = 0
    for index in range(16):
        x = start_x + index * (cell_w + gap) + cell_w // 2
        y = height // 2
        sample = pixels[max(0, y - 3) : min(height, y + 4), max(0, x - 3) : min(width, x + 4)]
        counter = (counter << 1) | int(float(sample.mean()) > 160)

    valid = all(corners.values())
    return {
        "valid": valid,
        "corners": corners,
        "counter": counter if valid else None,
        "reason": "marker decoded" if valid else "one or more color corners did not match",
    }


def evaluate_phase(
    name: str, samples: list[dict[str, Any]], *, generation_before_release: int | None = None
) -> dict[str, Any]:
    """Return a conservative, evidence-backed result for one probe condition."""
    usable = [
        sample for sample in samples
        if sample.get("valid") and sample.get("captured_in_phase", True)
    ]
    all_statuses = [sample.get("status") or {} for sample in samples]
    result: dict[str, Any] = {"condition": name, "status": "unverified", "reason": "no samples"}

    if name in {"minimized", "target_closed"}:
        if not samples:
            return result
        no_frames = all(
            sample.get("frame_received") is False
            and sample.get("valid") is False
            and status.get("valid_frame") is False
            for sample, status in zip(samples, all_statuses, strict=True)
        )
        if not no_frames:
            result.update(status="fail", reason="a frame remained after invalidation")
            return result
        evidence_key = "minimized" if name == "minimized" else "target_exists"
        expected = True if name == "minimized" else False
        observed = (
            [status.get(evidence_key) for status in all_statuses]
            if name == "minimized"
            else [sample.get(evidence_key) for sample in samples]
        )
        if all(value is expected for value in observed):
            reason = (
                "native status confirmed the target was minimized and returned no frames"
                if name == "minimized"
                else "IsWindow confirmed the target was closed and returned no frames"
            )
            result.update(status="pass", reason=reason)
        elif any(value is not None for value in observed):
            result.update(
                status="fail",
                reason=f"native status did not confirm {evidence_key}={str(expected).lower()}",
            )
        else:
            result.update(
                status="unverified", reason=f"native status did not report {evidence_key}"
            )
        return result

    if name == "static_pixels":
        counters = {sample.get("marker", {}).get("counter") for sample in usable}
        sequences = {sample.get("source_sequence") for sample in usable}
        if len(usable) >= 2 and len(sequences) >= 2:
            if len(counters) == 1:
                result.update(
                    status="pass",
                    result_subtype="fresh_frames_unchanged_marker",
                    fresh_frame_demonstration="pass",
                    reason="new source frames carried the same frozen marker pixels",
                )
            else:
                result.update(
                    status="fail",
                    result_subtype="fresh_frames_changed_marker",
                    fresh_frame_demonstration="fail",
                    reason="fresh source frames did not carry stable frozen marker pixels",
                )
        else:
            # A pre-freeze frame establishes that this source was alive; it
            # cannot demonstrate fresh frozen pixels. Subsequent pending reads
            # may still prove that a silent source is not reissued as fresh.
            known_source_samples = [
                sample
                for sample in samples
                if sample.get("valid") is True
                and sample.get("frame_received") is True
                and sample.get("new_source_frame") is True
            ]
            suppressed_samples = [sample for sample in samples if _source_suppressed(sample)]
            if known_source_samples and suppressed_samples:
                result.update(
                    status="pass",
                    result_subtype="source_updates_suppressed_stale_cache_withheld",
                    fresh_frame_demonstration="unverified",
                    reason=(
                        "WGC supplied no new source updates and the backend withheld stale frames; "
                        "a fresh unchanged-pixel demonstration remains unverified"
                    ),
                )
            else:
                result.update(
                    status="fail" if samples else "unverified",
                    result_subtype="not_demonstrated",
                    fresh_frame_demonstration="unverified",
                    reason=(
                        "fresh unchanged pixels or explicit stale-frame withholding "
                        "were not demonstrated"
                    ),
                )
        result["valid_frame_count"] = len(usable)
        return result

    if not usable:
        if samples:
            result.update(
                status="fail", reason="no fresh, decoded WGC frame matched the own-window marker"
            )
        return result

    if name == "non_foreground":
        if any((sample.get("status") or {}).get("foreground") is False for sample in usable):
            result.update(
                status="pass",
                reason="a fresh own-window frame arrived while the target was not foreground",
            )
        else:
            result.update(
                status="fail",
                reason="the target was foreground or its state was unknown for every frame",
            )
    elif name == "occluded":
        if any((sample.get("status") or {}).get("obscured") is True for sample in usable):
            result.update(
                status="pass",
                reason="the own marker remained readable while the target was reported obscured",
            )
        else:
            result.update(
                status="fail",
                reason="the target was not confirmed obscured while a valid frame arrived",
            )
    elif name == "release_rebuild":
        if generation_before_release is not None and any(
            int(sample.get("generation", -1)) > generation_before_release for sample in usable
        ):
            result.update(
                status="pass", reason="a valid frame arrived in a newer capture generation"
            )
        else:
            result.update(status="fail", reason="a newer capture generation was not demonstrated")
    else:
        result.update(status="pass", reason=f"{len(usable)} fresh own-window frame(s) were decoded")
    result["valid_frame_count"] = len(usable)
    return result


def _source_suppressed(sample: dict[str, Any]) -> bool:
    status = sample.get("status") or {}
    return bool(
        sample.get("frame_received") is False
        and sample.get("new_source_frame") is False
        and sample.get("error_type") == "CapturePendingError"
        and status.get("backend") == "wgc"
        and status.get("valid_frame") is False
        and status.get("fresh") is False
        and status.get("minimized") is False
        and status.get("source_sequence") is None
    )


def _is_window(hwnd: int) -> bool | None:
    """Return whether a known HWND still exists, or None if Windows cannot answer."""
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        is_window = user32.IsWindow
        is_window.argtypes = [ctypes.c_void_p]
        is_window.restype = ctypes.c_int
        return bool(is_window(ctypes.c_void_p(hwnd)))
    except Exception:
        return None


def _plain(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _process_rss_bytes() -> int | None:
    """Return RSS from Windows process counters, with psutil as a portable fallback."""
    if sys.platform == "win32":
        try:
            class _ProcessMemoryCountersEx(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_uint32),
                    ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            current_process = kernel32.GetCurrentProcess
            current_process.restype = ctypes.c_void_p
            handle = current_process()
            counters = _ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            get_process_memory_info = psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ProcessMemoryCountersEx),
                ctypes.c_uint32,
            ]
            get_process_memory_info.restype = ctypes.c_int
            if get_process_memory_info(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            pass
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        return None


class _BackendWorker(QThread):
    completed = Signal(int, object)

    def __init__(self):
        super().__init__()
        self._requests: queue.Queue[tuple[int, str, dict[str, Any]]] = queue.Queue()
        self._backend = None
        self._identity = None

    def submit(self, request_id: int, operation: str, **payload: Any) -> None:
        self._requests.put((request_id, operation, payload))

    def run(self) -> None:
        while True:
            request_id, operation, payload = self._requests.get()
            try:
                result = self._perform(operation, payload)
            except BaseException as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
            self.completed.emit(request_id, result)
            if operation == "close":
                return

    def _perform(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == "initialize":
            from .native import WindowsBackend

            self._backend = WindowsBackend()
            self._backend.set_capture_method("wgc")
            hwnd, pid = int(payload["hwnd"]), int(payload["pid"])
            matches = [
                identity
                for identity in self._backend.list_windows()
                if int(identity.hwnd) == hwnd and int(identity.pid) == pid
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "The marker HWND did not map to one exact current-process identity"
                )
            self._identity = matches[0]
            geometry = self._backend.inspect(self._identity)
            foreground = self._backend.foreground(self._identity)
            status = self._backend.capture_status()
            return {
                "identity": asdict(self._identity),
                "geometry": asdict(geometry),
                "foreground_before": foreground,
                "status": asdict(status),
            }

        if self._backend is None:
            if operation == "close":
                return {"closed": True, "close_clean": True, "status": None}
            raise RuntimeError("The native capture backend was not initialized")

        if operation == "capture":
            target_exists = (
                _is_window(int(self._identity.hwnd))
                if payload.get("condition") == "target_closed"
                else None
            )
            try:
                frame = self._backend.capture(self._identity, payload["session_id"])
                status = self._backend.capture_status()
                image = frame.image
                marker = decode_marker(image)
                shape_matches = bool(
                    image.ndim == 3
                    and image.shape[:2] == (frame.geometry.height, frame.geometry.width)
                )
                identity_matches = frame.identity == self._identity
                source_sequence = getattr(frame, "source_sequence", None)
                source_time = getattr(frame, "source_time_100ns", None)
                generation = int(getattr(frame, "capture_generation", 0))
                details = {
                    "frame_received": True,
                    "frame_sequence": int(frame.sequence),
                    "source_sequence": source_sequence,
                    "source_time_100ns": source_time,
                    "generation": generation,
                    "backend": getattr(frame, "backend", None),
                    "geometry": asdict(frame.geometry),
                    "image_shape": list(image.shape),
                    "shape_matches": shape_matches,
                    "identity_matches": identity_matches,
                    "marker": marker,
                    "captured_at": float(frame.captured_at),
                    "received_at": getattr(frame, "received_at", None),
                    "capture_latency_ms": status.capture_ms,
                    "frame_age_seconds": status.frame_age,
                    "target_exists": target_exists,
                }
                details["valid"] = bool(
                    status.valid_frame
                    and status.fresh
                    and details["backend"] == "wgc"
                    and source_sequence is not None
                    and source_time is not None
                    and shape_matches
                    and identity_matches
                    and marker["valid"]
                )
                return {**details, "status": asdict(status)}
            except Exception as exc:
                try:
                    status = self._backend.capture_status()
                    status_dict = asdict(status)
                except Exception as status_exc:
                    status_dict = {"reason": f"capture_status failed: {status_exc}"}
                return {
                    "frame_received": False,
                    "valid": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_type": type(exc).__name__,
                    "target_exists": target_exists,
                    "status": status_dict,
                }

        if operation == "release":
            before = self._backend.capture_status()
            self._backend.release_capture()
            after = self._backend.capture_status()
            return {
                "generation_before_release": before.generation,
                "generation_after_release": after.generation,
                "status": asdict(after),
            }

        if operation == "close":
            error = None
            try:
                self._backend.close()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            try:
                status = asdict(self._backend.capture_status())
            except Exception as exc:
                status = {"reason": f"capture_status after close failed: {exc}"}
            return {
                "closed": error is None,
                "close_clean": error is None,
                "error": error,
                "status": status,
            }

        raise RuntimeError(f"Unknown capture-check worker operation: {operation}")


class CaptureCheckRunner(QObject):
    """GUI-thread phase coordinator; native capture calls stay in one worker."""

    def __init__(self, app: QApplication, output_path: Path | None):
        super().__init__(app)
        self.app = app
        self.output_path = output_path
        self.target: CaptureProbeWindow | None = CaptureProbeWindow()
        self.occluder: CaptureOccluderWindow | None = None
        self.worker = _BackendWorker()
        self.worker.completed.connect(self._on_completed)
        self.phase_index = -1
        self.phase_started = 0.0
        self.current_phase: dict[str, Any] | None = None
        self.results: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []
        self.request_id = 0
        self.pending = False
        self.release_generation: int | None = None
        self.identity: dict[str, Any] | None = None
        self.geometry: dict[str, Any] | None = None
        self.foreground_before: bool | None = None
        self.foreground_after: bool | None = None
        self.close_clean: bool | None = None
        self.shutdown_started = False
        self.started_cpu = time.process_time()
        self.started_rss = _process_rss_bytes()
        self.started_at = time.time()
        self.session_id = f"capture-check-{uuid.uuid4()}"
        self.exit_code = 1
        self._last_source: dict[int, tuple[int, int]] = {}

    def start(self) -> None:
        if self.target is None:
            self._shutdown()
            return
        self.target.show()
        self.target.lower()
        hwnd = int(self.target.winId())
        self.worker.start()
        self._send("initialize", hwnd=hwnd, pid=os.getpid())

    def _send(self, operation: str, **payload: Any) -> None:
        self.request_id += 1
        self.pending = True
        self.worker.submit(self.request_id, operation, **payload)

    @Slot(int, object)
    def _on_completed(self, request_id: int, value: object) -> None:
        if request_id != self.request_id:
            return
        self.pending = False
        result = value if isinstance(value, dict) else {"error": "invalid worker response"}
        if self.shutdown_started:
            self.close_clean = bool(result.get("close_clean", False))
            self._finish()
            return
        if self.phase_index == -1:
            if result.get("error"):
                self.results.append(
                    {
                        "condition": "initialization",
                        "status": "fail",
                        "reason": result["error"],
                    }
                )
                self._shutdown()
                return
            self.identity = _plain(result.get("identity"))
            self.geometry = _plain(result.get("geometry"))
            self.foreground_before = result.get("foreground_before")
            self.results.append(
                {
                    "condition": "no_activation",
                    "status": (
                        "fail"
                        if self.foreground_before is True
                        else "pass"
                        if self.foreground_before is False
                        else "unverified"
                    ),
                    "reason": (
                        "the own marker window was foreground after show/lower"
                        if self.foreground_before is True
                        else "the own marker window was not foreground after show/lower"
                        if self.foreground_before is False
                        else "foreground state was not reported after show/lower"
                    ),
                }
            )
            self._start_phase(0)
            return

        phase_name, duration = _PHASES[self.phase_index]
        if phase_name == "release_rebuild" and "generation_before_release" in result:
            self.release_generation = int(result["generation_before_release"])
            self.current_phase = {
                "condition": phase_name,
                "duration_seconds": duration,
                "samples": [],
                "release": _plain(result),
            }
            self.phase_started = time.monotonic()
            self.samples = self.current_phase["samples"]
            self._schedule_next_capture()
            return

        sample = _plain(result)
        sample["captured_in_phase"] = bool(
            isinstance(sample.get("captured_at"), (int, float))
            and sample["captured_at"] >= self.phase_started
        )
        if result.get("status", {}).get("foreground") is not None:
            self.foreground_after = bool(result["status"]["foreground"])
        self._update_source_freshness(sample)
        self.samples.append(sample)

        elapsed = time.monotonic() - self.phase_started
        if elapsed >= duration:
            self._finish_phase()
            return
        self._schedule_next_capture()

    def _update_source_freshness(self, sample: dict[str, Any]) -> None:
        source_sequence = sample.get("source_sequence")
        source_time = sample.get("source_time_100ns")
        generation = sample.get("generation")
        previous = self._last_source.get(generation)
        if source_sequence is None or source_time is None:
            sample["new_source_frame"] = False
        else:
            sample["new_source_frame"] = bool(
                previous is None or (source_sequence > previous[0] and source_time > previous[1])
            )
            if sample["new_source_frame"]:
                self._last_source[generation] = (source_sequence, source_time)
        sample["valid"] = bool(sample.get("valid") and sample.get("new_source_frame"))

    def _start_phase(self, index: int) -> None:
        self.phase_index = index
        if index >= len(_PHASES):
            self._shutdown()
            return
        name, duration = _PHASES[index]
        self.current_phase = {"condition": name, "duration_seconds": duration, "samples": []}
        self.samples = self.current_phase["samples"]
        initial_delay = 0
        if name == "occluded":
            self._show_occluder()
        elif name == "static_pixels" and self.target is not None:
            self.target.marker.set_frozen(True)
            self._close_occluder()
        elif name == "minimized" and self.target is not None:
            self._close_occluder()
            self.target.minimize_without_activation()
            initial_delay = 420
        elif name == "restored" and self.target is not None:
            self.target.marker.set_frozen(False)
            self.target.restore_without_activation()
            self.target.lower()
        elif name == "release_rebuild":
            self._send("release")
            return
        elif name == "target_closed":
            self._close_occluder()
            if self.target is not None:
                self.target.close()
                self.target = None
            initial_delay = 420
        self.phase_started = time.monotonic()
        self.current_phase["started_at_monotonic"] = self.phase_started
        self._schedule_next_capture(initial_delay)

    def _show_occluder(self) -> None:
        if self.target is None:
            return
        self.occluder = CaptureOccluderWindow()
        self.occluder.setGeometry(self.target.geometry())
        self.occluder.show()
        self.occluder.lower()
        self.target.lower()

    def _close_occluder(self) -> None:
        if self.occluder is not None:
            self.occluder.close()
            self.occluder = None

    def _schedule_next_capture(self, delay_ms: int = _POLL_INTERVAL_MS) -> None:
        if not self.pending and not self.shutdown_started:
            QTimer.singleShot(delay_ms, self._capture_or_advance)

    def _capture_or_advance(self) -> None:
        if self.pending or self.shutdown_started:
            return
        if time.monotonic() - self.phase_started >= _PHASES[self.phase_index][1]:
            self._finish_phase()
            return
        self._send(
            "capture",
            session_id=self.session_id,
            condition=_PHASES[self.phase_index][0],
        )

    def _finish_phase(self) -> None:
        if self.current_phase is None:
            self._start_phase(self.phase_index + 1)
            return
        name = self.current_phase["condition"]
        phase_result = evaluate_phase(
            name,
            self.current_phase["samples"],
            generation_before_release=self.release_generation
            if name == "release_rebuild"
            else None,
        )
        self.current_phase["result"] = phase_result
        self.current_phase["elapsed_seconds"] = round(time.monotonic() - self.phase_started, 3)
        self.results.append(self.current_phase)
        if name == "restored":
            foregrounds = [
                sample.get("status", {}).get("foreground")
                for sample in self.current_phase["samples"]
            ]
            observed = [value for value in foregrounds if value is not None]
            if any(observed):
                status = "fail"
                reason = "the own marker window became foreground after nonactivating restore"
            elif observed:
                status = "pass"
                reason = "the own marker window remained nonforeground after restore"
            else:
                status = "unverified"
                reason = "foreground state was not reported after restore"
            self.results.append(
                {"condition": "restore_no_activation", "status": status, "reason": reason}
            )
        self.current_phase = None
        self._start_phase(self.phase_index + 1)

    def _shutdown(self) -> None:
        if self.shutdown_started:
            return
        self.shutdown_started = True
        self._close_occluder()
        if self.target is not None:
            self.target.close()
            self.target = None
        if self.worker.isRunning():
            self._send("close")
        else:
            self.close_clean = True
            self._finish()

    def _finish(self) -> None:
        if self.worker.isRunning():
            self.worker.wait(1000)
        clean = self.close_clean is not False and not self.worker.isRunning()
        statuses = [item.get("status") for item in self.results]
        statuses.extend(
            item.get("result", {}).get("status")
            for item in self.results
            if isinstance(item.get("result"), dict)
        )
        if not clean:
            overall, self.exit_code = "fail", 2
        elif not statuses:
            overall, self.exit_code = "unverified", 1
        elif any(status == "fail" for status in statuses):
            overall, self.exit_code = "fail", 1
        elif any(status == "unverified" for status in statuses):
            overall, self.exit_code = "unverified", 1
        else:
            overall, self.exit_code = "pass", 0
        report = {
            "schema_version": 1,
            "probe": "own_window_wgc_zero_input",
            "result": overall,
            "clean_teardown": clean,
            "capability": {
                "available": True,
                "reason": "WGC wrapper/OS probe succeeded; target support is separate",
            },
            "identity": self.identity,
            "initial_geometry": self.geometry,
            "foreground_before": self.foreground_before,
            "foreground_after": self.foreground_after,
            "foreground_unchanged_informational": (
                None
                if self.foreground_before is None or self.foreground_after is None
                else self.foreground_before == self.foreground_after
            ),
            "phases": self.results,
            "process": {
                "duration_seconds": round(time.time() - self.started_at, 3),
                "cpu_seconds": round(time.process_time() - self.started_cpu, 3),
                "rss_start_bytes": self.started_rss,
                "rss_end_bytes": _process_rss_bytes(),
            },
            "limitations": [
                "This verifies only the generated own-window marker, not a game or input path.",
                "This demonstrates usable own-window BGR/corner alignment, not a pixel-exact "
                "framed-window crop.",
                "Foreground equality is informational because the user may move focus.",
            ],
        }
        if self.output_path is not None:
            try:
                self.output_path.parent.mkdir(parents=True, exist_ok=True)
                self.output_path.write_text(
                    json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                report["output_error"] = f"{type(exc).__name__}: {exc}"
                self.exit_code = 2
        try:
            if sys.stdout is not None:
                print(json.dumps(report, ensure_ascii=False))
        except Exception:
            pass
        self.app.exit(self.exit_code)


def _unavailable_report(reason: str) -> dict[str, Any]:
    phases = [
        {"condition": name, "status": "unverified", "reason": "WGC capability is unavailable"}
        for name, _duration in _PHASES
    ]
    return {
        "schema_version": 1,
        "probe": "own_window_wgc_zero_input",
        "result": "unverified",
        "clean_teardown": True,
        "capability": {"available": False, "reason": reason},
        "phases": phases,
        "limitations": ["No target window was created and no capture was attempted."],
    }


def _write_report(report: dict[str, Any], output_path: Path | None) -> int:
    if output_path is not None:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            report["output_error"] = f"{type(exc).__name__}: {exc}"
            _print_report(report)
            return 2
    _print_report(report)
    return 1


def _print_report(report: dict[str, Any]) -> None:
    if sys.stdout is None:
        return
    try:
        print(json.dumps(report, ensure_ascii=False))
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auto-ready --capture-check")
    parser.add_argument("--output", type=Path, help="write the JSON evidence to this path")
    args = parser.parse_args(argv)
    available, reason = probe_wgc_support()
    if not available:
        return _write_report(_unavailable_report(reason), args.output)

    try:
        from .native import enable_dpi_awareness

        enable_dpi_awareness()
        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setApplicationName("Auto Ready Capture Check")
        app.setQuitOnLastWindowClosed(False)
        runner = CaptureCheckRunner(app, args.output)
        QTimer.singleShot(0, runner.start)
        exit_code = app.exec()
        if runner.worker.isRunning():
            runner.worker.wait(1000)
        return exit_code
    except Exception as exc:
        return _write_report(
            {
                "schema_version": 1,
                "probe": "own_window_wgc_zero_input",
                "result": "fail",
                "clean_teardown": False,
                "capability": {"available": True, "reason": reason},
                "error": f"{type(exc).__name__}: {exc}",
                "phases": [],
            },
            args.output,
        )
