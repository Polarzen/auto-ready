"""Inspect startup and orderly shutdown for a child Auto Ready process.

The probe never focuses a window or sends mouse/keyboard input. It starts only
normal, simulator, or default disarmed verification mode and closes only the
child's observed HWNDs after rechecking their process identity.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from auto_ready.models import Identity
from auto_ready.native import WindowsBackend, enable_dpi_awareness

_MODES = ("normal", "simulator", "verification")
_MODE_FLAGS = {
    "normal": (),
    "simulator": ("--simulator",),
    "verification": ("--verify-simulator",),
}
_WM_CLOSE = 0x0010
_SIMULATOR_TITLE = "Auto Ready 模拟器"
_VERIFICATION_MAIN_TITLE = "Auto Ready 模拟器验证"
_LOG_FIELDS = re.compile(r"(?<!\S)([a-z_]+)=([^\s]+)")
_PORTABLE_OVERRIDES = {
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "QT_PLUGIN_PATH",
    "QT_QPA_PLATFORM_PLUGIN_PATH",
    "QT_QPA_FONTDIR",
    "QT_QPA_PLATFORM",
    "QT_QPA_PLATFORMTHEME",
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only startup probe; no focus or input events are sent."
    )
    parser.add_argument("executable", nargs="?", help="optional packaged Auto Ready EXE")
    parser.add_argument("--mode", choices=_MODES, default="normal")
    parser.add_argument("--report", help="fresh JSON report basename")
    args = parser.parse_args(argv)
    if args.report and (
        Path(args.report).name != args.report or Path(args.report).suffix.lower() != ".json"
    ):
        parser.error("--report must be a JSON basename")
    return args


def _mode_flags(mode: str) -> tuple[str, ...]:
    try:
        return _MODE_FLAGS[mode]
    except KeyError as exc:
        raise ValueError(f"Unsupported probe mode: {mode}") from exc


def _build_command(
    mode: str, executable: Path | None, pid_file: Path, project_root: Path
) -> list[str]:
    flags = list(_mode_flags(mode))
    if executable is not None:
        return [str(executable), *flags]
    source_python = project_root / ".venv" / "Scripts" / "python.exe"
    if not source_python.is_file():
        raise FileNotFoundError(
            f"Project virtual-environment Python was not found: {source_python}"
        )
    source = (
        "import os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii'); "
        "from auto_ready.app import main; raise SystemExit(main(sys.argv[2:]))"
    )
    return [str(source_python), "-c", source, str(pid_file), *flags]


def _child_environment(
    *, portable: bool, isolated_local_app_data: Path, base: dict[str, str] | None = None
) -> dict[str, str]:
    environment = dict(os.environ if base is None else base)
    for key in tuple(environment):
        if (key.upper() == "LOCALAPPDATA"
                or (portable and key.upper() in _PORTABLE_OVERRIDES)):
            environment.pop(key, None)

    if not portable:
        environment["LOCALAPPDATA"] = str(isolated_local_app_data.resolve())
        return environment

    system_root = next(
        (value for key, value in environment.items()
         if key.upper() in {"SYSTEMROOT", "WINDIR"}),
        None,
    )
    if not system_root:
        raise ValueError("Portable launch requires SystemRoot to restrict PATH")
    root = Path(system_root)
    environment["PATH"] = os.pathsep.join((str(root / "System32"), str(root)))
    environment["LOCALAPPDATA"] = str(isolated_local_app_data.resolve())
    return environment


def _fresh_run_paths(
    artifacts_root: Path, mode: str, report_name: str | None
) -> tuple[Path, Path]:
    stem = Path(report_name).stem if report_name else f"{mode}-{uuid.uuid4().hex}"
    run_dir = artifacts_root / stem
    if run_dir.exists():
        stem = f"{stem}-{uuid.uuid4().hex}"
        run_dir = artifacts_root / stem
    run_dir.mkdir(parents=True, exist_ok=False)
    report_path = run_dir / (Path(report_name).name if report_name else "report.json")
    return run_dir, report_path


def _expected_roles(mode: str) -> tuple[str, ...]:
    if mode not in _MODES:
        raise ValueError(f"Unsupported probe mode: {mode}")
    return ("main",) if mode == "normal" else ("main", "simulator")


def _window_role(title: str, mode: str) -> str | None:
    if mode == "verification" and title.startswith(_VERIFICATION_MAIN_TITLE):
        return "main"
    if mode == "verification":
        if title.startswith(_SIMULATOR_TITLE):
            return "simulator"
        return None
    if title.startswith(_SIMULATOR_TITLE):
        return "simulator"
    if title.startswith("Auto Ready"):
        return "main"
    return None


def _classify_windows(
    identities: list[Identity], pid: int, mode: str
) -> dict[str, Any]:
    roles: dict[str, list[Identity]] = {name: [] for name in _expected_roles(mode)}
    unclassified: list[Identity] = []
    for identity in identities:
        if identity.pid != pid:
            continue
        role = _window_role(identity.title, mode)
        if role is None or role not in roles:
            unclassified.append(identity)
        else:
            roles[role].append(identity)
    return {**roles, "unclassified": unclassified}


def _same_process_identity(current: Identity, expected: Identity) -> bool:
    return (
        current.hwnd == expected.hwnd
        and current.pid == expected.pid
        and current.process_started == expected.process_started
        and current.executable == expected.executable
    )


def _post_wm_close(hwnd: int) -> bool:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    post = user32.PostMessageW
    post.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    post.restype = ctypes.c_int
    if not post(hwnd, _WM_CLOSE, 0, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return True


def _request_close(
    backend: Any, identity: Identity, post_message: Any = _post_wm_close
) -> str:
    current = next(
        (item for item in backend.list_windows() if _same_process_identity(item, identity)),
        None,
    )
    if current is None:
        return "already_closed"
    backend.inspect(current)  # Revalidate HWND, PID, process start, and executable.
    post_message(current.hwnd)
    return "wm_close_posted"


def _bool_or_unknown(value: str | None) -> bool | str:
    if value is None:
        return "unknown"
    lowered = value.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return "unknown"


def _fields_from_log_line(line: str) -> dict[str, str]:
    return {key: value for key, value in _LOG_FIELDS.findall(line)}


def _unknown_metadata(source: str = "unknown", path: Path | None = None) -> dict[str, Any]:
    return {
        "source": source,
        "path": str(path) if path is not None else None,
        "version": "unknown",
        "frozen": "unknown",
        "startup_state": "unknown",
        "mode": "unknown",
        "attempted": "unknown",
        "backend": "unknown",
        "input_backend": "unknown",
        "allow_real_input": "unknown",
        "shutdown_worker_running": "unknown",
        "shutdown_attempted": "unknown",
    }


def _metadata_from_log(path: Path, not_before_ns: int | None = None) -> dict[str, Any]:
    result = _unknown_metadata("auto-ready.log", path)
    try:
        if not_before_ns is not None and path.stat().st_mtime_ns < not_before_ns:
            return result
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    startup = next((line for line in reversed(lines) if "startup version=" in line), None)
    shutdown = next((line for line in reversed(lines) if "shutdown worker_running=" in line), None)
    if startup is not None:
        fields = _fields_from_log_line(startup)
        result.update({
            "version": fields.get("version", "unknown"),
            "frozen": _bool_or_unknown(fields.get("frozen")),
            "startup_state": fields.get("state", "unknown"),
            "mode": fields.get("mode", "unknown"),
            "attempted": _bool_or_unknown(fields.get("attempted")),
            "backend": "unknown",
            "input_backend": fields.get("input_backend", "unknown"),
        })
    if shutdown is not None:
        fields = _fields_from_log_line(shutdown)
        result["shutdown_worker_running"] = _bool_or_unknown(fields.get("worker_running"))
        result["shutdown_attempted"] = _bool_or_unknown(fields.get("attempted"))
    return result


def _metadata_from_verification(
    summary_root: Path, not_before_ns: int | None = None
) -> dict[str, Any]:
    candidates = []
    for path in summary_root.glob("runs/*/summary.json"):
        try:
            if not_before_ns is None or path.stat().st_mtime_ns >= not_before_ns:
                candidates.append(path)
        except OSError:
            continue
    if not candidates:
        return _unknown_metadata("verification-summary")
    summary_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    result = _unknown_metadata("verification-summary", summary_path)
    try:
        document = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return result
    run = document.get("run") if isinstance(document, dict) else None
    if not isinstance(run, dict):
        return result
    result.update({
        "version": run.get("app_version", "unknown"),
        "frozen": run.get("frozen", "unknown"),
        "startup_state": run.get("initial_engine_state", "unknown"),
        "mode": run.get("initial_engine_mode", "unknown"),
        "attempted": run.get("initial_engine_attempted", "unknown"),
        "backend": run.get("capture_backend_type", "unknown"),
        "input_backend": run.get("input_adapter_type", "unknown"),
        "allow_real_input": run.get("allow_real_input", "unknown"),
    })
    try:
        events = (summary_path.parent / "events.jsonl").read_text(encoding="utf-8").splitlines()
        for line in reversed(events):
            event = json.loads(line)
            if event.get("event") == "shutdown_state":
                result["shutdown_worker_running"] = event.get("worker_running", "unknown")
                result["shutdown_attempted"] = event.get("attempted", "unknown")
                break
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return result


def _read_startup_metadata(
    local_app_data: Path, mode: str, not_before_ns: int | None = None
) -> dict[str, Any]:
    app_root = local_app_data / "auto-ready"
    if mode == "verification":
        summary = _metadata_from_verification(
            app_root / "simulator-check", not_before_ns
        )
        if summary["startup_state"] != "unknown":
            return summary
    return _metadata_from_log(app_root / "auto-ready.log", not_before_ns)


def _metadata_has_default_state(metadata: dict[str, Any], mode: str) -> bool:
    required = ("version", "input_backend")
    required += ("backend",) if mode == "verification" else ()
    if any(metadata.get(field) in (None, "unknown", "") for field in required):
        return False
    if metadata.get("startup_state") != "未启用" or metadata.get("mode") != "仅提醒":
        return False
    if metadata.get("attempted") is not False:
        return False
    if metadata.get("shutdown_worker_running") is not False:
        return False
    if metadata.get("shutdown_attempted") is not False:
        return False
    if mode == "verification" and (
        metadata.get("input_backend") != "RefusingInput"
        or metadata.get("allow_real_input") is not False
    ):
        return False
    return True


def _wait_for_pid_file(pid_file: Path, process: subprocess.Popen, timeout: float = 8.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="ascii").strip())
            except (OSError, UnicodeError, ValueError):
                pid = 0
            if pid > 0:
                return pid
        if process.poll() is not None:
            break
        time.sleep(0.05)
    raise RuntimeError(f"Source child did not publish its process ID; exit={process.poll()}")


def _wait_for_windows(
    backend: Any, process: subprocess.Popen, target_pid: int, mode: str, timeout: float = 20.0
) -> dict[str, Any]:
    expected = _expected_roles(mode)
    deadline = time.monotonic() + timeout
    last = _classify_windows([], target_pid, mode)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Child exited before expected windows appeared: {process.poll()}")
        last = _classify_windows(backend.list_windows(), target_pid, mode)
        if (all(len(last[name]) == 1 for name in expected)
                and not last["unclassified"]):
            return last
        time.sleep(0.1)
    summary = {key: [item.title for item in value] for key, value in last.items()}
    raise RuntimeError(f"Expected child windows did not appear; observed={summary}")


def _owned_window_report(backend: Any, roles: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for role, identities in roles.items():
        values = []
        for identity in identities:
            item = {"identity": asdict(identity)}
            try:
                item["geometry"] = asdict(backend.inspect(identity))
            except Exception as exc:
                item["geometry_error"] = str(exc)
            values.append(item)
        output[role] = values
    return output


def _close_windows_and_wait(
    process: subprocess.Popen, backend: Any, roles: dict[str, Any], timeout: float = 10.0
) -> tuple[int, list[dict[str, Any]]]:
    known = [*roles.get("main", []), *roles.get("simulator", [])]
    close_requests = []
    for identity in known:
        try:
            status = _request_close(backend, identity)
            close_requests.append({"hwnd": identity.hwnd, "title": identity.title,
                                   "status": status})
        except Exception as exc:
            close_requests.append({"hwnd": identity.hwnd, "title": identity.title,
                                   "status": "error", "error": str(exc)})
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Child did not exit within {timeout:g} seconds after WM_CLOSE") from exc
    return exit_code, close_requests


def _write_report(report_path: Path, report: dict[str, Any]) -> None:
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    executable = Path(args.executable).resolve() if args.executable else None
    if executable is not None and not executable.is_file():
        raise FileNotFoundError(f"Packaged executable does not exist: {executable}")

    artifacts_root = project_root / "artifacts" / "startup-checks"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    run_dir, report_path = _fresh_run_paths(artifacts_root, args.mode, args.report)
    pid_file = run_dir / "source-child.pid"
    stdout_path = run_dir / "stdout.txt"
    stderr_path = run_dir / "stderr.txt"
    isolated_local_app_data = run_dir / "localappdata"
    try:
        child_env = _child_environment(
            portable=executable is not None,
            isolated_local_app_data=isolated_local_app_data,
        )
        command = _build_command(args.mode, executable, pid_file, project_root)
    except Exception as exc:
        stdout_path.touch(exist_ok=True)
        stderr_path.touch(exist_ok=True)
        report = {
            "passed": False,
            "mode": args.mode,
            "error": str(exc),
            "report_path": str(report_path.resolve()),
            "stdout_path": str(stdout_path.resolve()),
            "stderr_path": str(stderr_path.resolve()),
            "real_input_flag_forwarded": False,
            "startup_metadata": _unknown_metadata(),
        }
        _write_report(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    working_directory = executable.parent if executable is not None else project_root
    report: dict[str, Any] = {
        "passed": False,
        "mode": args.mode,
        "launch_flags": list(_mode_flags(args.mode)),
        "command": command,
        "working_directory": str(working_directory),
        "portable_environment_isolated": executable is not None,
        "portable_path": child_env.get("PATH") if executable is not None else None,
        "cleared_portable_overrides": (
            sorted(_PORTABLE_OVERRIDES) if executable is not None else []
        ),
        "isolated_local_app_data": str(Path(child_env["LOCALAPPDATA"]).resolve()),
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "report_path": str(report_path.resolve()),
        "real_input_flag_forwarded": False,
        "process_pid": None,
        "window_process_id": None,
        "window_observations": {},
        "startup_metadata": _unknown_metadata(),
    }

    process: subprocess.Popen | None = None
    backend = None
    roles = None
    launch_started_ns = time.time_ns()
    with stdout_path.open("w", encoding="utf-8", newline="\n") as stdout_file, \
            stderr_path.open("w", encoding="utf-8", newline="\n") as stderr_file:
        try:
            enable_dpi_awareness()
            backend = WindowsBackend()
            process = subprocess.Popen(
                command,
                cwd=working_directory,
                env=child_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
            )
            report["process_pid"] = process.pid
            target_pid = (
                process.pid if executable is not None else _wait_for_pid_file(pid_file, process)
            )
            report["window_process_id"] = target_pid
            # Windows venv python.exe is a redirector: the interpreter it launches
            # publishes its own PID into this probe's fresh private handshake file.
            if executable is not None and target_pid != process.pid:
                raise RuntimeError(
                    f"Reported window PID {target_pid} differs from direct child PID {process.pid}"
                )
            roles = _wait_for_windows(backend, process, target_pid, args.mode)
            report["window_observations"] = _owned_window_report(backend, roles)
            exit_code, close_requests = _close_windows_and_wait(process, backend, roles)
            report["close_requests"] = close_requests
            report["exit_code"] = exit_code
            report["child_process_exited"] = process.poll() is not None
            if exit_code != 0:
                raise RuntimeError(f"Application exit failure: {exit_code}")
            if not report["child_process_exited"]:
                raise RuntimeError("The probe child process remains alive after wait")
            remaining = [
                item for item in backend.list_windows() if item.pid == target_pid
            ]
            report["remaining_child_windows"] = [asdict(item) for item in remaining]
            if remaining:
                raise RuntimeError(
                    f"Child process exited with {len(remaining)} visible window(s) still listed"
                )
            report["passed"] = True
        except Exception as exc:
            report["error"] = str(exc)
        finally:
            if process is not None and process.poll() is None:
                report["forced_termination"] = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                report["child_process_exited"] = process.poll() is not None

    local_app_data = Path(child_env.get("LOCALAPPDATA", isolated_local_app_data))
    metadata = _read_startup_metadata(local_app_data, args.mode, launch_started_ns)
    report["startup_metadata"] = metadata
    report["startup_metadata_complete"] = _metadata_has_default_state(metadata, args.mode)
    if not report["startup_metadata_complete"]:
        report["passed"] = False
        report.setdefault("error", "Default startup state metadata could not be read")
    if process is not None:
        report["exit_code"] = process.returncode
        report["child_process_exited"] = process.poll() is not None
    report["stdout_tail"] = stdout_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    report["stderr_tail"] = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    _write_report(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
