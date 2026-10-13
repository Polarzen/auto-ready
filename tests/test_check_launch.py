from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_ready.models import Identity
from scripts import check_launch


def _identity(hwnd: int, pid: int, title: str, started: int = 100) -> Identity:
    return Identity(hwnd, pid, started, "AutoReady.exe", title)


def test_launch_modes_forward_only_the_selected_safe_flag(tmp_path: Path) -> None:
    root = tmp_path / "project"
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    pid_file = tmp_path / "child.pid"
    executable = tmp_path / "Auto Ready.exe"

    assert check_launch._mode_flags("normal") == ()
    assert check_launch._mode_flags("simulator") == ("--simulator",)
    assert check_launch._mode_flags("verification") == ("--verify-simulator",)
    assert check_launch._build_command("simulator", executable, pid_file, root) == [
        str(executable), "--simulator"
    ]
    source_command = check_launch._build_command("verification", None, pid_file, root)
    assert source_command[:2] == [str(venv_python), "-c"]
    assert source_command[-2:] == [str(pid_file), "--verify-simulator"]
    assert "--allow-real-input" not in source_command


def test_cli_rejects_arbitrary_real_input_flags() -> None:
    with pytest.raises(SystemExit):
        check_launch._parse_args(["--mode", "verification", "--allow-real-input"])


def test_portable_environment_is_isolated_and_path_is_restricted(tmp_path: Path) -> None:
    env = {
        "SystemRoot": r"C:\Windows",
        "PATH": r"C:\source\.venv;C:\Windows\System32",
        "PYTHONPATH": r"C:\source\src",
        "PyThOnHoMe": r"C:\python",
        "VIRTUAL_ENV": r"C:\source\.venv",
        "QT_PLUGIN_PATH": r"C:\source\qt-plugins",
        "QT_QPA_PLATFORM_PLUGIN_PATH": r"C:\source\qt-platforms",
        "QT_QPA_PLATFORM": "offscreen",
        "QT_QPA_FONTDIR": r"C:\source\fonts",
        "QT_QPA_PLATFORMTHEME": "test-theme",
        "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
        "KEEP_ME": "inherited",
    }
    isolated = tmp_path / "startup-checks" / "run" / "localappdata"

    portable = check_launch._child_environment(
        portable=True, isolated_local_app_data=isolated, base=env
    )
    source = check_launch._child_environment(
        portable=False, isolated_local_app_data=isolated, base=env
    )

    assert portable["PATH"] == os_pathsep(r"C:\Windows\System32", r"C:\Windows")
    assert portable["LOCALAPPDATA"] == str(isolated.resolve())
    assert portable["KEEP_ME"] == "inherited"
    assert not any(key.upper() in check_launch._PORTABLE_OVERRIDES
                   for key in portable)
    assert source["LOCALAPPDATA"] == str(isolated.resolve())
    assert {key: value for key, value in source.items() if key != "LOCALAPPDATA"} == {
        key: value for key, value in env.items() if key != "LOCALAPPDATA"
    }


def os_pathsep(*parts: str) -> str:
    import os

    return os.pathsep.join(parts)


@pytest.mark.parametrize(
    ("mode", "identities", "expected"),
    [
        ("normal", [_identity(1, 42, "Auto Ready · 开始前逐项确认")], "main"),
        ("simulator", [_identity(1, 42, "Auto Ready · start"),
                       _identity(2, 42, "Auto Ready 模拟器")], "simulator"),
        ("verification", [_identity(1, 42, "Auto Ready 模拟器验证 · run"),
                           _identity(2, 42, "Auto Ready 模拟器")], "simulator"),
    ],
)
def test_window_classification_is_mode_and_child_pid_specific(
    mode: str, identities: list[Identity], expected: str
) -> None:
    identities.append(_identity(3, 99, "Auto Ready unrelated process"))

    roles = check_launch._classify_windows(identities, 42, mode)

    assert len(roles["main"]) == 1
    assert len(roles.get("simulator", [])) == (0 if mode == "normal" else 1)
    assert roles[expected]
    assert roles["unclassified"] == []


def test_verification_main_title_is_not_misclassified_as_simulator() -> None:
    assert check_launch._window_role(
        "Auto Ready 模拟器验证 · evidence", "verification"
    ) == "main"


