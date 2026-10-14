"""Application entry point for the Auto Ready desktop UI."""

from __future__ import annotations

import argparse
import json
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
    parser.add_argument(
        "--capture-check",
        action="store_true",
        help="只读捕获本进程创建的标记窗口；不选择用户窗口或发送输入",
    )
    parser.add_argument(
        "--capture-check-output", type=Path, help="写入 --capture-check 的 JSON 证据路径"
    )
    parser.add_argument(
        "--capture-backend-check",
        choices=("available", "unavailable"),
        help="与 --smoke-test 一起检查捕获依赖导入路径，不创建目标窗口",
    )
    parser.add_argument("--allow-real-input", action="store_true",
                        help="仅限受限模拟器；由用户现场明确允许真实输入")
    args = parser.parse_args(argv)
    if args.allow_real_input and not args.verify_simulator:
        parser.error("--allow-real-input 只能与 --verify-simulator 一起使用")
    if args.verify_simulator and (args.simulator or args.smoke_test):
        parser.error("--verify-simulator 不能与 --simulator 或 --smoke-test 混用")
    if args.capture_check and (
        args.simulator
        or args.verify_simulator
        or args.smoke_test
        or args.allow_real_input
        or args.capture_backend_check is not None
    ):
        parser.error("--capture-check 不能与模拟器、验证、smoke 或真实输入选项混用")
    if args.capture_check_output is not None and not args.capture_check:
        parser.error("--capture-check-output 只能与 --capture-check 一起使用")
    if args.capture_backend_check is not None and not args.smoke_test:
        parser.error("--capture-backend-check 只能与 --smoke-test 一起使用")
    return args


def _check_capture_backend(mode: str) -> bool:
    """Probe only the packaged/source capture dependency; never create a target."""
    from .capture import probe_wgc_support

    available, reason = probe_wgc_support(force_unavailable=(mode == "unavailable"))
    expected = mode == "available"
    result = {
        "check": "capture_backend_import",
        "expected": mode,
        "available": available,
        "status": "pass" if available is expected else "fail",
        "reason": reason,
        "target_created": False,
        "capture_attempted": False,
        "input_attempted": False,
    }
    if sys.stdout is not None:
        try:
            print(json.dumps(result, ensure_ascii=False))
        except Exception:
            pass
    return available is expected


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.capture_check:
        from .capture_check import main as capture_check_main

        forwarded = (
            ["--output", str(args.capture_check_output)]
            if args.capture_check_output is not None
            else []
        )
        return capture_check_main(forwarded)
    if args.capture_backend_check is not None:
        if not _check_capture_backend(args.capture_backend_check):
            return 2
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
    exit_code = 0
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
        exit_code = app.exec()
    except Exception as exc:
        logging.getLogger("auto_ready").exception("Desktop startup failed")
        try:
            _show_error(f"请检查应用日志。启动失败：{exc}")
        except Exception:
            logging.getLogger("auto_ready").exception("Could not show startup error dialog")
        exit_code = 2
    finally:
        if previous_excepthook is not None:
            sys.excepthook = previous_excepthook
        logger = logging.getLogger("auto_ready")
        if window is not None:
            try:
                close_and_wait(window)
            except Exception:
                exit_code = 2
                logger.exception("Desktop shutdown failed")
            try:
                worker_running = window.worker.isRunning()
                release_errors: tuple[str, ...] = ()
                if not worker_running:
                    try:
                        release_errors = window.worker.capture_release_errors()
                    except Exception as exc:
                        release_errors = (
                            f"release status unavailable: {type(exc).__name__}: {exc}",
                        )
                if worker_running or release_errors:
                    exit_code = 2
                    if release_errors:
                        logger.error(
                            "Native capture teardown was not confirmed: %s",
                            "; ".join(release_errors),
                        )
                    else:
                        logger.error(
                            "Native capture teardown was not confirmed because "
                            "the worker is still running"
                        )
                capture_clean = not worker_running and not release_errors
                try:
                    attempted = window.engine.snapshot().attempted
                except Exception:
                    attempted = "unknown"
                    logger.exception("Could not inspect engine state during shutdown")
                    exit_code = 2
                    capture_clean = False
                logger.info(
                    "shutdown worker_running=%s attempted=%s capture_clean=%s",
                    worker_running, attempted, capture_clean,
                )
            except Exception:
                exit_code = 2
                logger.exception("Could not report UI shutdown state")
        if simulator_window is not None:
            try:
                simulator_window.close()
            except Exception:
                exit_code = 2
                logger.exception("Simulator shutdown failed")
        if instance is not None:
            try:
                instance.close()
            except Exception:
                exit_code = 2
                logger.exception("Single-instance cleanup failed")
        try:
            _cleanup_temporary_root(temporary_root)
        except Exception:
            exit_code = 2
            logger.exception("Temporary-root cleanup failed")
    return exit_code
