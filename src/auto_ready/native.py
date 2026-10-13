"""Windows desktop, input, hotkey, and single-instance support.

The module itself is importable on every platform. Windows APIs are loaded only
when a native object is constructed, which keeps model and UI tests portable.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

import numpy as np

from .models import Frame, Geometry, Identity, Rect


class SafetyError(RuntimeError):
    """A native operation could not be completed safely."""


_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_WORD = ctypes.c_uint16
_SHORT = ctypes.c_int16
_LONG = ctypes.c_int32
_UINT = ctypes.c_uint32
_INT = ctypes.c_int32
_WPARAM = ctypes.c_size_t
_LPARAM = ctypes.c_ssize_t
_HANDLE = ctypes.c_void_p
_HWND = ctypes.c_void_p
_HDC = ctypes.c_void_p
_HBITMAP = ctypes.c_void_p
_HGDIOBJ = ctypes.c_void_p
_ULONG_PTR = ctypes.c_size_t
_CALLBACK = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
_WNDENUMPROC = _CALLBACK(_BOOL, _HWND, ctypes.c_ssize_t)
_MAX_CAPTURE_DIMENSION = 16_384
_MAX_CAPTURE_PIXELS = 33_177_600


class _POINT(ctypes.Structure):
    _fields_ = [("x", _LONG), ("y", _LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", _LONG), ("top", _LONG), ("right", _LONG), ("bottom", _LONG)]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", _DWORD), ("dwHighDateTime", _DWORD)]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", _DWORD),
        ("flags", _DWORD),
        ("hwndActive", _HWND),
        ("hwndFocus", _HWND),
        ("hwndCapture", _HWND),
        ("hwndMenuOwner", _HWND),
        ("hwndMoveSize", _HWND),
        ("hwndCaret", _HWND),
        ("rcCaret", _RECT),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", _DWORD),
        ("biWidth", _LONG),
        ("biHeight", _LONG),
        ("biPlanes", _WORD),
        ("biBitCount", _WORD),
        ("biCompression", _DWORD),
        ("biSizeImage", _DWORD),
        ("biXPelsPerMeter", _LONG),
        ("biYPelsPerMeter", _LONG),
        ("biClrUsed", _DWORD),
        ("biClrImportant", _DWORD),
    ]


class _RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _RGBQUAD * 1)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", _LONG),
        ("dy", _LONG),
        ("mouseData", _DWORD),
        ("dwFlags", _DWORD),
        ("time", _DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("padding", ctypes.c_byte * ctypes.sizeof(_MOUSEINPUT))]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", _DWORD), ("data", _INPUTUNION)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", _HWND),
        ("message", _UINT),
        ("wParam", _WPARAM),
        ("lParam", _LPARAM),
        ("time", _DWORD),
        ("pt", _POINT),
        ("lPrivate", _DWORD),
    ]


class _CtypesWin32:
    """Small ctypes binding surface with explicit pointer-width prototypes."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows native support is available only on Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        try:
            self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        except OSError:
            self.dwmapi = None
        self._bind()

    def __getattr__(self, name: str):
        for library in (self.user32, self.kernel32, self.gdi32, self.dwmapi):
            if library is not None and hasattr(library, name):
                return getattr(library, name)
        raise AttributeError(name)

    @staticmethod
    def _prototype(library, name, argtypes, restype):
        function = getattr(library, name, None)
        if function is not None:
            function.argtypes = argtypes
            function.restype = restype
        return function

    def _bind(self) -> None:
        p_dword = ctypes.POINTER(_DWORD)
        p_filetime = ctypes.POINTER(_FILETIME)
        p_rect = ctypes.POINTER(_RECT)
        p_point = ctypes.POINTER(_POINT)
        p_gui = ctypes.POINTER(_GUITHREADINFO)
        p_msg = ctypes.POINTER(_MSG)
        p_input = ctypes.POINTER(_INPUT)
        p_bitmapinfo = ctypes.POINTER(_BITMAPINFO)
        p_handle = ctypes.POINTER(_HANDLE)
        prototypes = [
            (self.kernel32, "OpenProcess", [_DWORD, _BOOL, _DWORD], _HANDLE),
            (self.kernel32, "CloseHandle", [_HANDLE], _BOOL),
            (
                self.kernel32,
                "QueryFullProcessImageNameW",
                [_HANDLE, _DWORD, wintypes.LPWSTR, p_dword],
                _BOOL,
            ),
            (
                self.kernel32,
                "GetProcessTimes",
                [_HANDLE, p_filetime, p_filetime, p_filetime, p_filetime],
                _BOOL,
            ),
            (self.kernel32, "CreateMutexW", [_HANDLE, _BOOL, wintypes.LPCWSTR], _HANDLE),
            (self.kernel32, "GetLastError", [], _DWORD),
            (self.kernel32, "SetLastError", [_DWORD], None),
            (self.kernel32, "GetCurrentThreadId", [], _DWORD),
            (self.user32, "SetProcessDpiAwarenessContext", [_HANDLE], _BOOL),
            (self.user32, "GetThreadDpiAwarenessContext", [], _HANDLE),
            (self.user32, "AreDpiAwarenessContextsEqual", [_HANDLE, _HANDLE], _BOOL),
            (self.user32, "EnumWindows", [_WNDENUMPROC, _LPARAM], _BOOL),
            (self.user32, "GetWindowThreadProcessId", [_HWND, p_dword], _DWORD),
            (self.user32, "IsWindow", [_HWND], _BOOL),
            (self.user32, "IsWindowVisible", [_HWND], _BOOL),
            (self.user32, "IsIconic", [_HWND], _BOOL),
            (self.user32, "GetWindowTextLengthW", [_HWND], _INT),
            (self.user32, "GetWindowTextW", [_HWND, wintypes.LPWSTR, _INT], _INT),
            (self.user32, "GetClientRect", [_HWND, p_rect], _BOOL),
            (self.user32, "ClientToScreen", [_HWND, p_point], _BOOL),
            (self.user32, "GetDpiForWindow", [_HWND], _UINT),
            (self.user32, "GetSystemMetrics", [_INT], _INT),
            (self.user32, "GetForegroundWindow", [], _HWND),
            (self.user32, "GetGUIThreadInfo", [_DWORD, p_gui], _BOOL),
            (self.user32, "GetAsyncKeyState", [_INT], _SHORT),
            (self.user32, "SendInput", [_UINT, p_input, _INT], _UINT),
            (self.user32, "RegisterHotKey", [_HWND, _INT, _UINT, _UINT], _BOOL),
            (self.user32, "UnregisterHotKey", [_HWND, _INT], _BOOL),
            (self.user32, "PeekMessageW", [p_msg, _HWND, _UINT, _UINT, _UINT], _BOOL),
            (self.user32, "GetMessageW", [p_msg, _HWND, _UINT, _UINT], _INT),
            (self.user32, "PostThreadMessageW", [_DWORD, _UINT, _WPARAM, _LPARAM], _BOOL),
            (self.user32, "GetWindowRect", [_HWND, p_rect], _BOOL),
            (self.user32, "GetTopWindow", [_HWND], _HWND),
            (self.user32, "GetWindow", [_HWND, _UINT], _HWND),
            (self.user32, "GetDC", [_HWND], _HDC),
            (self.user32, "ReleaseDC", [_HWND, _HDC], _INT),
            (self.gdi32, "CreateCompatibleDC", [_HDC], _HDC),
            (self.gdi32, "DeleteDC", [_HDC], _BOOL),
            (
                self.gdi32,
                "CreateDIBSection",
                [_HDC, p_bitmapinfo, _UINT, p_handle, _HANDLE, _DWORD],
                _HBITMAP,
            ),
            (self.gdi32, "SelectObject", [_HDC, _HGDIOBJ], _HGDIOBJ),
            (self.gdi32, "BitBlt", [_HDC, _INT, _INT, _INT, _INT, _HDC, _INT, _INT, _DWORD], _BOOL),
            (self.gdi32, "DeleteObject", [_HGDIOBJ], _BOOL),
        ]
        if self.dwmapi is not None:
            prototypes.append(
                (
                    self.dwmapi,
                    "DwmGetWindowAttribute",
                    [_HWND, _DWORD, ctypes.POINTER(_DWORD), _DWORD],
                    _INT,
                )
            )
        for library, name, argtypes, restype in prototypes:
            self._prototype(library, name, argtypes, restype)


