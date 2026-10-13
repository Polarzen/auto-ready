from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_close_dispatches_gui_until_blocked_backend_worker_exits() -> None:
    child = textwrap.dedent(
        r"""
        import json
        import tempfile
        import threading
        from pathlib import Path

        from PySide6.QtCore import QMetaObject, QObject, Qt, Slot
        from PySide6.QtWidgets import QApplication

        from auto_ready.config import ConfigStore
        from auto_ready.models import Region, Settings
        from auto_ready.ui import close_and_wait
        from auto_ready.verification import _ManagedMainWindow
        from auto_ready.verification_record import VerificationRecorder

        class Dispatcher(QObject):
            def __init__(self):
                super().__init__()
                self.serviced = threading.Event()

            @Slot()
            def service(self):
                self.serviced.set()

        class Backend:
            def __init__(self, dispatcher):
                self.dispatcher = dispatcher
                self.entered = threading.Event()
                self.proceed = threading.Event()

            def list_windows(self):
                self.entered.set()
                if not self.proceed.wait(2):
                    raise RuntimeError("test did not release list_windows")
                invoked = QMetaObject.invokeMethod(
                    self.dispatcher,
                    "service",
                    Qt.ConnectionType.BlockingQueuedConnection,
                )
                if not invoked:
                    raise RuntimeError("blocking GUI request was not queued")
                return []

        class FakeInput:
            def click(self, *_args, **_kwargs):
                raise AssertionError("test shutdown must never issue input")

        class FakeHotkey:
            available = True
            error = ""

            def __init__(self, _spec, _callback):
                pass

            def start(self):
                return True

            def close(self):
                pass

        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        dispatcher = Dispatcher()
        backend = Backend(dispatcher)
        with tempfile.TemporaryDirectory(prefix="auto-ready-close-race-") as root:
            recorder = VerificationRecorder(Path(root))
            window = _ManagedMainWindow(
                lambda: None,
                recorder=recorder,
                backend=backend,
                input_adapter=FakeInput(),
                store=ConfigStore(Path(root) / "config"),
                hotkey_factory=FakeHotkey,
                notifier=lambda: None,
                capture_delay=0,
            )
            window.show()
            window.refresh_targets()

            assert backend.entered.wait(timeout=3), "worker never entered list_windows"
            assert window.worker.isRunning(), "test requires finally to join an active worker"
            window.close()
            assert not window._closed, "close must be ignored while worker is running"
            assert recorder._closed is False
            assert not recorder.summary_path.exists()
            window.close()
            assert not window._closed, "repeated close must remain pending"
            backend.proceed.set()

            # This call runs without app.exec(), as application finally does after
            # the event loop has returned; it must dispatch the worker's queued GUI call.
            close_and_wait(window)
            assert dispatcher.serviced.is_set(), "GUI did not service the worker's blocking call"
            assert not window.worker.isRunning(), "worker did not stop before close was accepted"
            assert window._closed
            assert recorder._closed

            events = [
                json.loads(line)
                for line in recorder.events_path.read_text(encoding="utf-8").splitlines()
            ]
            shutdown = [event for event in events if event["event"] == "shutdown_state"]
            assert len(shutdown) == 1
            assert shutdown[0]["worker_running"] is False
            count = len(events)
            window.close()
            app.processEvents()
            assert len(recorder.events_path.read_text(encoding="utf-8").splitlines()) == count
            print("two-phase shutdown passed")
        """
    )
    child_env = os.environ.copy()
    child_env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=Path(__file__).resolve().parents[1],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=6,
        check=False,
    )
    assert result.returncode == 0, (
        f"shutdown subprocess failed ({result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "two-phase shutdown passed" in result.stdout
