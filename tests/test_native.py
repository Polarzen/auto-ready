from __future__ import annotations

import ctypes
import queue
import threading

import numpy as np
import pytest

from auto_ready import native
from auto_ready.models import Geometry, Identity, Rect


class FakeInputAPI:
    def __init__(self, *, result: int = 3, failure: BaseException | None = None) -> None:
        self.metrics = {76: -1920, 77: -100, 78: 3840, 79: 2160}
        self.result = result
        self.failure = failure
        self.calls = []
        self.pressed: set[int] = set()
        self.last_error = 0

    def GetSystemMetrics(self, metric):
        return self.metrics[metric]

    def GetAsyncKeyState(self, key):
        return 0x8000 if key in self.pressed else 0

    def SendInput(self, count, events, size):
        self.calls.append((count, events, size))
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        return 1 if count == 1 else self.result

    def SetLastError(self, code):
        self.last_error = code

    def GetLastError(self):
        return self.last_error


def test_input_uses_physical_virtual_desktop_coordinates_and_one_batch():
    api = FakeInputAPI()
    native.WindowsInput(_api=api).click((1919, 2059), threading.Event())

    assert len(api.calls) == 1
    count, events, size = api.calls[0]
    assert count == 3
    assert size == ctypes.sizeof(native._INPUT)
    assert events[0].mi.dx == events[0].mi.dy == 65535
    assert events[0].mi.dwFlags == (
        native._MOUSEEVENTF_MOVE
        | native._MOUSEEVENTF_ABSOLUTE
        | native._MOUSEEVENTF_VIRTUALDESK
    )
    assert [events[i].mi.dwFlags for i in (1, 2)] == [
        native._MOUSEEVENTF_LEFTDOWN,
        native._MOUSEEVENTF_LEFTUP,
    ]


@pytest.mark.parametrize("result", [0, 1, 2])
def test_partial_sendinput_failure_cleans_up_only_when_down_may_be_held(result):
    api = FakeInputAPI(result=result)

    with pytest.raises(native.SafetyError, match="inserted"):
        native.WindowsInput(_api=api).click((-1920, -100), threading.Event())

    assert api.calls[0][0] == 3
    if result == 2:
        assert len(api.calls) == 2
        assert api.calls[1][0] == 1
        assert api.calls[1][1][0].mi.dwFlags == native._MOUSEEVENTF_LEFTUP
    else:
        assert len(api.calls) == 1


def test_sendinput_exception_also_attempts_one_up_only_cleanup():
    api = FakeInputAPI(failure=OSError("simulated SendInput failure"))

    with pytest.raises(OSError, match="simulated") as caught:
        native.WindowsInput(_api=api).click((-1920, -100), threading.Event())

    assert len(api.calls) == 2
    assert api.calls[1][1][0].mi.dwFlags == native._MOUSEEVENTF_LEFTUP
    assert not getattr(caught.value, "__notes__", [])


def test_cleanup_failure_is_attached_to_original_sendinput_error():
    class FailingCleanupAPI(FakeInputAPI):
        def SendInput(self, count, events, size):
            self.calls.append((count, events, size))
            if count == 3:
                raise OSError("primary failure")
            raise OSError("cleanup failure")

    api = FailingCleanupAPI()
    with pytest.raises(OSError, match="primary failure") as caught:
        native.WindowsInput(_api=api).click((-1920, -100), threading.Event())

    assert len(api.calls) == 2
    assert any("cleanup failure" in note for note in caught.value.__notes__)


def test_input_refuses_cancelled_events_held_buttons_modifiers_and_outside_points():
    api = FakeInputAPI()
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(native.SafetyError, match="cancelled"):
        native.WindowsInput(_api=api).click((-1920, -100), cancelled)
    assert not api.calls

    api.pressed.add(0xA2)  # VK_LCONTROL
    with pytest.raises(native.SafetyError, match="physical"):
        native.WindowsInput(_api=api).click((-1920, -100), threading.Event())
    assert not api.calls

    api.pressed.clear()
    with pytest.raises(native.SafetyError, match="outside"):
        native.WindowsInput(_api=api).click((1920, 0), threading.Event())
    assert not api.calls


def test_input_checks_mouse_buttons_as_well_as_modifiers():
    api = FakeInputAPI()
    api.pressed.add(0x01)

    with pytest.raises(native.SafetyError, match="physical"):
        native.WindowsInput(_api=api).click((-1920, -100), threading.Event())

    assert not api.calls