def _load_api():
    return _CtypesWin32()


def _handle(value) -> ctypes.c_void_p:
    if isinstance(value, ctypes.c_void_p):
        return value
    if value is None:
        return ctypes.c_void_p()
    return ctypes.c_void_p(int(value))


def _handle_value(value) -> int:
    if isinstance(value, ctypes.c_void_p):
        value = value.value
    return int(value or 0)


def _last_error(api) -> int:
    try:
        return int(api.GetLastError())
    except (AttributeError, OSError):
        return int(ctypes.get_last_error()) if hasattr(ctypes, "get_last_error") else 0


def _set_last_error(api, code: int = 0) -> None:
    setter = getattr(api, "SetLastError", None)
    if setter is not None:
        setter(code)


def _format_win_error(api, operation: str, code: int | None = None) -> str:
    if code is None:
        code = _last_error(api)
    detail = ""
    formatter = getattr(api, "FormatError", None)
    try:
        if formatter is not None:
            detail = str(formatter(code)).strip()
        elif hasattr(ctypes, "FormatError"):
            detail = str(ctypes.FormatError(code)).strip()
    except (OSError, ValueError):
        detail = ""
    suffix = f": {detail}" if detail else ""
    return f"{operation} failed (Windows error {code}){suffix}"


def _api_failure(api, operation: str) -> SafetyError:
    return SafetyError(_format_win_error(api, operation))


