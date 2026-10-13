"""Single-round state machine and the only authorization path to real input.

One worker calls tick. The lock is also a serialization boundary for tests invoking
concurrent callbacks. Stop signals the session Event before waiting for that lock.
The unavoidable final OS check/input race is documented, not claimed away.
"""

import logging
import threading
import time
import uuid
from dataclasses import asdict

from .models import (
    Candidate,
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
TERMINAL = {State.DISARMED, State.SUCCESS, State.UNCONFIRMED, State.ERROR}


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
        self._attempted_at = 0.0
        self._last_sequence = -1
        self._last_time = -1.0
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

    def set_hotkey_available(self, available):
        if not available:
            self._cancel.set()
        with self._lock:
            self._hotkey = bool(available)
            if not available and self.mode == Mode.AUTO and self._state not in TERMINAL:
                self._transition(State.ERROR, "全局急停不可用，自动确认已禁用")

    def set_hotkey_probe(self, probe):
        """Optional live native readiness check, evaluated again at the input boundary."""
        with self._lock:
            self._hotkey_probe = probe

    def _hotkey_ready(self):
        try:
            return self._hotkey and (self._hotkey_probe is None or bool(self._hotkey_probe()))
        except Exception:
            return False

    def start(self):
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
            self._attempted = self._notified = self._manual_pause = False
            self._last_sequence, self._last_time = -1, -1.0
            self._geometry, self._stable_since = geometry, self.clock()
            self._frame = self._detection = None
            self._reset_confirmation()
            self._transition(State.MONITORING, "本轮已启用；请手动将目标窗口置于前台")
            self._emit("round_started", identity=asdict(self.identity), geometry=asdict(geometry),
                       mode=self.mode.value, success_template=self.success is not None)
            return self._session

    def stop(self, reason="已停止"):
        self._cancel.set()
        with self._lock:
            self._reset_confirmation()
            self._manual_pause = False
            self._transition(State.DISARMED, reason)

    def pause(self):
        self._pause_requested.set()
        with self._lock:
            if self._state not in TERMINAL:
                self._manual_pause = True
                self._reset_confirmation()
                self._transition(State.PAUSED, "手动暂停；只可手动恢复")

    def resume(self):
        with self._lock:
            if self._manual_pause and not self._cancel.is_set():
                self._manual_pause = False
                self._pause_requested.clear()
                self._geometry = None
                self._reset_confirmation()
                self._transition(
                    State.VERIFYING if self._attempted else State.MONITORING,
                    "已恢复，重新采集并确认",
                )

    def close(self):
        self.stop("应用已退出")
        with self._lock:
            self._closed = True
            self._emit("closed", reason=self._reason)

    def snapshot(self):
        with self._lock:
            return Snapshot(
                self._state,
                self._reason,
                self._session,
                self._attempted,
                self._count,
                self._detection,
                self._frame,
                max(0, self.clock() - self._started) if self._session else 0,
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
        self._transition(state, reason)
        self._notify()

    def _block(self, reason):
        self._reset_confirmation()
        self._geometry = None
        self._transition(State.PAUSED, reason)

    def _validate_frame(self, frame, session):
        now = self.clock()
        if frame.session_id != session or frame.identity != self.identity:
            raise ValueError("旧会话或窗口身份不一致，丢弃截图")
        if (
            not 0 <= now - frame.captured_at <= self.settings.max_frame_age
            or frame.sequence <= self._last_sequence
            or frame.captured_at <= self._last_time
        ):
            raise ValueError("截图过期或重复，丢弃截图")
        self._last_sequence, self._last_time = frame.sequence, frame.captured_at

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

    def _scene(self, geometry, candidate=None):
        if self.backend.inspect(self.identity) != geometry:
            return "窗口位置、尺寸或 DPI 已变化"
        if not self.backend.foreground(self.identity):
            return "目标不在前台，请手动切换到目标"
        if self.backend.moving(self.identity):
            return "正在移动窗口，等待稳定"
        if candidate is not None:
            if not candidate.rect.inside(
                geometry.width, geometry.height
            ) or not candidate.rect.contains(candidate.rect.center):
                return "点击点不在有效按钮范围内"
            if not self.backend.unobscured(self.identity, candidate.rect, geometry):
                return "按钮区域被遮挡或无法确认可见"
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
        reason = self._scene(frame.geometry, candidate)
        if reason:
            self._block(reason)
            return
        fresh = self.backend.capture(self.identity, session)
        self._validate_frame(fresh, session)
        self._frame = fresh
        if fresh.geometry != frame.geometry:
            self._block("最终复核时窗口发生移动；重新识别")
            return
        detection = match(fresh, self.button, self.region, self.settings)
        self._detection = detection
        if not self._same_candidate(candidate, detection.unique):
            self._block("最终新截图未确认同一唯一按钮")
            return
        if self.success is not None:
            success_detection = match(fresh, self.success, Region(), self.settings)
            self._record_success_detection("before_input", fresh, success_detection)
            if success_detection.candidates:
                self._block("点击前已出现成功模板，不能证明本轮结果；请检查模板")
                return
        reason = self._scene(fresh.geometry, detection.unique)
        if reason:
            self._block(reason)
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
        self._emit("input_attempt", point=list(point), geometry=asdict(fresh.geometry),
                   frame_sequence=fresh.sequence, captured_at=fresh.captured_at)
        try:
            # Evidence I/O must not make an otherwise fresh authorization stale.
            if (self._cancel.is_set() or self._pause_requested.is_set()
                    or not self._hotkey_ready()
                    or self.clock() - fresh.captured_at > self.settings.max_frame_age
                    or self.clock() - self._started >= self.settings.round_timeout
                    or self._scene(fresh.geometry, detection.unique)):
                raise ValueError("输入前最后复核失效，未发送系统输入")
            self.input_adapter.click(point, _InputCancellation(self._cancel, self._pause_requested))
        except Exception as exc:
            self._emit("input_result", ok=False, error=str(exc))
            LOG.exception("单次输入失败，不重试")
            self._finish(State.UNCONFIRMED, f"已尝试点击，结果未确认：{exc}")
            return
        self._emit("input_result", ok=True, error="")
        self._notify()
        if self.success is None:
            self._finish(State.UNCONFIRMED, "已尝试点击，结果未确认：未配置成功模板")
        else:
            self._transition(State.VERIFYING, "已发送点击；等待明确成功证据")

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
                geometry = self.backend.inspect(self.identity)
                if not self.button.compatible(geometry):
                    self._finish(State.ERROR, "窗口缩放或 DPI 改变，请重新采集模板并启用")
                    return self.snapshot()
                reason = self._scene(geometry)
                if reason:
                    self._block(reason)
                    return self.snapshot()
                if geometry != self._geometry:
                    self._geometry, self._stable_since = geometry, self.clock()
                    self._reset_confirmation()
                    self._transition(State.PAUSED, "窗口移动后等待几何稳定，再重新识别")
                    return self.snapshot()
                if self.clock() - self._stable_since < self.settings.stable_seconds:
                    self._transition(State.PAUSED, "等待客户区位置稳定")
                    return self.snapshot()
                frame = self.backend.capture(self.identity, session)
                self._validate_frame(frame, session)
                self._frame = frame
                if frame.geometry != geometry:
                    self._block("截图期间窗口移动，丢弃该帧")
                    return self.snapshot()
                if self._cancel.is_set():
                    return self.snapshot()
                if self._attempted:
                    detection = match(frame, self.success, Region(), self.settings)
                    self._record_success_detection("after_input", frame, detection)
                    self._detection = detection
                    if (
                        detection.unique
                        and not self._scene(geometry, detection.unique)
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
                self._detection = detection
                candidate = detection.unique
                if candidate is None:
                    self._reset_confirmation()
                    self._transition(State.MONITORING, detection.reason)
                    return self.snapshot()
                reason = self._scene(geometry, candidate)
                if reason:
                    self._block(reason)
                    return self.snapshot()
                self._count = (
                    self._count + 1 if self._same_candidate(self._candidate, candidate) else 1
                )
                self._candidate = candidate
                self._transition(
                    State.CONFIRMING,
                    f"唯一候选：{self._count}/{self.settings.consecutive_frames} 新帧",
                )
                if self._count >= self.settings.consecutive_frames:
                    if self.mode == Mode.REMIND:
                        self._finish(State.UNCONFIRMED, "已识别按钮并提醒；仅提醒模式未发送输入")
                    else:
                        self._execute(session, frame, candidate)
            except ValueError as exc:
                self._block(str(exc))
            except Exception as exc:
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