def test_close_rechecks_the_full_process_identity_before_posting() -> None:
    expected = _identity(10, 42, "Auto Ready · main")

    class Backend:
        def __init__(self, current: Identity) -> None:
            self.current = current
            self.inspected: list[Identity] = []

        def list_windows(self) -> list[Identity]:
            return [self.current]

        def inspect(self, identity: Identity) -> object:
            self.inspected.append(identity)
            return object()

    backend = Backend(_identity(10, 42, "Auto Ready · recycled", started=101))
    posted: list[int] = []
    assert check_launch._request_close(backend, expected, posted.append) == "already_closed"
    assert backend.inspected == []
    assert posted == []

    backend.current = expected
    assert check_launch._request_close(backend, expected, posted.append) == "wm_close_posted"
    assert backend.inspected == [expected]
    assert posted == [expected.hwnd]


def test_startup_metadata_reports_observed_log_and_verifier_fields(tmp_path: Path) -> None:
    log = tmp_path / "auto-ready.log"
    log.write_text(
        "2026-10-13 INFO auto_ready: startup version=0.2.0 frozen=True "
        "state=未启用 mode=仅提醒 attempted=False input_backend=WindowsInput\n"
        "2026-10-13 INFO auto_ready: shutdown worker_running=False attempted=False\n",
        encoding="utf-8",
    )
    metadata = check_launch._metadata_from_log(log)
    assert metadata["version"] == "0.2.0"
    assert metadata["startup_state"] == "未启用"
    assert metadata["attempted"] is False
    assert metadata["backend"] == "unknown"
    assert metadata["input_backend"] == "WindowsInput"
    assert metadata["shutdown_worker_running"] is False
    assert check_launch._metadata_has_default_state(metadata, "normal")

    summary_root = tmp_path / "simulator-check"
    summary = summary_root / "runs" / "run-id" / "summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(json.dumps({
        "run": {
            "app_version": "0.2.0",
            "frozen": True,
            "initial_engine_state": "未启用",
            "initial_engine_mode": "仅提醒",
            "initial_engine_attempted": False,
            "capture_backend_type": "WindowsBackend",
            "input_adapter_type": "RefusingInput",
            "allow_real_input": False,
        }
    }), encoding="utf-8")
    (summary.parent / "events.jsonl").write_text(json.dumps({
        "event": "shutdown_state", "worker_running": False, "attempted": False,
    }) + "\n", encoding="utf-8")
    verified = check_launch._metadata_from_verification(summary_root)
    assert verified["version"] == "0.2.0"
    assert verified["startup_state"] == "未启用"
    assert verified["attempted"] is False
    assert verified["backend"] == "WindowsBackend"
    assert check_launch._metadata_has_default_state(verified, "verification")

    for field, value in (
        ("startup_state", "已启用"), ("mode", "自动确认"), ("attempted", True),
        ("shutdown_worker_running", True), ("shutdown_attempted", True),
        ("input_backend", "WindowsInput"), ("allow_real_input", True),
    ):
        assert not check_launch._metadata_has_default_state(
            {**verified, field: value}, "verification"
        )
        assert not check_launch._metadata_has_default_state(
            {**verified, field: "unknown"}, "verification"
        )


def test_metadata_does_not_reuse_a_log_older_than_the_launch(tmp_path: Path) -> None:
    log = tmp_path / "auto-ready.log"
    log.write_text(
        "2026-10-13 INFO auto_ready: startup version=old frozen=True state=未启用 "
        "mode=仅提醒 attempted=False input_backend=WindowsInput\n",
        encoding="utf-8",
    )

    metadata = check_launch._metadata_from_log(log, log.stat().st_mtime_ns + 1)

    assert metadata["startup_state"] == "unknown"
    assert not check_launch._metadata_has_default_state(metadata, "normal")


def test_report_paths_are_fresh_even_when_report_name_is_reused(tmp_path: Path) -> None:
    first, first_report = check_launch._fresh_run_paths(tmp_path, "normal", "review.json")
    second, second_report = check_launch._fresh_run_paths(tmp_path, "normal", "review.json")

    assert first != second
    assert first_report.name == second_report.name == "review.json"