def test_ctypes_input_layout_matches_windows_pointer_width():
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(native._MOUSEINPUT) == 32
        assert ctypes.sizeof(native._INPUT) == 40
    else:
        assert ctypes.sizeof(native._MOUSEINPUT) == 24
        assert ctypes.sizeof(native._INPUT) == 28


class FakeWindowAPI:
    def __init__(self):
        self.client_origin = (-100, 20)
        self.client_size = (40, 30)
        self.dpi = 144
        self.creation_time = 0x123456789ABC
        self.process_path = r"C:\Apps\target.exe"
        self.gdi_bytes = bytes([3, 7, 11, 0] * (self.client_size[0] * self.client_size[1]))
        self.gdi_buffer = None
        self.released = []
        self.gui_flags = 0
        self.z_order = [2, 1]
        self.window_rects = {2: (0, 0, 10, 10)}
        self.cloaked: set[int] = set()
        self.enumerated_windows = [1]
        self.event_log = []
        self.bitblt_args = []
        self.release_dc_args = []

    @staticmethod
    def _contents(pointer, kind):
        return ctypes.cast(pointer, ctypes.POINTER(kind)).contents

    def IsWindow(self, hwnd):
        return native._handle_value(hwnd) == 1

    def IsWindowVisible(self, hwnd):
        return True

    def IsIconic(self, hwnd):
        return False

    def EnumWindows(self, callback, _lparam):
        return all(callback(native._handle(hwnd), 0) for hwnd in self.enumerated_windows)

    def GetWindowTextLengthW(self, _hwnd):
        return len("Target title")

    def GetWindowTextW(self, _hwnd, buffer, _length):
        buffer.value = "Target title"
        return len(buffer.value)

    def GetWindowThreadProcessId(self, hwnd, pid_pointer):
        if pid_pointer is not None:
            self._contents(pid_pointer, native._DWORD).value = 44
        return 55

    def OpenProcess(self, _access, _inherit, pid):
        assert pid == 44
        return 77

    def QueryFullProcessImageNameW(self, _process, _flags, buffer, length_pointer):
        buffer.value = self.process_path
        self._contents(length_pointer, native._DWORD).value = len(self.process_path)
        return 1

    def GetProcessTimes(self, _process, creation, _exit, _kernel, _user):
        value = self._contents(creation, native._FILETIME)
        value.dwLowDateTime = self.creation_time & 0xFFFFFFFF
        value.dwHighDateTime = self.creation_time >> 32
        return 1

    def CloseHandle(self, _handle):
        return 1

    def GetClientRect(self, _hwnd, rect_pointer):
        rect = self._contents(rect_pointer, native._RECT)
        rect.left = rect.top = 0
        rect.right, rect.bottom = self.client_size
        return 1

    def ClientToScreen(self, _hwnd, point_pointer):
        point = self._contents(point_pointer, native._POINT)
        point.x += self.client_origin[0]
        point.y += self.client_origin[1]
        return 1

    def GetDpiForWindow(self, _hwnd):
        return self.dpi

    def GetSystemMetrics(self, metric):
        return {76: -1920, 77: -200, 78: 3840, 79: 2160}[metric]

    def GetForegroundWindow(self):
        return 1

    def GetGUIThreadInfo(self, _thread, info_pointer):
        self._contents(info_pointer, native._GUITHREADINFO).flags = self.gui_flags
        return 1

    def GetTopWindow(self, _hwnd):
        return self.z_order[0] if self.z_order else 0

    def GetWindow(self, hwnd, command):
        assert command == 2
        current = native._handle_value(hwnd)
        try:
            return self.z_order[self.z_order.index(current) + 1]
        except (ValueError, IndexError):
            return 0

    def GetWindowRect(self, hwnd, rect_pointer):
        left, top, right, bottom = self.window_rects[native._handle_value(hwnd)]
        rect = self._contents(rect_pointer, native._RECT)
        rect.left, rect.top, rect.right, rect.bottom = left, top, right, bottom
        return 1

    def DwmGetWindowAttribute(self, hwnd, attribute, output, size):
        assert attribute == 14
        assert size == ctypes.sizeof(native._DWORD)
        self._contents(output, native._DWORD).value = int(
            native._handle_value(hwnd) in self.cloaked
        )
        return 0

    def GetDC(self, hwnd):
        assert hwnd is None
        self.event_log.append("get_screen_dc")
        return 31

    def CreateCompatibleDC(self, _dc):
        return 32

    def CreateDIBSection(self, _dc, info_pointer, _usage, bits_pointer, _section, _offset):
        info = self._contents(info_pointer, native._BITMAPINFO)
        assert info.bmiHeader.biHeight == -self.client_size[1]
        self.gdi_buffer = ctypes.create_string_buffer(self.gdi_bytes)
        self._contents(bits_pointer, native._HANDLE).value = ctypes.addressof(self.gdi_buffer)
        return 33

    def SelectObject(self, _dc, bitmap):
        if native._handle_value(bitmap) == 33:
            return 44
        self.released.append("restore")
        return 33

    def BitBlt(self, *_args):
        self.bitblt_args.append(_args)
        self.event_log.append("bitblt")
        return 1

    def DeleteObject(self, _bitmap):
        self.released.append("bitmap")
        return 1

    def DeleteDC(self, _dc):
        self.released.append("memory_dc")
        return 1

    def ReleaseDC(self, _hwnd, _dc):
        self.release_dc_args.append((_hwnd, _dc))
        self.released.append("source_dc")
        return 1