def _enum_windows(api, callback: Callable[[int], bool]) -> bool:
    def visit(hwnd, _lparam):
        try:
            return bool(callback(_handle_value(hwnd)))
        except BaseException:
            return False

    proc = _WNDENUMPROC(visit)
    return bool(api.EnumWindows(proc, 0))


def _process_identity(api, hwnd: int, title: str = "") -> Identity | None:
    pid = _DWORD()
    thread_id = int(api.GetWindowThreadProcessId(_handle(hwnd), ctypes.byref(pid)))
    if not thread_id or not pid.value:
        return None
    process = api.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    process_value = _handle_value(process)
    if not process_value:
        return None
    try:
        path_buffer = ctypes.create_unicode_buffer(32768)
        path_length = _DWORD(len(path_buffer))
        if not api.QueryFullProcessImageNameW(
            _handle(process_value), 0, path_buffer, ctypes.byref(path_length)
        ):
            return None
        creation = _FILETIME()
        exit_time = _FILETIME()
        kernel_time = _FILETIME()
        user_time = _FILETIME()
        if not api.GetProcessTimes(
            _handle(process_value),
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        started = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return Identity(hwnd, int(pid.value), started, path_buffer.value, title)
    finally:
        api.CloseHandle(_handle(process_value))


def _virtual_desktop(api) -> tuple[int, int, int, int]:
    left = int(api.GetSystemMetrics(76))
    top = int(api.GetSystemMetrics(77))
    width = int(api.GetSystemMetrics(78))
    height = int(api.GetSystemMetrics(79))
    if width <= 0 or height <= 0:
        raise SafetyError("Windows reported an invalid virtual desktop size")
    return left, top, width, height


def _same_executable(left: str, right: str) -> bool:
    # Windows paths are case-insensitive; preserve the source strings in Identity.
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _identity_matches(actual: Identity | None, expected: Identity) -> bool:
    return bool(
        actual
        and actual.hwnd == expected.hwnd
        and actual.pid == expected.pid
        and actual.process_started == expected.process_started
        and _same_executable(actual.executable, expected.executable)
    )


def enable_dpi_awareness(*, _api=None) -> None:
    """Set the process default to Per-Monitor V2 before creating application HWNDs."""
    api = _load_api() if _api is None else _api
    setter = getattr(api, "SetProcessDpiAwarenessContext", None)
    if setter is None:
        raise RuntimeError("Per-Monitor V2 DPI awareness requires Windows 10 version 1607 or newer")
    context = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    _set_last_error(api)
    if setter(context):
        return
    error = _last_error(api)
    if error == 5:
        get_context = getattr(api, "GetThreadDpiAwarenessContext", None)
        compare = getattr(api, "AreDpiAwarenessContextsEqual", None)
        if get_context is not None and compare is not None:
            current = get_context()
            if compare(_handle(current), context):
                return
    raise SafetyError(_format_win_error(api, "SetProcessDpiAwarenessContext", error))


class WindowsBackend:
    """Read-only target-window inspection and physical-pixel client capture."""

    def __init__(self, *, _api=None) -> None:
        self._api = _load_api() if _api is None else _api
        self._sequence = 0
        self._sequence_lock = threading.Lock()

    def list_windows(self) -> list[Identity]:
        result: list[Identity] = []

        def collect(hwnd: int) -> bool:
            try:
                if not hwnd or not self._api.IsWindowVisible(_handle(hwnd)):
                    return True
                if self._api.IsIconic(_handle(hwnd)):
                    return True
                length = max(0, int(self._api.GetWindowTextLengthW(_handle(hwnd))))
                title = ""
                if length:
                    buffer = ctypes.create_unicode_buffer(min(length + 1, 32768))
                    self._api.GetWindowTextW(_handle(hwnd), buffer, len(buffer))
                    title = buffer.value
                identity = _process_identity(self._api, hwnd, title)
                if identity is not None:
                    result.append(identity)
            except Exception:
                # One inaccessible or transiently changing window must not hide others.
                return True
            return True

        _set_last_error(self._api)
        if not _enum_windows(self._api, collect):
            raise _api_failure(self._api, "EnumWindows")
        return result

    def _validate_identity(self, identity: Identity) -> int:
        if not isinstance(identity, Identity) or identity.hwnd <= 0:
            raise SafetyError("The selected window identity is invalid")
        hwnd = int(identity.hwnd)
        if not self._api.IsWindow(_handle(hwnd)):
            raise SafetyError("The selected window no longer exists")
        actual = _process_identity(self._api, hwnd)
        if not _identity_matches(actual, identity):
            raise SafetyError("The selected window identity changed; select it again")
        if not self._api.IsWindowVisible(_handle(hwnd)):
            raise SafetyError("The selected window is not visible")
        if self._api.IsIconic(_handle(hwnd)):
            raise SafetyError("The selected window is minimized")
        return hwnd

    def inspect(self, identity: Identity) -> Geometry:
        hwnd = self._validate_identity(identity)
        if self._is_cloaked(hwnd):
            raise SafetyError("The selected window is cloaked")
        client = _RECT()
        if not self._api.GetClientRect(_handle(hwnd), ctypes.byref(client)):
            raise _api_failure(self._api, "GetClientRect")
        width = int(client.right - client.left)
        height = int(client.bottom - client.top)
        if width <= 0 or height <= 0:
            raise SafetyError("The selected window has an empty client area")
        self._validate_capture_size(width, height)
        origin = _POINT(client.left, client.top)
        if not self._api.ClientToScreen(_handle(hwnd), ctypes.byref(origin)):
            raise _api_failure(self._api, "ClientToScreen")
        left, top, desktop_width, desktop_height = _virtual_desktop(self._api)
        if (
            origin.x < left
            or origin.y < top
            or origin.x + width > left + desktop_width
            or origin.y + height > top + desktop_height
        ):
            raise SafetyError("The selected client area is not entirely inside the virtual desktop")
        dpi = int(self._api.GetDpiForWindow(_handle(hwnd)))
        if dpi <= 0:
            raise SafetyError("Windows could not determine the selected window DPI")
        return Geometry(int(origin.x), int(origin.y), width, height, dpi)

    def foreground(self, identity: Identity) -> bool:
        try:
            hwnd = self._validate_identity(identity)
        except SafetyError:
            return False
        if _handle_value(self._api.GetForegroundWindow()) != hwnd:
            return False
        try:
            self._validate_identity(identity)
        except SafetyError:
            return False
        return True

    def moving(self, identity: Identity) -> bool:
        try:
            hwnd = self._validate_identity(identity)
        except SafetyError:
            return True
        pid = _DWORD()
        thread_id = int(self._api.GetWindowThreadProcessId(_handle(hwnd), ctypes.byref(pid)))
        if not thread_id or int(pid.value) != identity.pid:
            return True
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not self._api.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return True
        return bool(info.flags & 0x0002)  # GUI_INMOVESIZE

    def _is_cloaked(self, hwnd: int) -> bool:
        getter = getattr(self._api, "DwmGetWindowAttribute", None)
        if getter is None:
            return False
        cloaked = _DWORD()
        result = getter(_handle(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        # An unavailable DWM result is treated as uncloaked, the conservative case.
        return result == 0 and bool(cloaked.value)

    @staticmethod
    def _intersects(left: Rect, right: Rect) -> bool:
        return (
            left.x < right.x + right.width
            and right.x < left.x + left.width
            and left.y < right.y + right.height
            and right.y < left.y + left.height
        )

    def unobscured(self, identity: Identity, rect: Rect, geometry: Geometry) -> bool:
        if not rect.inside(geometry.width, geometry.height):
            return False
        try:
            hwnd = self._validate_identity(identity)
            current = self.inspect(identity)
        except SafetyError:
            return False
        if current != geometry:
            return False
        target = geometry.screen_rect(rect)
        current_hwnd = _handle_value(self._api.GetTopWindow(None))
        visited: set[int] = set()
        for _ in range(32768):
            if not current_hwnd or current_hwnd in visited:
                return False
            visited.add(current_hwnd)
            if current_hwnd == hwnd:
                try:
                    return self.inspect(identity) == geometry
                except SafetyError:
                    return False
            candidate = _handle(current_hwnd)
            if self._api.IsWindowVisible(candidate) and not self._api.IsIconic(candidate):
                if not self._is_cloaked(current_hwnd):
                    window_rect = _RECT()
                    if not self._api.GetWindowRect(candidate, ctypes.byref(window_rect)):
                        return False
                    bounds = Rect(
                        int(window_rect.left),
                        int(window_rect.top),
                        int(window_rect.right - window_rect.left),
                        int(window_rect.bottom - window_rect.top),
                    )
                    if self._intersects(bounds, target):
                        return False
            current_hwnd = _handle_value(self._api.GetWindow(candidate, 2))  # GW_HWNDNEXT
        return False

    def capture(self, identity: Identity, session_id: str) -> Frame:
        captured_at = time.monotonic()
        before = self.inspect(identity)
        image_bytes = self._capture_client(before)
        after = self.inspect(identity)
        if after != before:
            raise SafetyError("The selected window geometry or DPI changed during capture")
        pixels = np.frombuffer(image_bytes, dtype=np.uint8).reshape(before.height, before.width, 4)
        image = np.ascontiguousarray(pixels[:, :, :3])
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        return Frame(image, before, identity, captured_at, session_id, sequence)

    @staticmethod
    def _validate_capture_size(width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise SafetyError("The selected window has an empty client area")
        if (
            width > _MAX_CAPTURE_DIMENSION
            or height > _MAX_CAPTURE_DIMENSION
            or width * height > _MAX_CAPTURE_PIXELS
        ):
            raise SafetyError(
                f"The selected client area exceeds capture limits "
                f"({_MAX_CAPTURE_DIMENSION}px per dimension, {_MAX_CAPTURE_PIXELS} pixels)"
            )

    def _capture_client(self, geometry: Geometry) -> bytes:
        width, height = geometry.width, geometry.height
        self._validate_capture_size(width, height)
        source_dc = None
        memory_dc = None
        bitmap = None
        old_object = None
        selected = False
        original_error: BaseException | None = None
        cleanup_errors: list[str] = []
        pixels = b""

        try:
            source_dc = self._api.GetDC(None)
            if not _handle_value(source_dc):
                raise _api_failure(self._api, "GetDC")
            memory_dc = self._api.CreateCompatibleDC(_handle(source_dc))
            if not _handle_value(memory_dc):
                raise _api_failure(self._api, "CreateCompatibleDC")
            info = _BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down rows
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB, bytes are B,G,R,unused
            info.bmiHeader.biSizeImage = width * height * 4
            bits = _HANDLE()
            bitmap = self._api.CreateDIBSection(
                _handle(source_dc), ctypes.byref(info), 0, ctypes.byref(bits), None, 0
            )
            if not _handle_value(bitmap) or not _handle_value(bits):
                raise _api_failure(self._api, "CreateDIBSection")
            old_object = self._api.SelectObject(_handle(memory_dc), _handle(bitmap))
            old_value = _handle_value(old_object)
            if not old_value or old_value == _handle_value(_handle(-1)):
                raise _api_failure(self._api, "SelectObject")
            selected = True
            if not self._api.BitBlt(
                _handle(memory_dc),
                0,
                0,
                width,
                height,
                _handle(source_dc),
                geometry.left,
                geometry.top,
                0x40CC0020,  # SRCCOPY | CAPTUREBLT
            ):
                raise _api_failure(self._api, "BitBlt")
            pixels = ctypes.string_at(_handle_value(bits), width * height * 4)
        except BaseException as exc:
            original_error = exc
        finally:
            if selected and memory_dc and old_object:
                try:
                    self._api.SelectObject(_handle(memory_dc), _handle(old_object))
                except BaseException as exc:
                    cleanup_errors.append(f"restore selected GDI object: {exc}")
            if bitmap:
                try:
                    if not self._api.DeleteObject(_handle(bitmap)):
                        cleanup_errors.append(_format_win_error(self._api, "DeleteObject"))
                except BaseException as exc:
                    cleanup_errors.append(f"DeleteObject: {exc}")
            if memory_dc:
                try:
                    if not self._api.DeleteDC(_handle(memory_dc)):
                        cleanup_errors.append(_format_win_error(self._api, "DeleteDC"))
                except BaseException as exc:
                    cleanup_errors.append(f"DeleteDC: {exc}")
            if source_dc:
                try:
                    if not self._api.ReleaseDC(None, _handle(source_dc)):
                        cleanup_errors.append(_format_win_error(self._api, "ReleaseDC"))
                except BaseException as exc:
                    cleanup_errors.append(f"ReleaseDC: {exc}")

        if cleanup_errors:
            detail = "; ".join(cleanup_errors)
            if original_error is not None:
                original_error.add_note(f"GDI cleanup also reported: {detail}")
            else:
                raise SafetyError(f"GDI resource cleanup failed: {detail}")
        if original_error is not None:
            raise original_error
        return pixels


_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_MOUSEEVENTF_ABSOLUTE = 0x8000
_VK_MOUSE_BUTTONS = (0x01, 0x02, 0x04, 0x05, 0x06)
_VK_MODIFIERS = (0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)


class WindowsInput:
    """Guarded absolute virtual-desktop mouse input through SendInput."""

    def __init__(self, *, _api=None, observer=None) -> None:
        self._api = _load_api() if _api is None else _api
        self._observer = observer

    def _record_result(self, requested, sent, error, cleanup, win_error, returned_at=None):
        if self._observer is not None:
            try:
                self._observer("system_input_result", requested=requested, sent=sent,
                               error=error, cleanup=cleanup, win_error=win_error,
                               native_return_at=(returned_at if returned_at is not None
                                                 else time.monotonic()),
                               backend="WindowsInput/SendInput")
            except Exception:
                logging.getLogger(__name__).exception("系统输入验收记录失败，不能作为通过证据")

    @staticmethod
    def _normalize(point: tuple[int, int], desktop: tuple[int, int, int, int]) -> tuple[int, int]:
        if (
            not isinstance(point, tuple)
            or len(point) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in point)
        ):
            raise SafetyError("The input point must contain two physical integer coordinates")
        x, y = point
        left, top, width, height = desktop
        if not (left <= x < left + width and top <= y < top + height):
            raise SafetyError("The input point is outside the virtual desktop")
        if width < 2 or height < 2:
            raise SafetyError("The virtual desktop is too small for absolute mouse input")
        return (
            round((x - left) * 65535 / (width - 1)),
            round((y - top) * 65535 / (height - 1)),
        )

    def _check_cancelled(self, cancelled: threading.Event) -> None:
        if cancelled.is_set():
            raise SafetyError("Mouse input was cancelled")

    def _check_physical_input(self) -> None:
        pressed = [
            vk
            for vk in (*_VK_MOUSE_BUTTONS, *_VK_MODIFIERS)
            if int(self._api.GetAsyncKeyState(vk)) & 0x8000
        ]
        if pressed:
            names = ", ".join(f"0x{vk:02X}" for vk in pressed)
            raise SafetyError(
                "Mouse input refused while a physical mouse button or modifier is down "
                f"({names})"
            )

    @staticmethod
    def _input(flags: int, x: int = 0, y: int = 0) -> _INPUT:
        event = _INPUT()
        event.type = _INPUT_MOUSE
        event.mi = _MOUSEINPUT(x, y, 0, flags, 0, 0)
        return event

    def _release_left_button(self) -> None:
        release = (_INPUT * 1)(self._input(_MOUSEEVENTF_LEFTUP))
        _set_last_error(self._api)
        try:
            inserted = int(self._api.SendInput(1, release, ctypes.sizeof(_INPUT)))
        except BaseException as exc:
            self._record_result(1, None, str(exc), True, _last_error(self._api))
            raise
        error_code = _last_error(self._api)
        failure = (_api_failure(self._api, "SendInput left-button cleanup")
                   if inserted != 1 else None)
        self._record_result(1, inserted, str(failure) if failure else "", True, error_code)
        if inserted != 1:
            raise failure

    def click(self, point: tuple[int, int], cancelled: threading.Event) -> None:
        self._check_cancelled(cancelled)
        desktop = _virtual_desktop(self._api)
        x, y = self._normalize(point, desktop)
        events = (_INPUT * 3)(
            self._input(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK, x, y),
            self._input(_MOUSEEVENTF_LEFTDOWN),
            self._input(_MOUSEEVENTF_LEFTUP),
        )
        self._check_physical_input()
        self._check_cancelled(cancelled)
        attempted = False
        try:
            _set_last_error(self._api)
            self._check_cancelled(cancelled)
            attempted = True
            inserted = int(self._api.SendInput(3, events, ctypes.sizeof(_INPUT)))
        except BaseException as exc:
            if attempted:
                return_time, error_code = time.monotonic(), _last_error(self._api)
                try:
                    self._release_left_button()
                except BaseException as cleanup_error:
                    exc.add_note(f"Left-button release cleanup also failed: {cleanup_error}")
                self._record_result(3, None, str(exc), False, error_code, return_time)
            raise
        return_time = time.monotonic()
        error_code = _last_error(self._api)
        failure = (_api_failure(self._api, f"SendInput inserted {inserted} of 3 click events")
                   if inserted != 3 else None)
        if inserted != 3:
            # A returned count of zero or one proves Windows did not insert the
            # left-button-down event. Two means the down may be held, so send
            # only an up event; never retry the click batch.
            if inserted >= 2:
                try:
                    self._release_left_button()
                except BaseException as cleanup_error:
                    failure.add_note(f"Left-button release cleanup also failed: {cleanup_error}")
            self._record_result(3, inserted, str(failure), False, error_code, return_time)
            raise failure
        self._record_result(3, inserted, "", False, error_code, return_time)


_MOD_CONTROL = 0x0002
_MOD_ALT = 0x0001
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_PM_NOREMOVE = 0x0000
_HOTKEY_ID = 0x5A71


def hotkey_policy_reason(spec: str) -> str | None:
    """Return the migration policy message for any spelling of reserved F12."""
    if not isinstance(spec, str):
        return None
    for part in spec.split("+"):
        name = "".join(part.split()).casefold()
        if (name.startswith("f") and name[1:].isdigit()
                and name[1:].lstrip("0") == "12"):
            return (
                "F12 已保留；旧配置不会自动迁移。请明确选择非 F12 停止键 "
                "（建议 Ctrl+Alt+F11）。"
            )
    return None


def _parse_hotkey(spec: str) -> tuple[int, int]:
    policy_reason = hotkey_policy_reason(spec)
    if policy_reason is not None:
        raise ValueError(policy_reason)
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("Hotkey must be a non-empty combination such as Ctrl+Alt+F11")
    names = [part.strip().casefold() for part in spec.split("+")]
    if any(not part for part in names) or len(set(names)) != len(names):
        raise ValueError(f"Invalid hotkey specification: {spec!r}")
    modifiers = {
        "ctrl": _MOD_CONTROL,
        "control": _MOD_CONTROL,
        "alt": _MOD_ALT,
        "shift": _MOD_SHIFT,
        "win": _MOD_WIN,
        "windows": _MOD_WIN,
    }
    modifier_bits = 0
    key = None
    for name in names:
        if name in modifiers:
            bit = modifiers[name]
            if modifier_bits & bit:
                raise ValueError(f"Duplicate hotkey modifier in {spec!r}")
            modifier_bits |= bit
        elif key is None:
            if len(name) == 1 and name.isascii() and name.isalpha():
                key = ord(name.upper())
            elif len(name) == 1 and name.isascii() and name.isdigit():
                key = ord(name)
            elif name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
                key = 0x70 + int(name[1:]) - 1
            else:
                key_codes = {
                    "space": 0x20,
                    "tab": 0x09,
                    "escape": 0x1B,
                    "esc": 0x1B,
                    "enter": 0x0D,
                    "insert": 0x2D,
                    "delete": 0x2E,
                    "home": 0x24,
                    "end": 0x23,
                    "pageup": 0x21,
                    "pagedown": 0x22,
                    "up": 0x26,
                    "down": 0x28,
                    "left": 0x25,
                    "right": 0x27,
                }
                key = key_codes.get(name)
                if key is None:
                    raise ValueError(f"Unsupported hotkey key: {name!r}")
        else:
            raise ValueError(f"A hotkey must contain exactly one non-modifier key: {spec!r}")
    if key is None:
        raise ValueError(f"A hotkey must contain exactly one non-modifier key: {spec!r}")
    if not modifier_bits:
        raise ValueError("A global hotkey must include at least one modifier")
    return modifier_bits | _MOD_NOREPEAT, key


class GlobalHotkey:
    """Thread-registered global hotkey with bounded startup and shutdown waits."""

    def __init__(self, spec: str, callback: Callable[[], None], *, _api=None) -> None:
        self.spec = spec
        self.callback = callback
        self._api = _load_api() if _api is None else _api
        self.error = ""
        self.available = False
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._registered = False
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None:
                return self.available
            try:
                modifiers, key = _parse_hotkey(self.spec)
            except ValueError as exc:
                self.error = str(exc)
                return False
            self._modifiers = modifiers
            self._key = key
            self._thread = threading.Thread(
                target=self._message_loop, name="AutoReadyHotkey", daemon=True
            )
            self._thread.start()
        if not self._ready.wait(timeout=2.0):
            self.error = "Timed out while starting the global hotkey message loop"
            self._stop.set()
            if self._thread_id:
                try:
                    self._api.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
                except BaseException:
                    pass
            self._thread.join(timeout=2.0)
            return False
        return self.available

    def _message_loop(self) -> None:
        message = _MSG()
        registered = False
        unexpected_exit = False
        try:
            self._api.PeekMessageW(ctypes.byref(message), None, 0, 0, _PM_NOREMOVE)
            self._thread_id = int(self._api.GetCurrentThreadId())
            if self._stop.is_set():
                return
            _set_last_error(self._api)
            if not self._api.RegisterHotKey(None, _HOTKEY_ID, self._modifiers, self._key):
                self.error = _format_win_error(self._api, f"RegisterHotKey({self.spec})")
                return
            self._registered = True
            registered = True
            self.available = True
            self._ready.set()
            while True:
                result = int(self._api.GetMessageW(ctypes.byref(message), None, 0, 0))
                if result == 0:
                    if not self._stop.is_set():
                        self.error = "Global hotkey message loop exited unexpectedly (WM_QUIT)"
                        unexpected_exit = True
                    break
                if result == -1:
                    self.error = _format_win_error(self._api, "GetMessageW")
                    unexpected_exit = not self._stop.is_set()
                    break
                if int(message.message) == _WM_HOTKEY and int(message.wParam) == _HOTKEY_ID:
                    try:
                        self.callback()
                    except BaseException as exc:
                        self.error = f"Global hotkey callback failed: {exc}"
        except BaseException as exc:
            self.error = f"Global hotkey message loop failed: {exc}"
            unexpected_exit = registered and not self._stop.is_set()
        finally:
            self.available = False
            if self._registered:
                try:
                    _set_last_error(self._api)
                    if not self._api.UnregisterHotKey(None, _HOTKEY_ID):
                        cleanup_error = _format_win_error(self._api, "UnregisterHotKey")
                        self.error = (
                            f"{self.error}; {cleanup_error}" if self.error else cleanup_error
                        )
                except BaseException as exc:
                    cleanup_error = f"UnregisterHotKey failed: {exc}"
                    self.error = (
                        f"{self.error}; {cleanup_error}" if self.error else cleanup_error
                    )
                finally:
                    self._registered = False
            self._ready.set()
            if registered and unexpected_exit and not self._stop.is_set():
                try:
                    self.callback()
                except BaseException as exc:
                    suffix = f"Cancellation callback failed: {exc}"
                    self.error = f"{self.error}; {suffix}" if self.error else suffix

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            self.available = False
            return
        if thread.is_alive() and self._thread_id:
            try:
                posted = self._api.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
                if not posted and not self.error:
                    self.error = _format_win_error(self._api, "PostThreadMessageW(WM_QUIT)")
            except BaseException as exc:
                if not self.error:
                    self.error = f"PostThreadMessageW(WM_QUIT) failed: {exc}"
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if thread.is_alive():
            self.available = False
            if not self.error:
                self.error = "Global hotkey message loop did not stop within 2 seconds"


class SingleInstance:
    """Session-local named mutex handle used to reject a second application instance."""

    _MUTEX_NAME = "Local\\AutoReady.SingleInstance.v1"

    def __init__(self, *, _api=None) -> None:
        self._api = _load_api() if _api is None else _api
        self._handle = 0

    def acquire(self) -> bool:
        if self._handle:
            return True
        _set_last_error(self._api)
        handle = self._api.CreateMutexW(None, False, self._MUTEX_NAME)
        value = _handle_value(handle)
        if not value:
            raise _api_failure(self._api, "CreateMutexW")
        error = _last_error(self._api)
        if error == 183:  # ERROR_ALREADY_EXISTS
            self._api.CloseHandle(_handle(value))
            return False
        self._handle = value
        return True

    def close(self) -> None:
        handle, self._handle = self._handle, 0
        if handle and not self._api.CloseHandle(_handle(handle)):
            raise _api_failure(self._api, "CloseHandle(mutex)")
