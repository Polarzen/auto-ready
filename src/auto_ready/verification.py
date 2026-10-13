"""Opt-in real-input check bound to this process's mock simulator only.

The helper never selects a target, captures a template, changes the mode, or
starts a round. Those steps remain manual in the UI. Real Windows input is
constructed only after the explicit ``--allow-real-input`` option is supplied.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from collections.abc import Callable
from threading import Event
from typing import Any

import cv2
from PySide6.QtWidgets import QApplication, QMessageBox

from . import __version__
from .models import Frame, Geometry, Identity, Rect
from .native import SafetyError
from .simulator import SimulatorWindow
from .ui import MainWindow, close_and_wait
from .verification_record import VerificationRecorder

SIMULATOR_TITLE = "Auto Ready 模拟器"


def _same_executable(actual: str, expected: str) -> bool:
    """Compare Windows executable paths without case or separator differences."""
    return os.path.normcase(os.path.normpath(actual)) == os.path.normcase(
        os.path.normpath(expected)
    )


def same_identity(actual: Identity | None, expected: Identity) -> bool:
    """Compare the immutable native identity fields; title is display-only."""
    return bool(
        actual is not None
        and actual.hwnd == expected.hwnd
        and actual.pid == expected.pid
        and actual.process_started == expected.process_started
        and _same_executable(actual.executable, expected.executable)
    )


class SimulatorOnlyBackend:
    """Restrict the UI and engine to one captured simulator HWND/process."""

    def __init__(self, backend: Any, identity: Identity):
        self._backend = backend
        self.identity = identity
        self._frame_lock = threading.RLock()
        self._latest_frame: Frame | None = None

    def _require_identity(self, identity: Identity) -> Identity:
        if not same_identity(identity, self.identity):
            raise SafetyError("This verification helper allows only its own simulator window")
        return self.identity

    def list_windows(self) -> list[Identity]:
        matches = [
            current
            for current in self._backend.list_windows()
            if same_identity(current, self.identity)
        ]
        return [self.identity] if len(matches) == 1 else []

    def assert_simulator_current(self) -> None:
        if self.list_windows() != [self.identity]:
            raise SafetyError("The helper's simulator identity is no longer present")

    def inspect(self, identity: Identity) -> Geometry:
        target = self._require_identity(identity)
        return self._backend.inspect(target)

    def foreground(self, identity: Identity) -> bool:
        target = self._require_identity(identity)
        return bool(self._backend.foreground(target))

    def moving(self, identity: Identity) -> bool:
        target = self._require_identity(identity)
        return bool(self._backend.moving(target))

    def unobscured(self, identity: Identity, rect: Rect, geometry: Geometry) -> bool:
        target = self._require_identity(identity)
        return bool(self._backend.unobscured(target, rect, geometry))

    def capture(self, identity: Identity, session_id: str):
        target = self._require_identity(identity)
        frame = self._backend.capture(target, session_id)
        with self._frame_lock:
            self._latest_frame = frame if isinstance(frame, Frame) else None
        return frame

    def latest_frame(self) -> Frame | None:
        with self._frame_lock:
            return self._latest_frame

    def clear_latest_frame(self) -> None:
        with self._frame_lock:
            self._latest_frame = None


class RefusingInput:
    """Default adapter: reject any accidental click instead of simulating one."""

    def click(self, _point: tuple[int, int], _cancelled: Event) -> None:
        raise SafetyError(
            "Real mouse input is disabled. Restart this helper with --allow-real-input "
            "only when you intend to click the simulator."
        )


class SimulatorBoundInput:
    """Recheck the simulator, foreground, and client bounds at the input boundary."""

    def __init__(
        self,
        backend: SimulatorOnlyBackend,
        identity: Identity,
        delegate: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._backend = backend
        self._identity = identity
        self._delegate = delegate
        self._clock = clock
        self._max_frame_age_probe: Callable[[], float] | None = None
        self._session_id_probe: Callable[[], str] | None = None

    def set_runtime_probes(
        self,
        *,
        max_frame_age: Callable[[], float],
        session_id: Callable[[], str],
    ) -> None:
        """Attach live engine values after MainWindow creates its Engine."""
        self._max_frame_age_probe = max_frame_age
        self._session_id_probe = session_id

    def click(self, point: tuple[int, int], cancelled: Event) -> None:
        if _is_cancelled(cancelled):
            raise SafetyError("Mouse input was cancelled before simulator verification")
        self._backend.assert_simulator_current()
        geometry = self._backend.inspect(self._identity)
        if not self._backend.foreground(self._identity):
            raise SafetyError("The simulator must be the foreground window; no input was sent")
        frame = self._backend.latest_frame()
        if frame is None or not same_identity(frame.identity, self._identity):
            raise SafetyError("No current simulator capture is available; no input was sent")
        if frame.geometry != geometry:
            raise SafetyError("The simulator moved or resized after its latest capture")
        age = self._clock() - frame.captured_at
        max_age = self._max_frame_age_probe() if self._max_frame_age_probe else 0.35
        if (
            isinstance(max_age, bool)
            or not isinstance(max_age, (int, float))
            or not 0 < max_age <= 0.5
            or not 0 <= age <= max_age
        ):
            raise SafetyError("The latest simulator capture is stale; no input was sent")
        if self._session_id_probe is not None:
            expected_session = self._session_id_probe()
            if not expected_session or frame.session_id != expected_session:
                raise SafetyError("The latest capture does not belong to the active round")
        try:
            if (
                not isinstance(point, tuple)
                or len(point) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in point)
            ):
                raise ValueError
            client_point = (point[0] - geometry.left, point[1] - geometry.top)
        except (TypeError, ValueError, IndexError) as exc:
            raise SafetyError("The proposed input point is invalid") from exc
        if not Rect(0, 0, geometry.width, geometry.height).contains(client_point):
            raise SafetyError("The proposed input point is outside the simulator client area")
        if _is_cancelled(cancelled):
            raise SafetyError("Mouse input was cancelled before simulator verification")
        self._backend.assert_simulator_current()
        if not self._backend.foreground(self._identity):
            raise SafetyError("The simulator is no longer foreground; no input was sent")
        if self._backend.inspect(self._identity) != frame.geometry:
            raise SafetyError("The simulator moved or resized before input; no input was sent")
        current_age = self._clock() - frame.captured_at
        if not 0 <= current_age <= max_age:
            raise SafetyError("The latest simulator capture became stale; no input was sent")
        if _is_cancelled(cancelled):
            raise SafetyError("Mouse input was cancelled before simulator verification")
        self._delegate.click(point, cancelled)


def build_input_adapter(
    backend: SimulatorOnlyBackend,
    identity: Identity,
    *,
    allow_real_input: bool,
    input_factory: Callable[[], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RefusingInput | SimulatorBoundInput:
    """Create a refusing adapter by default and defer WindowsInput construction."""
    if not allow_real_input:
        return RefusingInput()
    if input_factory is None:
        from auto_ready.native import WindowsInput

        input_factory = WindowsInput
    return SimulatorBoundInput(backend, identity, input_factory(), clock=clock)


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open Auto Ready beside its own simulator. Real mouse input is disabled by default; "
            "even with --allow-real-input, you must manually select the simulator, capture a "
            "template, choose automatic mode, start a round, and foreground the simulator."
        )
    )
    parser.add_argument(
        "--allow-real-input",
        action="store_true",
        help="explicitly permit the engine's one guarded mouse click, bound to this simulator",
    )
    return parser.parse_args(argv)


class _ManagedSimulatorWindow(SimulatorWindow):
    def __init__(self, close_peer: Callable[[], None], observer: Any):
        self._close_peer = close_peer
        super().__init__(observer=observer, evidence_source="auto")

    def closeEvent(self, event: Any) -> None:
        self._close_peer()
        self.ready_timer.stop()
        self.expiry_timer.stop()
        super().closeEvent(event)


class _ManagedMainWindow(MainWindow):
    def __init__(
        self, close_peer: Callable[[], None], recorder: VerificationRecorder, **kwargs: Any
    ):
        self._close_peer = close_peer
        self._recorder = recorder
        self._recorder_finalized = False
        super().__init__(**kwargs)

    def closeEvent(self, event: Any) -> None:
        self._close_peer()
        super().closeEvent(event)
        if event.isAccepted() and not self.worker.isRunning() and not self._recorder_finalized:
            self._recorder_finalized = True
            self._recorder.record("shutdown_state", worker_running=self.worker.isRunning(),
                                  attempted=self.engine.snapshot().attempted)
            if hasattr(self.backend, "clear_latest_frame"):
                self.backend.clear_latest_frame()
            self._recorder.close()


def _simulator_identity(backend: Any, window: SimulatorWindow) -> Identity:
    expected_hwnd = int(window.winId())
    expected_executables = [sys.executable]
    if not getattr(sys, "frozen", False):
        # Windows venv python.exe redirects to its base interpreter. The HWND and
        # PID below still must belong to this exact simulator in our own process.
        expected_executables.append(getattr(sys, "_base_executable", sys.executable))
    matches = [
        identity
        for identity in backend.list_windows()
        if identity.hwnd == expected_hwnd
        and identity.pid == os.getpid()
        and identity.title == SIMULATOR_TITLE
        and any(_same_executable(identity.executable, path) for path in expected_executables)
    ]
    if len(matches) != 1:
        raise SafetyError("Could not uniquely identify this helper's visible simulator window")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if os.name != "nt":
        print("Simulator real-input verification is available only on Windows.", file=sys.stderr)
        return 2

    from auto_ready.config import ConfigStore, setup_logging
    from auto_ready.native import SingleInstance, WindowsBackend, enable_dpi_awareness

    instance = None
    app = None
    simulator = None
    window = None
    recorder = None
    closing = [False]
    peers: dict[str, Any] = {}

    def close_peer(closing_window: str) -> None:
        if closing[0]:
            return
        closing[0] = True
        peer = peers.get("simulator" if closing_window == "main" else "main")
        if peer is not None:
            peer.close()

    try:
        enable_dpi_awareness()
        cv2.setNumThreads(1)
        instance = SingleInstance()
        if not instance.acquire():
            print("Another Auto Ready instance is already running.", file=sys.stderr)
            return 2

        store = ConfigStore(ConfigStore().root / "simulator-check")
        setup_logging(store.root)
        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setApplicationName("Auto Ready Simulator Verification")

        raw_backend = WindowsBackend()
        recorder = VerificationRecorder(store.root)
        simulator = _ManagedSimulatorWindow(
            lambda: close_peer("simulator"), observer=recorder.record
        )
        peers["simulator"] = simulator
        simulator.show()
        app.processEvents()
        identity = _simulator_identity(raw_backend, simulator)
        backend = SimulatorOnlyBackend(raw_backend, identity)

        geometry = backend.inspect(identity)

        def make_real_input():
            from .native import WindowsInput

            return WindowsInput(observer=recorder.record)

        input_adapter = build_input_adapter(
            backend,
            identity,
            allow_real_input=args.allow_real_input,
            input_factory=make_real_input,
        )
        window = _ManagedMainWindow(
            lambda: close_peer("main"),
            recorder=recorder,
            backend=backend,
            input_adapter=input_adapter,
            store=store,
        )
        window.engine.set_observer(recorder)
        startup = window.engine.snapshot()
        if not recorder.record(
            "run_started",
            app_version=__version__,
            frozen=bool(getattr(sys, "frozen", False)),
            initial_engine_state=startup.state.value,
            initial_engine_reason=startup.reason,
            initial_engine_attempted=startup.attempted,
            initial_engine_mode=window.mode.value,
            capture_backend_type=type(raw_backend).__name__,
            input_adapter_type=type(input_adapter).__name__,
            real_input_backend_type=(
                type(input_adapter._delegate).__name__
                if isinstance(input_adapter, SimulatorBoundInput)
                else None
            ),
            simulator_identity=identity,
            initial_geometry=geometry,
            dpi=geometry.dpi,
            allow_real_input=args.allow_real_input,
            system_input_status="NOT_EXECUTED",
        ):
            raise RuntimeError("Unable to write initial simulator verification evidence")
        recorder.record(
            "startup_state",
            state=startup.state.value,
            reason=startup.reason,
            attempted=startup.attempted,
            session_id=startup.session_id or None,
        )
        if isinstance(input_adapter, SimulatorBoundInput):
            input_adapter.set_runtime_probes(
                max_frame_age=lambda: window.engine.settings.max_frame_age,
                session_id=lambda: window.engine.snapshot().session_id,
            )
        window.setWindowTitle(f"Auto Ready 模拟器验证 · {recorder.run_dir}")
        if hasattr(window, "status_label"):
            window.status_label.setText(f"验证证据保存在：{recorder.run_dir}")
        peers["main"] = window
        window.show()
        return app.exec()
    except Exception as exc:
        message = f"Simulator verification could not start: {exc}"
        print(message, file=sys.stderr)
        if recorder is not None:
            recorder.record("startup_error", error=f"{type(exc).__name__}: {exc}")
        if app is not None:
            QMessageBox.critical(None, "模拟器验证无法启动", message)
        return 2
    finally:
        try:
            if window is not None:
                close_and_wait(window)
            if simulator is not None:
                simulator.close()
        finally:
            try:
                if recorder is not None and (window is None or not window.worker.isRunning()):
                    recorder.close()
            finally:
                if instance is not None:
                    instance.close()


def _is_cancelled(cancelled: Any) -> bool:
    try:
        return bool(cancelled.is_set())
    except Exception as exc:
        raise SafetyError("Cancellation status could not be verified") from exc


if __name__ == "__main__":
    raise SystemExit(main())