def test_inspect_and_capture_return_stable_physical_bgr_frame_and_release_gdi(monkeypatch):
    api = FakeWindowAPI()
    backend = native.WindowsBackend(_api=api)
    identity = Identity(1, 44, api.creation_time, api.process_path, "old title")
    timestamps = iter((12.5, 19.0))

    def capture_time():
        api.event_log.append("timestamp")
        return next(timestamps)

    monkeypatch.setattr(native.time, "monotonic", capture_time)

    geometry = backend.inspect(identity)
    first = backend.capture(identity, "session-a")
    second = backend.capture(identity, "session-a")

    assert geometry == Geometry(-100, 20, 40, 30, 144)
    assert first.image.shape == (30, 40, 3)
    assert first.image.dtype == np.uint8
    assert first.image.flags.c_contiguous
    assert first.image[0, 0].tolist() == [3, 7, 11]
    assert first.sequence == 1 and second.sequence == 2
    assert first.captured_at == 12.5
    assert second.captured_at == 19.0
    assert api.event_log.index("timestamp") < api.event_log.index("get_screen_dc")
    first_blt = api.bitblt_args[0]
    assert first_blt[1:5] == (0, 0, 40, 30)
    assert first_blt[6:8] == (-100, 20)
    assert first_blt[8] == 0x40CC0020  # SRCCOPY | CAPTUREBLT
    assert all(hwnd is None for hwnd, _dc in api.release_dc_args)
    assert api.released.count("restore") == 2
    assert api.released.count("bitmap") == 2
    assert api.released.count("memory_dc") == 2
    assert api.released.count("source_dc") == 2


def test_list_windows_builds_identity_from_hwnd_process_and_title():
    api = FakeWindowAPI()
    api.enumerated_windows = [1]
    backend = native.WindowsBackend(_api=api)

    assert backend.list_windows() == [
        Identity(1, 44, api.creation_time, api.process_path, "Target title")
    ]


def test_capture_rejects_changed_geometry_after_releasing_gdi_resources():
    class MovingWindowAPI(FakeWindowAPI):
        def __init__(self):
            super().__init__()
            self.inspect_count = 0

        def GetClientRect(self, hwnd, rect_pointer):
            self.inspect_count += 1
            if self.inspect_count > 1:
                self.client_size = (41, 30)
                self.gdi_bytes = bytes([3, 7, 11, 0] * (41 * 30))
            return super().GetClientRect(hwnd, rect_pointer)

    api = MovingWindowAPI()
    backend = native.WindowsBackend(_api=api)
    identity = Identity(1, 44, api.creation_time, api.process_path)

    with pytest.raises(native.SafetyError, match="changed during capture"):
        backend.capture(identity, "session")

    assert {"bitmap", "memory_dc", "source_dc"}.issubset(api.released)


def test_bitblt_failure_still_releases_all_created_gdi_resources():
    class FailedBltAPI(FakeWindowAPI):
        def BitBlt(self, *_args):
            return 0

    api = FailedBltAPI()
    backend = native.WindowsBackend(_api=api)
    identity = Identity(1, 44, api.creation_time, api.process_path)

    with pytest.raises(native.SafetyError, match="BitBlt"):
        backend.capture(identity, "session")

    assert {"restore", "bitmap", "memory_dc", "source_dc"}.issubset(api.released)


@pytest.mark.parametrize("client_size", [(16_385, 1), (8_193, 4_050)])
def test_inspect_rejects_capture_size_over_dimension_or_pixel_limit(client_size):
    api = FakeWindowAPI()
    api.client_size = client_size
    backend = native.WindowsBackend(_api=api)
    identity = Identity(1, 44, api.creation_time, api.process_path)

    with pytest.raises(native.SafetyError, match="capture limits"):
        backend.inspect(identity)

    assert not api.event_log


