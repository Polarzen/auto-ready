"""Application entry point for the Auto Ready desktop UI."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from . import __version__

_error_app: QApplication | None = None


def _show_error(message: str) -> None:
    global _error_app
    if QApplication.instance() is None:
        _error_app = QApplication(sys.argv[:1])
    QMessageBox.critical(None, "Auto Ready 无法启动", message)


def _cleanup_temporary_root(temporary_root: tempfile.TemporaryDirectory[str] | None) -> None:
    if temporary_root is None:
        return
    root = Path(temporary_root.name).resolve()
    logger = logging.getLogger("auto_ready")
    for handler in list(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            try:
                Path(handler.baseFilename).resolve().relative_to(root)
            except ValueError:
                continue
            logger.removeHandler(handler)
            handler.close()
    temporary_root.cleanup()


class _SmokeBackend:
    """No-window backend used only by --smoke-test; it cannot send input."""

    def list_windows(self) -> list[Any]:
        return []

    def inspect(self, _identity: Any) -> Any:
        raise RuntimeError("smoke test has no target windows")

    def capture(self, _identity: Any, _session_id: str) -> Any:
        raise RuntimeError("smoke test has no target windows")


class _SmokeInput:
    def click(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("--smoke-test never sends input")


class _SmokeHotkey:
    available = True
    error = ""

    def __init__(self, _spec: str, _callback: Any):
        pass

    def start(self) -> bool:
        return True

    def close(self) -> None:
        pass


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="auto-ready", description="本地单轮就绪助手")
    parser.add_argument("--simulator", action="store_true", help="同时打开交互式模拟窗口")
    parser.add_argument("--smoke-test", action="store_true",
                        help="启动界面后自动关闭；不选择窗口、不发送输入")
    parser.add_argument("--verify-simulator", action="store_true",
                        help="打开受限模拟器验收；默认拒绝真实输入")
    parser.add_argument("--allow-real-input", action="store_true",
                        help="仅限受限模拟器；由用户现场明确允许真实输入")
    args = parser.parse_args(argv)
    if args.allow_real_input and not args.verify_simulator:
        parser.error("--allow-real-input 只能与 --verify-simulator 一起使用")
    if args.verify_simulator and (args.simulator or args.smoke_test):
        parser.error("--verify-simulator 不能与 --simulator 或 --smoke-test 混用")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.verify_simulator:
        from .verification import main as verify_main

        return verify_main(["--allow-real-input"] if args.allow_real_input else [])
    temporary_root = (tempfile.TemporaryDirectory(prefix="auto-ready-smoke-")
                      if args.smoke_test else None)
    try:
        from .config import ConfigStore, setup_logging

        store = (ConfigStore(Path(temporary_root.name)) if temporary_root is not None
                 else ConfigStore())
        setup_logging(store.root)
    except Exception as exc:
        _show_error(f"无法准备日志目录或配置位置：{exc}")
        _cleanup_temporary_root(temporary_root)
        return 2

    try:
        import cv2

        cv2.setNumThreads(1)
    except Exception as exc:
        _show_error(f"无法初始化图像识别运行环境：{exc}")
        _cleanup_temporary_root(temporary_root)
        return 2

    instance = None
    app = None
    simulator_window = None
    window = None
    previous_excepthook = None
    try:
        from .native import SingleInstance, enable_dpi_awareness

        enable_dpi_awareness()
        if not args.smoke_test:
            instance = SingleInstance()
            if not instance.acquire():
                instance.close()
                instance = None
                return 0

        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setApplicationName("Auto Ready")
        window_ref: list[Any] = [None]
        previous_excepthook = sys.excepthook

        def handle_uncaught_exception(exc_type: Any, exc_value: Any,
                                      exc_traceback: Any) -> None:
            logging.getLogger("auto_ready").critical(
                "Unhandled desktop exception",
                exc_info=(exc_type, exc_value, exc_traceback),
            )
            active_window = window_ref[0]
            if active_window is not None:
                try:
                    active_window.worker.invalidate_authorization()
                    active_window.engine.stop("未捕获异常，立即取消当前轮")
                except Exception:
                    logging.getLogger("auto_ready").exception("Immediate stop failed")

            def shutdown() -> None:
                try:
                    if window_ref[0] is not None:
                        window_ref[0].close()
                    if simulator_window is not None:
                        simulator_window.close()
                finally:
                    app.exit(2)

            QTimer.singleShot(0, shutdown)

        sys.excepthook = handle_uncaught_exception
        from .ui import MainWindow, close_and_wait

        if args.simulator:
            from .simulator import SimulatorWindow

            simulator_window = SimulatorWindow()
            simulator_window.show()
        if args.smoke_test:
            window = MainWindow(backend=_SmokeBackend(), input_adapter=_SmokeInput(),
                                store=store, hotkey_factory=_SmokeHotkey, notifier=lambda: None,
                                capture_delay=0)
        else:
            window = MainWindow(store=store)
        window_ref[0] = window
        window.show()
        logging.getLogger("auto_ready").info(
            "startup version=%s frozen=%s state=%s mode=%s attempted=%s input_backend=%s",
            __version__, bool(getattr(sys, "frozen", False)), window.engine.snapshot().state.value,
            window.mode.value, window.engine.snapshot().attempted,
            type(window.engine.input_adapter).__name__,
        )
        if args.smoke_test:
            def close_smoke_windows():
                window.close()
                if simulator_window is not None:
                    simulator_window.close()

            QTimer.singleShot(120, close_smoke_windows)
        return app.exec()
    except Exception as exc:
        logging.getLogger("auto_ready").exception("Desktop startup failed")
        try:
            _show_error(f"请检查应用日志。启动失败：{exc}")
        except Exception:
            logging.getLogger("auto_ready").exception("Could not show startup error dialog")
        return 2
    finally:
        if previous_excepthook is not None:
            sys.excepthook = previous_excepthook
        try:
            if window is not None:
                close_and_wait(window)
                logging.getLogger("auto_ready").info(
                    "shutdown worker_running=%s attempted=%s", window.worker.isRunning(),
                    window.engine.snapshot().attempted,
                )
        finally:
            try:
                if simulator_window is not None:
                    simulator_window.close()
            finally:
                if instance is not None:
                    instance.close()
                _cleanup_temporary_root(temporary_root)
