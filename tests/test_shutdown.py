from __future__ import annotations

import json
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
    repo_root = Path(__file__).resolve().parents[1]
    child_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root / "src"), child_env.get("PYTHONPATH", "")) if part
    )
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=repo_root,
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


def test_main_reports_capture_release_failure_and_preserves_clean_exit(tmp_path: Path) -> None:
    child = textwrap.dedent(
        r"""
        import json
        import logging
        import os
        import sys
        from pathlib import Path
        from types import SimpleNamespace

        from PySide6.QtWidgets import QMainWindow

        from auto_ready import app as entry
        from auto_ready import config, ui

        log_path = Path(os.environ["AUTO_READY_TEST_LOG"])
        root_marker = Path(os.environ["AUTO_READY_TEST_ROOT_MARKER"])

        class RecordingStore(config.ConfigStore):
            def __init__(self, root=None):
                super().__init__(root)
                root_marker.write_text(str(self.root), encoding="utf-8")

        class Worker:
            def __init__(self):
                self.release_errors = (
                    ("RuntimeError: native release failed",)
                    if os.environ["AUTO_READY_TEST_RELEASE_ERROR"] == "1"
                    else ()
                )

            def isRunning(self):
                return False

            def capture_release_errors(self):
                return self.release_errors

        class FakeWindow(QMainWindow):
            def __init__(self, **_kwargs):
                super().__init__()
                self._closed = False
                self.worker = Worker()
                self.engine = SimpleNamespace(
                    snapshot=lambda: SimpleNamespace(
                        state=SimpleNamespace(value="未启用"), attempted=False
                    ),
                    input_adapter=object(),
                )
                self.mode = SimpleNamespace(value="仅提醒")

            def closeEvent(self, event):
                self._closed = True
                event.accept()

        def setup_test_logging(_root):
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"
            ))
            logger = logging.getLogger("auto_ready")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            logger.addHandler(handler)

        config.ConfigStore = RecordingStore
        config.setup_logging = setup_test_logging
        ui.MainWindow = FakeWindow
        exit_code = entry.main(["--smoke-test"])
        temporary_root = Path(root_marker.read_text(encoding="utf-8"))
        print(json.dumps({
            "exit_code": exit_code,
            "temporary_root_removed": not temporary_root.exists(),
        }))
        sys.exit(exit_code)
        """
    )

    for has_release_error, expected_exit, expected_clean in (
        (True, 2, False),
        (False, 0, True),
    ):
        case_root = tmp_path / ("release-error" if has_release_error else "clean")
        case_root.mkdir()
        log_path = case_root / "auto-ready.log"
        marker_path = case_root / "temporary-root.txt"
        child_env = os.environ.copy()
        child_env["QT_QPA_PLATFORM"] = "offscreen"
        child_env["AUTO_READY_TEST_LOG"] = str(log_path)
        child_env["AUTO_READY_TEST_ROOT_MARKER"] = str(marker_path)
        child_env["AUTO_READY_TEST_RELEASE_ERROR"] = "1" if has_release_error else "0"
        repo_root = Path(__file__).resolve().parents[1]
        child_env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(repo_root / "src"), child_env.get("PYTHONPATH", ""))
            if part
        )
        result = subprocess.run(
            [sys.executable, "-c", child],
            cwd=repo_root,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        assert result.returncode == expected_exit, (
            f"app subprocess failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        result_data = json.loads(result.stdout.strip().splitlines()[-1])
        assert result_data == {
            "exit_code": expected_exit,
            "temporary_root_removed": True,
        }
        log = log_path.read_text(encoding="utf-8")
        assert f"capture_clean={str(expected_clean)}" in log
        if has_release_error:
            assert "Native capture teardown was not confirmed" in log
            assert "RuntimeError: native release failed" in log
        else:
            assert "Native capture teardown was not confirmed" not in log