def test_inspect_rejects_dwm_cloaked_target():
    api = FakeWindowAPI()
    api.cloaked.add(1)
    backend = native.WindowsBackend(_api=api)
    identity = Identity(1, 44, api.creation_time, api.process_path)

    with pytest.raises(native.SafetyError, match="cloaked"):
        backend.inspect(identity)


def test_unobscured_checks_visible_uncloaked_windows_above_target():
    api = FakeWindowAPI()
    backend = native.WindowsBackend(_api=api)
    identity = Identity(1, 44, api.creation_time, api.process_path)
    geometry = backend.inspect(identity)
    target = Rect(0, 0, 10, 10)  # screen position (-100, 20)

    api.window_rects[2] = (-101, 19, -90, 30)
    assert not backend.unobscured(identity, target, geometry)

    api.window_rects[2] = (100, 100, 110, 110)
    assert backend.unobscured(identity, target, geometry)

    api.window_rects[2] = (-101, 19, -90, 30)
    api.cloaked.add(2)
    assert backend.unobscured(identity, target, geometry)


def test_moving_reports_gui_move_size_state_and_fails_closed_on_query_failure():
    class FailedQueryAPI(FakeWindowAPI):
        def GetGUIThreadInfo(self, _thread, _info_pointer):
            return 0

    api = FakeWindowAPI()
    backend = native.WindowsBackend(_api=api)
    identity = Identity(1, 44, api.creation_time, api.process_path)
    api.gui_flags = 0x0002
    assert backend.moving(identity)
    api.gui_flags = 0
    assert not backend.moving(identity)
    assert native.WindowsBackend(_api=FailedQueryAPI()).moving(identity)


def test_dpi_enable_is_idempotent_only_when_context_is_already_per_monitor_v2():
    class DPIAPI:
        last_error = 5

        def SetProcessDpiAwarenessContext(self, context):
            assert native._handle_value(context) == native._handle_value(ctypes.c_void_p(-4))
            return 0

        def GetLastError(self):
            return self.last_error

        def GetThreadDpiAwarenessContext(self):
            return ctypes.c_void_p(-4)

        def AreDpiAwarenessContextsEqual(self, current, expected):
            return native._handle_value(current) == native._handle_value(expected)

    native.enable_dpi_awareness(_api=DPIAPI())

    class WrongContextAPI(DPIAPI):
        def AreDpiAwarenessContextsEqual(self, _current, _expected):
            return 0

    with pytest.raises(native.SafetyError, match="Windows error 5"):
        native.enable_dpi_awareness(_api=WrongContextAPI())


class FakeHotkeyAPI:
    def __init__(self, *, register_result=True, last_error=0, send_hotkey=False):
        self.register_result = register_result
        self.last_error = last_error
        self.messages: queue.Queue[object] = queue.Queue()
        self.register_args = None
        self.unregistered = 0
        if send_hotkey:
            self.messages.put(native._WM_HOTKEY)

    def PeekMessageW(self, *_args):
        return 0

    def GetCurrentThreadId(self):
        return 501

    def SetLastError(self, code):
        self.last_error = code

    def RegisterHotKey(self, hwnd, hotkey_id, modifiers, key):
        self.register_args = (hwnd, hotkey_id, modifiers, key)
        if not self.register_result:
            self.last_error = 1409
        return self.register_result

    def GetLastError(self):
        return self.last_error

    def GetMessageW(self, message_pointer, _hwnd, _minimum, _maximum):
        try:
            message = self.messages.get(timeout=1.5)
        except queue.Empty:
            message = native._WM_QUIT
        if isinstance(message, BaseException):
            raise message
        if message == -1:
            self.last_error = 6
            return -1
        output = ctypes.cast(message_pointer, ctypes.POINTER(native._MSG)).contents
        output.message = message
        output.wParam = native._HOTKEY_ID if message == native._WM_HOTKEY else 0
        return 0 if message == native._WM_QUIT else 1

    def PostThreadMessageW(self, thread_id, message, _wparam, _lparam):
        assert thread_id == 501
        self.messages.put(message)
        return 1

    def UnregisterHotKey(self, _hwnd, _hotkey_id):
        self.unregistered += 1
        return 1


def test_hotkey_registration_failure_is_reported_without_substitution():
    api = FakeHotkeyAPI(register_result=False, last_error=1409)
    hotkey = native.GlobalHotkey("Ctrl+Alt+F11", lambda: None, _api=api)

    assert not hotkey.start()
    assert not hotkey.available
    assert "RegisterHotKey(Ctrl+Alt+F11)" in hotkey.error
    assert "Windows error 1409" in hotkey.error
    assert api.register_args == (None, native._HOTKEY_ID, 0x4000 | 0x0002 | 0x0001, 0x7A)
    assert api.unregistered == 0
    hotkey.close()


