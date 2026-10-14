"""Single-round state machine and the only authorization path to real input.

One worker calls tick. The lock is also a serialization boundary for tests invoking
concurrent callbacks. Stop signals the session Event before waiting for that lock.
The unavoidable final OS check/input race is documented, not claimed away.
"""

import logging
import math
import threading
import time
import uuid
from dataclasses import asdict

from .models import (
    Candidate,
    CaptureStatus,
    Detection,
    Frame,
    Geometry,
    Identity,
    Mode,
    Region,
    Settings,
    Snapshot,
    State,
    Template,
)
from .recognition import match, validate_template

LOG = logging.getLogger(__name__)
TERMINAL = {State.DISARMED, State.SUCCESS, State.UNCONFIRMED, State.ERROR, State.REMINDED}


class _CaptureGenerationChanged(ValueError):
    """A capture resource was replaced during a round; old evidence is unusable."""


class _InputCancellation:
    def __init__(self, stopped, paused):
        self.stopped, self.paused = stopped, paused

    def is_set(self):
        return self.stopped.is_set() or self.paused.is_set()


class Engine:
    def __init__(self, backend, input_adapter, notifier, clock=time.monotonic):
        self.backend, self.input_adapter, self.notifier = backend, input_adapter, notifier
        self.clock = clock
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._cancel.set()
        self._pause_requested = threading.Event()
        self.identity: Identity | None = None
        self.button: Template | None = None
        self.success: Template | None = None
        self.settings = Settings()
        self.region = Region()
        self.mode = Mode.REMIND
        self._hotkey = False
        self._hotkey_probe = None
        self._closed = False
        self._state = State.DISARMED
        self._reason = "请选择目标并采集模板；默认仅提醒"
        self._session = ""
        self._attempted = False
        self._notified = False
        self._manual_pause = False
        self._started = 0.0
        self._ended_at: float | None = None
        self._attempted_at = 0.0
        self._input_completed_at: float | None = None
        self._last_sequence = -1
        self._last_time = -1.0
        self._capture_generation: int | None = None
        self._capture_backend: str | None = None
        self._last_source_sequence: int | None = None
        self._last_source_time_100ns: int | None = None
        self._capture_status: CaptureStatus | None = None
        self._foreground: bool | None = None
        self._capture_method_applied: str | None = None
        self._activation_allowed = False
        self._activation_attempted = False
        self._awaiting_foreground = False
        self._foreground_barrier: float | None = None
        self._geometry: Geometry | None = None
        self._stable_since = 0.0
        self._candidate: Candidate | None = None
        self._count = 0
        self._success_count = 0
        self._success_candidate: Candidate | None = None
        self._frame: Frame | None = None
        self._detection: Detection | None = None
        self.history: list[tuple[str, State, str]] = []
        self._observer = None

    def set_observer(self, observer):
        """Attach optional structured evidence; it never grants input authorization."""
        with self._lock:
            self._observer = observer

    def _emit(self, event, **fields):
        if self._observer is not None:
            try:
                self._observer(event, session_id=self._session, state=self._state.value,
                               attempted=self._attempted, **fields)
            except Exception:
                LOG.exception("验收记录失败；该记录不能作为通过证据")

    def _record_success_detection(self, stage, frame, detection):
        self._emit("success_detection", stage=stage, candidates=len(detection.candidates),
                   score=max((c.score for c in detection.candidates), default=None),
                   reason=detection.reason, frame_sequence=frame.sequence,
                   captured_at=frame.captured_at, geometry=asdict(frame.geometry))

    def _transition(self, state, reason):
        if state in TERMINAL and self._session and self._ended_at is None:
            self._ended_at = self.clock()
        if (self._state, self._reason) != (state, reason):
            self._state, self._reason = state, reason
            self.history.append((self._session, state, reason))
            self.history = self.history[-100:]
            LOG.info("session=%s state=%s reason=%s", self._session, state, reason)
            self._emit("state", reason=reason)

    def _reset_confirmation(self):
        self._candidate, self._count = None, 0
        self._success_count = 0
        self._success_candidate = None

    def configure(
        self, identity, button, success=None, region=None, settings=None, mode=Mode.REMIND
    ):
        self.stop("配置已更改，请重新启用本轮")
        with self._lock:
            # Failed replacement must not leave an older valid click meaning usable.
            self.identity = self.button = self.success = None
            settings = settings or Settings()
            region = region or Region()
            settings.validate()
            region.validate()
            if button is not None:
                validate_template(button.image)
            if success is not None:
                validate_template(success.image)
            self.identity, self.button, self.success = identity, button, success
            self.region, self.settings, self.mode = region, settings, Mode(mode)
            self._frame = self._detection = None
            self._capture_status = None
            self._foreground = None
            self._activation_allowed = False
            self._activation_attempted = False
            self._awaiting_foreground = False
            self._foreground_barrier = None
            self._capture_method_applied = None

    def set_hotkey_available(self, available):
        # Signal before taking the state lock so a worker blocked in native code
        # can observe loss of its emergency stop before it reaches input.
        if not available and self.mode == Mode.AUTO:
            self._cancel.set()
        with self._lock:
            self._hotkey = bool(available)
            if not available and self.mode == Mode.AUTO and self._state not in TERMINAL:
                self._finish(State.ERROR, "全局急停不可用，自动确认已禁用")

    def set_hotkey_probe(self, probe):
        """Optional live native readiness check, evaluated again at the input boundary."""
        with self._lock:
            self._hotkey_probe = probe

    def _hotkey_ready(self):
        try:
            return self._hotkey and (self._hotkey_probe is None or bool(self._hotkey_probe()))
        except Exception:
            return False

    def start(self, *, allow_activation=False):
        if not isinstance(allow_activation, bool):
            raise ValueError("切回授权必须为明确的布尔值")
        self.stop("重新启用")
        with self._lock:
            if self._closed:
                raise ValueError("应用已退出")
            if self.identity is None or self.button is None:
                raise ValueError("请先选择目标窗口并采集有效按钮模板")
            if self.mode == Mode.AUTO and not self._hotkey_ready():
                raise ValueError("全局急停注册失败，不能启用自动确认")
            self.settings.validate()
            geometry = self.backend.inspect(self.identity)
            if not self.button.compatible(geometry) or (
                self.success is not None and not self.success.compatible(geometry)
            ):
                raise ValueError("窗口尺寸或 DPI 已改变，请重新采集模板")
            self._session = uuid.uuid4().hex
            self._cancel = threading.Event()
            self._pause_requested.clear()
            self._started = self.clock()
            self._ended_at = None
            self._attempted = self._notified = self._manual_pause = False
            self._input_completed_at = None
            self._activation_allowed = bool(allow_activation and self.mode == Mode.AUTO)
            self._activation_attempted = False
            self._awaiting_foreground = False
            self._foreground_barrier = None
            self._last_sequence, self._last_time = -1, -1.0
            self._capture_generation = None
            self._capture_backend = None
            self._last_source_sequence = None
            self._last_source_time_100ns = None
            self._capture_status = None
            self._foreground = None
            self._capture_method_applied = None
            self._geometry, self._stable_since = geometry, self.clock()
            self._frame = self._detection = None
            self._reset_confirmation()
            self._transition(State.MONITORING, "本轮已启用；等待目标窗口画面")
            self._emit("round_started", identity=asdict(self.identity), geometry=asdict(geometry),
                       mode=self.mode.value, success_template=self.success is not None)
            return self._session

    def stop(self, reason="已停止"):
        self._cancel.set()
        with self._lock:
            self._reset_confirmation()
            self._manual_pause = False
            self._pause_requested.clear()
            self._activation_allowed = False
            self._awaiting_foreground = False
            self._transition(State.DISARMED, reason)

    def pause(self):
        self._pause_requested.set()
        with self._lock:
            if self._state not in TERMINAL:
                self._manual_pause = True
                self._activation_allowed = False
                self._reset_confirmation()
                self._transition(State.PAUSED, "手动暂停；只可手动恢复")
            else:
                self._pause_requested.clear()

    def resume(self):
        with self._lock:
            if self._manual_pause and not self._cancel.is_set():
                self._manual_pause = False
                self._pause_requested.clear()
                self._geometry = None
                self._reset_confirmation()
                self._reset_frame_baseline()
                self._foreground_barrier = self.clock()
                self._transition(
                    State.VERIFYING if self._attempted else (
                        State.WAITING_FOREGROUND if self._awaiting_foreground
                        else State.MONITORING
                    ),
                    "已恢复，重新采集并确认",
                )

    def close(self):
        self.stop("应用已退出")
        with self._lock:
            self._closed = True
            self._emit("closed", reason=self._reason)

    def snapshot(self):
        with self._lock:
            elapsed = 0
            if self._session:
                ended_at = self._ended_at if self._ended_at is not None else self.clock()
                elapsed = max(0, ended_at - self._started)
            return Snapshot(
                self._state,
                self._reason,
                self._session,
                self._attempted,
                self._count,
                self._detection,
                self._frame,
                elapsed,
                self._capture_status,
                self._foreground,
                self._activation_allowed,
                self._activation_attempted,
                self._manual_pause or self._pause_requested.is_set(),
            )

    def _notify(self):
        if not self._notified:
            self._notified = True
            try:
                self.notifier()
            except Exception:
                LOG.exception("提示音失败")

    def _finish(self, state, reason):
        self._cancel.set()
        self._manual_pause = False
        self._pause_requested.clear()
        self._activation_allowed = False
        self._transition(state, reason)
        self._notify()

    def _block(self, reason):
        self._reset_confirmation()
        self._geometry = None
        self._transition(State.PAUSED, reason)

    def _reset_frame_baseline(self):
        self._last_sequence = -1
        self._last_time = -1.0
        self._capture_generation = None
        self._capture_backend = None
        self._last_source_sequence = None
        self._last_source_time_100ns = None
        self._frame = None
        self._detection = None

    def _continue_round(self, session):
        """Recheck cancellation and the fixed session deadline after backend work."""
        if session != self._session or self._cancel.is_set():
            return False
        if self.clock() - self._started >= self.settings.round_timeout:
            self._finish(State.UNCONFIRMED, "本轮超时，已结束；请手动启用下一轮")
            return False
        return not self._manual_pause and not self._pause_requested.is_set()

    def _pause_for_missing_wgc_frame(self, session, error):
        """Keep a live WGC session armed when its source has not produced a new frame."""
        if not self._continue_round(session):
            return True
        if not callable(getattr(self.backend, "capture_status", None)):
            return False
        try:
            status = self._read_capture_status()
        except Exception as status_error:
            if self._continue_round(session):
                LOG.exception("WGC 取帧失败后无法读取捕获状态")
                self._finish(State.ERROR, f"无法确认捕获状态：{status_error}")
            return True
        if not self._continue_round(session):
            return True
        if (
            self._canonical_backend(getattr(status, "backend", "")) != "wgc"
            or getattr(status, "supported", None) is not True
            or getattr(status, "valid_frame", True) is not False
            or getattr(status, "fresh", True) is not False
        ):
            return False
        generation = getattr(status, "generation", None)
        if self._capture_generation is not None and generation != self._capture_generation:
            self._finish(State.ERROR, "捕获会话已重建，请重新启用本轮")
            return True
        if getattr(status, "minimized", False):
            self._reset_confirmation()
            self._frame = self._detection = None
            self._transition(State.PAUSED, "目标窗口已最小化，暂停识别")
            return True
        try:
            geometry = self.backend.inspect(self.identity)
        except Exception as inspect_error:
            if self._continue_round(session):
                LOG.exception("目标窗口失效 after WGC frame miss")
                self._finish(State.ERROR, f"目标窗口或身份不可用：{inspect_error}")
            return True
        if not self._continue_round(session):
            return True
        if not self.button.compatible(geometry) or (
            self.success is not None and not self.success.compatible(geometry)
        ):
            self._finish(State.ERROR, "窗口缩放或 DPI 改变，请重新采集模板并启用")
            return True
        if geometry != self._geometry:
            self._geometry, self._stable_since = geometry, self.clock()
            self._reset_confirmation()
            self._frame = self._detection = None
            self._transition(State.PAUSED, "窗口移动后等待几何稳定，再重新识别")
            return True
        self._reset_confirmation()
        self._frame = self._detection = None
        self._transition(State.PAUSED, getattr(status, "reason", "") or str(error))
        return True

    def _wait_for_manual_foreground_after_gdi_failure(self, session, error):
        """Recover only when GDI explicitly reports that the target is not foreground."""
        if not self._continue_round(session):
            return True
        if not callable(getattr(self.backend, "capture_status", None)):
            return False
        try:
            status = self._read_capture_status()
        except Exception as status_error:
            if self._continue_round(session):
                LOG.exception("GDI 前台捕获失败后无法读取捕获状态")
                self._finish(State.ERROR, f"无法确认捕获状态：{status_error}")
            return True
        if not self._continue_round(session):
            return True
        error_reason = str(error).casefold()
        foreground_failure = any(
            marker in error_reason
            for marker in (
                "gdi 前台兼容捕获要求目标窗口位于前台",
                "目标离开前台",
                "gdi capture requires the target window to be foreground",
                "target left foreground",
            )
        )
        if (
            self._canonical_backend(getattr(status, "backend", "")) != "gdi"
            or getattr(status, "valid_frame", True) is not False
            or getattr(status, "fresh", True) is not False
            or getattr(status, "foreground", None) is not False
            or getattr(status, "minimized", False)
            or not foreground_failure
        ):
            return False
        generation = getattr(status, "generation", None)
        if self._capture_generation is not None and generation != self._capture_generation:
            return False
        try:
            geometry = self.backend.inspect(self.identity)
        except Exception as inspect_error:
            if self._continue_round(session):
                LOG.exception("GDI 前台捕获失败后目标身份或窗口不可用")
                self._finish(State.ERROR, f"目标窗口或身份不可用：{inspect_error}")
            return True
        if not self._continue_round(session):
            return True
        if not self.button.compatible(geometry) or (
            self.success is not None and not self.success.compatible(geometry)
        ):
            self._finish(State.ERROR, "窗口缩放或 DPI 改变，请重新采集模板并启用")
            return True
        if geometry != self._geometry:
            self._geometry, self._stable_since = geometry, self.clock()
        self._foreground = False
        self._awaiting_foreground = True
        self._reset_confirmation()
        self._frame = self._detection = None
        self._transition(State.WAITING_FOREGROUND, "兼容捕获要求你手动切回目标窗口")
        return True

    def _apply_capture_method(self, session):
        method = self.settings.capture_method
        if method == self._capture_method_applied:
            return True
        setter = getattr(self.backend, "set_capture_method", None)
        if callable(setter):
            result = setter(method)
            if isinstance(result, CaptureStatus):
                self._capture_status = result
        if not self._continue_round(session):
            return False
        self._capture_method_applied = method
        return True

    def _read_capture_status(self):
        reader = getattr(self.backend, "capture_status", None)
        status = reader() if callable(reader) else None
        self._capture_status = status
        return status

    @staticmethod
    def _canonical_backend(value):
        if not isinstance(value, str):
            return ""
        name = value.casefold().replace("-", "").replace("_", "").replace(" ", "")
        if "wgc" in name or "graphicscapture" in name:
            return "wgc"
        if "gdi" in name or "bitblt" in name:
            return "gdi"
        return name

    def _validate_capture_status(self, frame, status):
        if status is None:
            return
        generation = getattr(status, "generation", None)
        if generation is not None and generation != frame.capture_generation:
            raise ValueError("捕获状态与截图代次不一致，丢弃截图")
        if getattr(status, "minimized", False):
            raise ValueError("目标窗口已最小化，暂停识别")
        status_backend = self._canonical_backend(getattr(status, "backend", ""))
        frame_backend = self._canonical_backend(frame.backend)
        if status_backend in {"wgc", "gdi"} and frame_backend in {"wgc", "gdi"}:
            if status_backend != frame_backend:
                raise ValueError("捕获后端与当前捕获状态不一致，丢弃截图")
        if frame_backend == "wgc":
            if getattr(status, "valid_frame", True) is False:
                raise ValueError("后台捕获尚未取得有效目标画面")
            if getattr(status, "fresh", True) is False:
                raise ValueError("后台捕获尚未收到新源帧")

    def _validate_frame(self, frame, session, capture_status=None):
        now = self.clock()
        if not isinstance(frame, Frame):
            raise ValueError("捕获后端没有返回有效截图")
        if frame.session_id != session or frame.identity != self.identity:
            raise ValueError("旧会话或窗口身份不一致，丢弃截图")
        if (
            not 0 <= now - frame.captured_at <= self.settings.max_frame_age
            or frame.sequence <= self._last_sequence
            or frame.captured_at <= self._last_time
        ):
            raise ValueError("截图过期或重复，丢弃截图")
        generation = frame.capture_generation
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("捕获会话代次无效，丢弃截图")
        backend_name = self._canonical_backend(frame.backend)
        if backend_name not in {"wgc", "gdi"}:
            raise ValueError("未知捕获后端，丢弃截图")
        if self._capture_generation is not None and generation != self._capture_generation:
            raise _CaptureGenerationChanged("捕获会话已重建，请重新启用本轮")
        if self._capture_backend is not None and backend_name != self._capture_backend:
            raise _CaptureGenerationChanged("捕获后端已切换，请重新启用本轮")
        source_sequence = frame.source_sequence
        source_time = frame.source_time_100ns
        received_at = frame.received_at
        if backend_name == "wgc":
            if (
                isinstance(source_sequence, bool)
                or not isinstance(source_sequence, int)
                or source_sequence < 0
                or isinstance(source_time, bool)
                or not isinstance(source_time, int)
                or source_time < 0
            ):
                raise ValueError("后台源帧序号或时间无效，丢弃截图")
            if (
                isinstance(received_at, bool)
                or not isinstance(received_at, (int, float))
                or not math.isfinite(received_at)
                or not 0 <= now - received_at <= self.settings.max_frame_age
                or not 0 <= received_at - frame.captured_at <= self.settings.max_frame_age
            ):
                raise ValueError("后台源帧接收时间无效或过期，丢弃截图")
            if (
                self._last_source_sequence is not None
                and source_sequence <= self._last_source_sequence
            ) or (
                self._last_source_time_100ns is not None
                and source_time <= self._last_source_time_100ns
            ):
                raise ValueError("后台源帧重复或时间倒退，丢弃截图")
        self._validate_capture_status(frame, capture_status)
        if self._capture_generation is None:
            self._capture_generation = generation
            self._capture_backend = backend_name
        self._last_sequence, self._last_time = frame.sequence, frame.captured_at
        if backend_name == "wgc":
            self._last_source_sequence = source_sequence
            self._last_source_time_100ns = source_time

    def _input_conflict(self):
        probe = getattr(self.backend, "input_conflict", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            LOG.exception("无法确认物理输入状态；拒绝自动动作")
            return True

    def _begin_foreground_reconfirm(self, geometry):
        self._awaiting_foreground = False
        self._foreground_barrier = self.clock()
        self._geometry, self._stable_since = geometry, self._foreground_barrier
        self._reset_confirmation()
        self._frame = self._detection = None
        self._transition(
            State.VERIFYING if self._attempted else State.MONITORING,
            "目标已切回；等待稳定并重新确认新画面",
        )

    def _recognition_scene(self, geometry, frame, foreground, candidate=None, session=None):
        if session is not None and not self._continue_round(session):
            return ""
        if self.backend.inspect(self.identity) != geometry:
            return "窗口位置、尺寸或 DPI 已变化"
        if session is not None and not self._continue_round(session):
            return ""
        if self.backend.moving(self.identity):
            return "正在移动窗口，等待稳定"
        if session is not None and not self._continue_round(session):
            return ""
        if candidate is not None:
            if (
                not candidate.rect.inside(geometry.width, geometry.height)
                or not candidate.rect.contains(candidate.rect.center)
            ):
                return "点击点不在有效按钮范围内"
            if self._canonical_backend(frame.backend) != "wgc":
                if not foreground:
                    return "当前兼容捕获要求目标位于前台"
                if not self.backend.unobscured(self.identity, candidate.rect, geometry):
                    return "按钮区域被遮挡或无法确认可见"
                if session is not None and not self._continue_round(session):
                    return ""
        return ""

    def _same_candidate(self, first, second):
        if first is None or second is None:
            return False
        ax, ay = first.rect.center
        bx, by = second.rect.center
        return (
            abs(ax - bx) <= self.settings.position_tolerance
            and abs(ay - by) <= self.settings.position_tolerance
            and first.rect.width == second.rect.width
            and first.rect.height == second.rect.height
        )

    def _scene(self, geometry, candidate=None, session=None):
        if session is not None and not self._continue_round(session):
            return ""
        if self.backend.inspect(self.identity) != geometry:
            return "窗口位置、尺寸或 DPI 已变化"
        if session is not None and not self._continue_round(session):
            return ""
        foreground = bool(self.backend.foreground(self.identity))
        self._foreground = foreground
        if session is not None and not self._continue_round(session):
            return ""
        if not foreground:
            return "目标不在前台，请手动切换到目标"
        if self.backend.moving(self.identity):
            return "正在移动窗口，等待稳定"
        if session is not None and not self._continue_round(session):
            return ""
        if candidate is not None:
            if not candidate.rect.inside(
                geometry.width, geometry.height
            ) or not candidate.rect.contains(candidate.rect.center):
                return "点击点不在有效按钮范围内"
            if not self.backend.unobscured(self.identity, candidate.rect, geometry):
                return "按钮区域被遮挡或无法确认可见"
            if session is not None and not self._continue_round(session):
                return ""
        return ""

    def _execute(self, session, frame, candidate):
        """Only method allowed to call input_adapter.click; caller holds _lock."""
        if (
            session != self._session
            or self._cancel.is_set()
            or self._attempted
            or self._manual_pause
            or self._pause_requested.is_set()
            or self.mode != Mode.AUTO
            or not self._hotkey_ready()
        ):
            return
        if not self._continue_round(session):
            return
        reason = self._scene(frame.geometry, candidate, session)
        if not self._continue_round(session):
            return
        if reason:
            self._block(reason)
            return
        conflict = self._input_conflict()
        if not self._continue_round(session):
            return
        if conflict:
            self._block("检测到正在按键或拖动；松开后再确认")
            return
        fresh = self.backend.capture(self.identity, session)
        if not self._continue_round(session):
            return
        status = self._read_capture_status()
        if not self._continue_round(session):
            return
        self._validate_frame(fresh, session, status)
        self._frame = fresh
        if fresh.geometry != frame.geometry:
            self._block("最终复核时窗口发生移动；重新识别")
            return
        detection = match(fresh, self.button, self.region, self.settings)
        if not self._continue_round(session):
            return
        self._detection = detection
        if not self._same_candidate(candidate, detection.unique):
            self._block("最终新截图未确认同一唯一按钮")
            return
        if self.success is not None:
            success_detection = match(fresh, self.success, Region(), self.settings)
            if not self._continue_round(session):
                return
            self._record_success_detection("before_input", fresh, success_detection)
            if not self._continue_round(session):
                return
            if success_detection.candidates:
                self._block("点击前已出现成功模板，不能证明本轮结果；请检查模板")
                return
        reason = self._scene(fresh.geometry, detection.unique, session)
        if not self._continue_round(session):
            return
        if reason:
            self._block(reason)
            return
        conflict = self._input_conflict()
        if not self._continue_round(session):
            return
        if conflict:
            self._block("检测到正在按键或拖动；松开后再确认")
            return
        if (
            self._cancel.is_set()
            or self._manual_pause
            or self._pause_requested.is_set()
            or not self._hotkey_ready()
            or session != self._session
            or self._attempted
            or self.clock() - self._started >= self.settings.round_timeout
            or self.clock() - fresh.captured_at > self.settings.max_frame_age
        ):
            return
        point = fresh.geometry.screen_point(detection.unique.rect.center)
        # Atomic consumption precedes any OS input. Exceptions cannot re-arm it.
        self._attempted = True
        self._attempted_at = self.clock()
        self._transition(State.ATTEMPTED, "已消耗本轮点击资格，正在发送一次点击")
        if not self._continue_round(session):
            return
        self._emit("input_attempt", point=list(point), geometry=asdict(fresh.geometry),
                   frame_sequence=fresh.sequence, captured_at=fresh.captured_at)
        try:
            # Evidence I/O must not make an otherwise fresh authorization stale.
            if not self._continue_round(session):
                return
            conflict = self._input_conflict()
            if not self._continue_round(session):
                return
            if conflict:
                raise ValueError("检测到正在按键或拖动，未发送系统输入")
            if (self._cancel.is_set() or self._pause_requested.is_set()
                    or not self._hotkey_ready()
                    or self.clock() - fresh.captured_at > self.settings.max_frame_age
                    or self.clock() - self._started >= self.settings.round_timeout
                    or self._scene(fresh.geometry, detection.unique, session)):
                raise ValueError("输入前最后复核失效，未发送系统输入")
            self.input_adapter.click(point, _InputCancellation(self._cancel, self._pause_requested))
        except Exception as exc:
            if (
                session != self._session
                or self._cancel.is_set()
                or self._manual_pause
                or self._pause_requested.is_set()
            ):
                return
            self._emit("input_result", ok=False, error=str(exc))
            if not self._continue_round(session):
                return
            LOG.exception("单次输入失败，不重试")
            self._finish(State.UNCONFIRMED, f"已尝试点击，结果未确认：{exc}")
            return
        self._input_completed_at = self.clock()
        if (
            session != self._session
            or self._cancel.is_set()
            or self._manual_pause
            or self._pause_requested.is_set()
        ):
            return
        self._emit("input_result", ok=True, error="")
        if (
            session != self._session
            or self._cancel.is_set()
            or self._manual_pause
            or self._pause_requested.is_set()
        ):
            return
        self._notify()
        if session != self._session or self._cancel.is_set() or self._pause_requested.is_set():
            return
        if self.success is None:
            self._finish(State.UNCONFIRMED, "已尝试点击，结果未确认：未配置成功模板")
        else:
            self._transition(State.VERIFYING, "已发送点击；等待明确成功证据")

    def _background_ready(self, session, geometry):
        self._awaiting_foreground = True
        self._reset_confirmation()
        self._detection = None  # Never retain a background coordinate for later input.
        self._transition(State.WAITING_FOREGROUND, "已发现按钮，等待你切回游戏")
        if not self._continue_round(session):
            return
        self._notify()
        if not self._continue_round(session):
            return
        if self._activation_allowed and not self._activation_attempted:
            self._attempt_foreground_activation(session, geometry)

    def _attempt_foreground_activation(self, session, geometry):
        if (
            not self._activation_allowed
            or self._activation_attempted
            or session != self._session
            or self.mode != Mode.AUTO
        ):
            return
        if not self._continue_round(session):
            return
        if not self._hotkey_ready():
            self._finish(State.ERROR, "全局急停已失效，本轮已取消")
            return
        conflict = self._input_conflict()
        if not self._continue_round(session):
            return
        if conflict:
            self._awaiting_foreground = False
            self._transition(State.WAITING_FOREGROUND, "检测到正在按键或拖动；松开后再尝试")
            return
        moving = self.backend.moving(self.identity)
        if not self._continue_round(session):
            return
        if moving:
            self._awaiting_foreground = False
            self._transition(State.WAITING_FOREGROUND, "窗口正在移动；稳定后再尝试切回")
            return
        request = getattr(self.backend, "request_foreground", None)
        if not callable(request):
            self._transition(State.WAITING_FOREGROUND, "当前后端不能主动切回；请手动切回游戏")
            return
        current_geometry = self.backend.inspect(self.identity)
        if not self._continue_round(session):
            return
        if not self.button.compatible(current_geometry):
            self._finish(State.ERROR, "窗口缩放或 DPI 改变，请重新采集模板并启用")
            return
        if current_geometry != geometry:
            self._geometry, self._stable_since = current_geometry, self.clock()
            self._reset_confirmation()
            self._transition(State.PAUSED, "窗口移动后等待几何稳定，再重新识别")
            return
        foreground = bool(self.backend.foreground(self.identity))
        self._foreground = foreground
        if not self._continue_round(session):
            return
        if foreground:
            self._begin_foreground_reconfirm(current_geometry)
            return

        # The authorization and one-shot budget are consumed before native code.
        self._activation_attempted = True
        self._transition(State.ACTIVATING, "已授权切回；正在尝试一次前台切换")
        if not self._continue_round(session):
            return
        self._emit("activation_attempt", identity=asdict(self.identity),
                   geometry=asdict(current_geometry))
        if not self._continue_round(session):
            return
        api_result = None
        api_error = ""
        try:
            api_result = request(
                self.identity, _InputCancellation(self._cancel, self._pause_requested)
            )
        except Exception as exc:
            api_error = f"{type(exc).__name__}: {exc}"
            LOG.info("前台切换请求未完成：%s", api_error)
        if not self._continue_round(session):
            return

        # A successful API return is not evidence. Recheck target identity via
        # inspect and independently ask which window is actually foreground.
        actual_geometry = self.backend.inspect(self.identity)
        if not self._continue_round(session):
            return
        actual_foreground = bool(self.backend.foreground(self.identity))
        self._foreground = actual_foreground
        if not self._continue_round(session):
            return
        self._emit("activation_result", api_result=bool(api_result), api_error=api_error,
                   foreground=actual_foreground, geometry=asdict(actual_geometry))
        if not self._continue_round(session):
            return
        if not self.button.compatible(actual_geometry):
            self._finish(State.ERROR, "窗口缩放或 DPI 改变，请重新采集模板并启用")
            return
        if actual_foreground:
            self._begin_foreground_reconfirm(actual_geometry)
        else:
            self._transition(State.WAITING_FOREGROUND, "系统未能切回；请手动切回游戏")

    def tick(self):
        with self._lock:
            if self._closed or self._cancel.is_set() or self._state in TERMINAL:
                return self.snapshot()
            if self.mode == Mode.AUTO and not self._hotkey_ready():
                self._finish(State.ERROR, "全局急停已失效，本轮已取消")
                return self.snapshot()
            if self._attempted and self.clock() - self._attempted_at >= (
                self.settings.verification_timeout
            ):
                self._finish(State.UNCONFIRMED, "已尝试点击，结果未确认：验证超时")
                return self.snapshot()
            if self.clock() - self._started >= self.settings.round_timeout:
                self._finish(State.UNCONFIRMED, "本轮超时，已结束；请手动启用下一轮")
                return self.snapshot()
            if self._manual_pause:
                return self.snapshot()
            session = self._session
            try:
                if not self._apply_capture_method(session):
                    return self.snapshot()
                geometry = self.backend.inspect(self.identity)
                if not self._continue_round(session):
                    return self.snapshot()
                if not self.button.compatible(geometry) or (
                    self.success is not None and not self.success.compatible(geometry)
                ):
                    self._finish(State.ERROR, "窗口缩放或 DPI 改变，请重新采集模板并启用")
                    return self.snapshot()
                if geometry != self._geometry:
                    self._geometry, self._stable_since = geometry, self.clock()
                    self._reset_confirmation()
                    self._transition(State.PAUSED, "窗口移动后等待几何稳定，再重新识别")
                    return self.snapshot()

                status = self._read_capture_status()
                if not self._continue_round(session):
                    return self.snapshot()
                if getattr(status, "minimized", False):
                    self._block("目标窗口已最小化，暂停识别")
                    return self.snapshot()

                previous_foreground = self._foreground
                foreground = bool(self.backend.foreground(self.identity))
                self._foreground = foreground
                if not self._continue_round(session):
                    return self.snapshot()
                if foreground and (self._awaiting_foreground or previous_foreground is False):
                    self._begin_foreground_reconfirm(geometry)
                    return self.snapshot()
                if self.backend.moving(self.identity):
                    if not self._continue_round(session):
                        return self.snapshot()
                    self._block("正在移动窗口，等待稳定")
                    return self.snapshot()
                if not self._continue_round(session):
                    return self.snapshot()
                if self.clock() - self._stable_since < self.settings.stable_seconds:
                    state = State.WAITING_FOREGROUND if self._awaiting_foreground else (
                        State.BACKGROUND if not foreground else State.PAUSED
                    )
                    reason = "等待目标窗口位置稳定" if state == State.PAUSED else (
                        "后台等待新画面" if state == State.BACKGROUND else "等待你切回游戏"
                    )
                    self._transition(state, reason)
                    return self.snapshot()

                status_backend = self._canonical_backend(getattr(status, "backend", ""))
                if (
                    not foreground
                    and (self.settings.capture_method == "gdi" or status_backend == "gdi")
                ):
                    if self._awaiting_foreground:
                        self._transition(
                            State.WAITING_FOREGROUND,
                            "目标不在前台；当前兼容捕获要求你切回游戏",
                        )
                    else:
                        self._block("目标不在前台；当前使用前台兼容捕获，请切回目标窗口")
                    return self.snapshot()

                frame = self.backend.capture(self.identity, session)
                if not self._continue_round(session):
                    return self.snapshot()
                status = self._read_capture_status()
                if not self._continue_round(session):
                    return self.snapshot()
                self._validate_frame(frame, session, status)
                self._frame = frame
                if frame.geometry != geometry:
                    self._block("截图期间窗口移动，丢弃该帧")
                    return self.snapshot()
                if getattr(status, "minimized", False):
                    self._block("目标窗口已最小化，暂停识别")
                    return self.snapshot()

                previous_foreground = foreground
                foreground = bool(self.backend.foreground(self.identity))
                self._foreground = foreground
                if not self._continue_round(session):
                    return self.snapshot()
                if foreground and (self._awaiting_foreground or previous_foreground is False):
                    self._begin_foreground_reconfirm(geometry)
                    return self.snapshot()
                if self._awaiting_foreground:
                    self._transition(State.WAITING_FOREGROUND, "已发现按钮，等待你切回游戏")
                    return self.snapshot()

                if not foreground and self._canonical_backend(frame.backend) != "wgc":
                    self._block("目标不在前台；当前使用前台兼容捕获，请切回目标窗口")
                    return self.snapshot()
                reason = self._recognition_scene(geometry, frame, foreground, session=session)
                if not self._continue_round(session):
                    return self.snapshot()
                if reason:
                    self._block(reason)
                    return self.snapshot()
                if self._foreground_barrier is not None:
                    if frame.captured_at <= self._foreground_barrier:
                        state = State.VERIFYING if self._attempted else State.MONITORING
                        self._transition(state, "等待切回后取得新的目标画面")
                        return self.snapshot()
                    self._foreground_barrier = None

                if self._attempted:
                    detection = match(frame, self.success, Region(), self.settings)
                    if not self._continue_round(session):
                        return self.snapshot()
                    self._record_success_detection("after_input", frame, detection)
                    if not self._continue_round(session):
                        return self.snapshot()
                    self._detection = detection
                    reason = self._recognition_scene(
                        geometry, frame, foreground, detection.unique, session=session
                    )
                    if not self._continue_round(session):
                        return self.snapshot()
                    if (
                        detection.unique
                        and not reason
                        and self._input_completed_at is not None
                        and frame.captured_at > self._input_completed_at
                        and self.clock() - frame.captured_at <= self.settings.max_frame_age
                        and self.clock() - self._attempted_at < self.settings.verification_timeout
                    ):
                        self._success_count = (
                            self._success_count + 1
                            if self._same_candidate(self._success_candidate, detection.unique)
                            else 1
                        )
                        self._success_candidate = detection.unique
                    else:
                        self._success_count = 0
                        self._success_candidate = None
                    if self._success_count >= 2:
                        self._finish(State.SUCCESS, "成功：已连续识别到配置的成功状态模板")
                    else:
                        self._transition(State.VERIFYING, "已发送点击；尚未确认成功状态")
                    return self.snapshot()

                detection = match(frame, self.button, self.region, self.settings)
                if not self._continue_round(session):
                    return self.snapshot()
                self._detection = detection
                candidate = detection.unique
                if candidate is None:
                    self._reset_confirmation()
                    self._transition(
                        State.BACKGROUND if not foreground else State.MONITORING,
                        detection.reason,
                    )
                    return self.snapshot()
                reason = self._recognition_scene(
                    geometry, frame, foreground, candidate, session=session
                )
                if not self._continue_round(session):
                    return self.snapshot()
                if reason:
                    self._block(reason)
                    return self.snapshot()
                self._count = (
                    self._count + 1 if self._same_candidate(self._candidate, candidate) else 1
                )
                self._candidate = candidate
                is_background = not foreground and self._canonical_backend(frame.backend) == "wgc"
                self._transition(
                    State.BACKGROUND if is_background else State.CONFIRMING,
                    f"{'后台' if is_background else '前台'}唯一候选："
                    f"{self._count}/{self.settings.consecutive_frames} 新帧",
                )
                if not self._continue_round(session):
                    return self.snapshot()
                if self._count >= self.settings.consecutive_frames:
                    if self.mode == Mode.REMIND:
                        self._finish(State.REMINDED, "已识别按钮并提醒；仅提醒模式未发送输入")
                    elif is_background:
                        self._background_ready(session, geometry)
                    else:
                        self._execute(session, frame, candidate)
            except _CaptureGenerationChanged as exc:
                if session == self._session and not self._cancel.is_set():
                    self._finish(State.ERROR, str(exc))
            except ValueError as exc:
                if self._continue_round(session):
                    self._block(str(exc))
            except Exception as exc:
                if not self._wait_for_manual_foreground_after_gdi_failure(session, exc):
                    if self._pause_for_missing_wgc_frame(session, exc):
                        return self.snapshot()
                    if self._continue_round(session):
                        LOG.exception("监测失败，结束本轮")
                        self._finish(State.ERROR, f"监测停止：{exc}")
            return self.snapshot()

    def preview(self):
        self.stop("识别预览不会点击；请重新启用本轮")
        with self._lock:
            if self.identity is None or self.button is None:
                raise ValueError("请先选择窗口并采集模板")
            frame = self.backend.capture(self.identity, "preview-" + uuid.uuid4().hex)
            detection = match(frame, self.button, self.region, self.settings)
            reason = detection.reason or self._scene(frame.geometry, detection.unique)
            self._frame, self._detection = frame, detection
            self._transition(State.DISARMED, reason or "唯一候选；预览模式不会点击")
            return self.snapshot()
