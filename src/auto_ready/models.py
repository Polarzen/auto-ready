"""Shared immutable data. All rectangles are physical client pixels unless named otherwise."""

import math
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def contains(self, point: tuple[int, int]) -> bool:
        x, y = point
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def inside(self, width: int, height: int) -> bool:
        return (
            self.width > 0
            and self.height > 0
            and self.x >= 0
            and self.y >= 0
            and self.x + self.width <= width
            and self.y + self.height <= height
        )


@dataclass(frozen=True)
class Region:
    x: float = 0.0
    y: float = 0.0
    width: float = 1.0
    height: float = 1.0

    def validate(self) -> None:
        if not all(math.isfinite(v) for v in (self.x, self.y, self.width, self.height)):
            raise ValueError("搜索区域必须为有限数值")
        if (
            self.x < 0
            or self.y < 0
            or self.width <= 0
            or self.height <= 0
            or self.x + self.width > 1.000001
            or self.y + self.height > 1.000001
        ):
            raise ValueError("搜索区域必须在客户区内")

    def pixels(self, width: int, height: int) -> Rect:
        self.validate()
        left, top = int(self.x * width), int(self.y * height)
        return Rect(
            left,
            top,
            min(width, round((self.x + self.width) * width)) - left,
            min(height, round((self.y + self.height) * height)) - top,
        )

    @classmethod
    def from_rect(cls, rect: Rect, width: int, height: int) -> "Region":
        if not rect.inside(width, height):
            raise ValueError("框选范围不在图像内")
        return cls(rect.x / width, rect.y / height, rect.width / width, rect.height / height)


@dataclass(frozen=True)
class Identity:
    hwnd: int
    pid: int
    process_started: int
    executable: str
    title: str = field(default="", compare=False)


@dataclass(frozen=True)
class Geometry:
    left: int
    top: int
    width: int
    height: int
    dpi: int

    @property
    def template_key(self) -> tuple[int, int, int]:
        return self.width, self.height, self.dpi

    def screen_point(self, point: tuple[int, int]) -> tuple[int, int]:
        if not Rect(0, 0, self.width, self.height).contains(point):
            raise ValueError("点击点不在客户区内")
        return self.left + point[0], self.top + point[1]

    def screen_rect(self, rect: Rect) -> Rect:
        if not rect.inside(self.width, self.height):
            raise ValueError("候选框不在客户区内")
        return Rect(self.left + rect.x, self.top + rect.y, rect.width, rect.height)


@dataclass(frozen=True)
class Frame:
    image: np.ndarray
    geometry: Geometry
    identity: Identity
    captured_at: float
    session_id: str
    sequence: int


@dataclass(frozen=True)
class Template:
    image: np.ndarray
    reference_size: tuple[int, int]
    dpi: int
    name: str = "按钮"

    def compatible(self, geometry: Geometry) -> bool:
        return (*self.reference_size, self.dpi) == geometry.template_key


@dataclass(frozen=True)
class Candidate:
    rect: Rect
    score: float


@dataclass(frozen=True)
class Detection:
    candidates: tuple[Candidate, ...] = ()
    best_score: float = 0.0
    reason: str = "无匹配"

    @property
    def unique(self) -> Candidate | None:
        return self.candidates[0] if len(self.candidates) == 1 and not self.reason else None


class Mode(StrEnum):
    REMIND = "仅提醒"
    AUTO = "自动确认"


class State(StrEnum):
    DISARMED = "未启用"
    MONITORING = "监测中"
    CONFIRMING = "候选确认"
    ATTEMPTED = "点击已尝试"
    VERIFYING = "结果验证"
    SUCCESS = "成功"
    UNCONFIRMED = "结果未确认"
    PAUSED = "暂停"
    ERROR = "错误"


@dataclass(frozen=True)
class Settings:
    threshold: float = 0.92
    consecutive_frames: int = 3
    stable_seconds: float = 0.45
    max_frame_age: float = 0.35
    poll_seconds: float = 0.15
    position_tolerance: int = 3
    round_timeout: float = 120.0
    verification_timeout: float = 5.0
    hotkey: str = "Ctrl+Alt+F11"

    def validate(self) -> None:
        limits = {
            "threshold": (0.8, 0.9999),
            "consecutive_frames": (2, 10),
            "stable_seconds": (0.2, 5),
            "max_frame_age": (0.05, 0.5),
            "poll_seconds": (0.1, 2),
            "position_tolerance": (0, 8),
            "round_timeout": (5, 3600),
            "verification_timeout": (1, 30),
        }
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} 必须为数值")
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} 超出允许范围 {low}..{high}")
        for name in ("consecutive_frames", "position_tolerance"):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} 必须为整数")


@dataclass(frozen=True)
class Snapshot:
    state: State
    reason: str
    session_id: str
    attempted: bool
    count: int
    detection: Detection | None = None
    frame: Frame | None = None
    elapsed: float = 0.0


def preview_to_pixel(x: float, y: float, display: Rect, image_size: tuple[int, int]):
    """Qt logical widget point to image pixel; letterboxing is explicit."""
    if not display.contains((int(x), int(y))):
        return None
    width, height = image_size
    return (
        min(width - 1, int((x - display.x) * width / display.width)),
        min(height - 1, int((y - display.y) * height / display.height)),
    )