@pytest.mark.parametrize("spec", [
    "Ctrl+Alt+F12",
    "ctrl + alt + f12",
    "CTRL+ALT+F012",
    " SHIFT + f12 ",
    "F12",
    "Ctrl+F 12",
])
def test_reserved_f12_variants_fail_before_os_registration(spec):
    api = FakeHotkeyAPI()
    hotkey = native.GlobalHotkey(spec, lambda: None, _api=api)

    assert not hotkey.start()
    assert not hotkey.available
    assert "F12 已保留" in hotkey.error
    assert api.register_args is None
    assert api.unregistered == 0
    hotkey.close()


def test_default_global_hotkey_is_f11():
    from auto_ready.models import Settings

    assert Settings().hotkey == "Ctrl+Alt+F11"
    assert native._parse_hotkey(Settings().hotkey) == (0x4000 | 0x0002 | 0x0001, 0x7A)


def test_hotkey_callback_runs_on_message_thread_and_close_unregisters():
    api = FakeHotkeyAPI(send_hotkey=True)
    callback_called = threading.Event()
    callback_calls = []

    def callback():
        callback_calls.append(True)
        callback_called.set()

    hotkey = native.GlobalHotkey("Ctrl+Alt+F11", callback, _api=api)

    assert hotkey.start()
    assert callback_called.wait(timeout=1)
    hotkey.close()

    assert api.register_args[3] == 0x7A
    assert api.unregistered == 1
    assert callback_calls == [True]
    assert not hotkey.available


@pytest.mark.parametrize(
    ("exit_message", "expected_error"),
    [
        (native._WM_QUIT, "unexpectedly (WM_QUIT)"),
        (-1, "GetMessageW failed (Windows error 6)"),
        (OSError("simulated message-loop failure"), "simulated message-loop failure"),
    ],
)
def test_unexpected_hotkey_message_loop_exit_cancels_after_marking_unavailable(
    exit_message, expected_error
):
    api = FakeHotkeyAPI()
    callback_state = []
    callback_called = threading.Event()
    hotkey = None

    def callback():
        callback_state.append((hotkey.available, hotkey.error))
        callback_called.set()

    hotkey = native.GlobalHotkey("Ctrl+Alt+F11", callback, _api=api)
    assert hotkey.start()
    api.messages.put(exit_message)

    assert callback_called.wait(timeout=1)
    hotkey._thread.join(timeout=1)

    assert not hotkey._thread.is_alive()
    assert callback_state == [(False, hotkey.error)]
    assert expected_error in hotkey.error
    assert api.unregistered == 1
    hotkey.close()


def test_intentional_hotkey_close_does_not_invoke_exit_callback():
    api = FakeHotkeyAPI()
    callback_calls = []
    hotkey = native.GlobalHotkey(
        "Ctrl+Alt+F11", lambda: callback_calls.append(True), _api=api
    )

    assert hotkey.start()
    hotkey.close()

    assert callback_calls == []
    assert api.unregistered == 1
    assert not hotkey.error


def test_invalid_hotkey_fails_before_registering():
    api = FakeHotkeyAPI()
    hotkey = native.GlobalHotkey("Ctrl+Alt", lambda: None, _api=api)

    assert not hotkey.start()
    assert "exactly one" in hotkey.error
    assert api.register_args is None


def test_session_mutex_detects_existing_instance_and_releases_handles():
    class MutexAPI:
        def __init__(self, create_error):
            self.create_error = create_error
            self.error = 0
            self.closed = []
            self.name = None

        def SetLastError(self, code):
            self.error = code

        def CreateMutexW(self, _security, initial_owner, name):
            assert not initial_owner
            self.name = name
            self.error = self.create_error
            return 91

        def GetLastError(self):
            return self.error

        def CloseHandle(self, handle):
            self.closed.append(native._handle_value(handle))
            return 1

    new_api = MutexAPI(0)
    instance = native.SingleInstance(_api=new_api)
    assert instance.acquire()
    assert instance.acquire()
    assert new_api.name.startswith("Local\\")
    instance.close()
    instance.close()
    assert new_api.closed == [91]

    existing_api = MutexAPI(183)
    existing = native.SingleInstance(_api=existing_api)
    assert not existing.acquire()
    assert existing_api.closed == [91]
