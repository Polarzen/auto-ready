"""Persistent user configuration and template image storage."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from .models import Geometry, Region, Settings, Template

SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024
MAX_TEMPLATE_BYTES = 8 * 1024 * 1024
MAX_TEMPLATE_DIMENSION = 4096
MAX_TEMPLATE_PIXELS = 16 * 1024 * 1024
_TEMPLATE_REF = re.compile(r"templates/[A-Za-z0-9_-]{1,100}\.png\Z")


def _default_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "auto-ready"
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "auto-ready"
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "auto-ready"
    return Path.home() / ".local" / "share" / "auto-ready"


def _validate_image(image: np.ndarray) -> None:
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8:
        raise ValueError("模板图像必须是 uint8 像素数组")
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("模板图像必须是三通道 BGR PNG")
    height, width = image.shape[:2]
    if (width < 1 or height < 1 or width > MAX_TEMPLATE_DIMENSION
            or height > MAX_TEMPLATE_DIMENSION or width * height > MAX_TEMPLATE_PIXELS):
        raise ValueError("模板图像尺寸超出允许范围")

    from .recognition import validate_template

    validate_template(image)


def _validate_settings(settings: Settings) -> None:
    settings.validate()
    if not isinstance(settings.hotkey, str) or not 1 <= len(settings.hotkey) <= 100:
        raise ValueError("hotkey 必须是 1 到 100 个字符的快捷键字符串")


def _validate_region_values(data: dict[str, Any]) -> None:
    for name in ("x", "y", "width", "height"):
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"region.{name} 必须为数值")


def _safe_template_path(root: Path, reference: Any) -> Path:
    if not isinstance(reference, str) or not _TEMPLATE_REF.fullmatch(reference):
        raise ValueError("配置中的模板文件名无效；只允许 templates/ 下的 PNG 文件")
    try:
        root_abs = root.resolve()
        path = (root / Path(*reference.split("/"))).resolve()
        path.relative_to(root_abs)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("模板文件路径超出配置目录") from exc
    return path


def load_template(path: Path, geometry: Geometry) -> Template:
    """Load a bounded PNG template, associating it with current client geometry."""
    template_path = Path(path)
    if template_path.suffix.lower() != ".png":
        raise ValueError("模板文件必须为 PNG")
    try:
        with template_path.open("rb") as stream:
            data = stream.read(MAX_TEMPLATE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"无法读取模板 PNG：{exc}") from exc
    if len(data) <= 0 or len(data) > MAX_TEMPLATE_BYTES:
        raise ValueError("模板 PNG 文件大小超出允许范围")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("模板文件不是有效的 PNG")
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise ValueError("模板 PNG 缺少有效的图像头")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if (width < 1 or height < 1 or width > MAX_TEMPLATE_DIMENSION
            or height > MAX_TEMPLATE_DIMENSION or width * height > MAX_TEMPLATE_PIXELS):
        raise ValueError("模板图像尺寸超出允许范围")
    try:
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise ValueError("模板 PNG 无法解码") from exc
    if image is None:
        raise ValueError("模板 PNG 无法解码")
    _validate_image(image)
    return Template(image=np.ascontiguousarray(image),
                    reference_size=(geometry.width, geometry.height), dpi=geometry.dpi,
                    name=template_path.stem)


class ConfigStore:
    """Stores explicit user saves under a local, user-writable directory."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else _default_root()
        self.config_path = self.root / "config.json"
        self.templates_path = self.root / "templates"

    def save(self, settings: Settings, region: Region, button: Template | None,
             success: Template | None) -> None:
        _validate_settings(settings)
        region.validate()
        self.root.mkdir(parents=True, exist_ok=True)
        self.templates_path.mkdir(parents=True, exist_ok=True)
        try:
            self.templates_path.resolve().relative_to(self.root.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("模板目录必须位于配置目录内") from exc

        entries: dict[str, dict[str, Any] | None] = {}
        pending_images: list[tuple[Path, bytes]] = []
        for key, template in (("button_template", button), ("success_template", success)):
            if template is None:
                entries[key] = None
                continue
            _validate_image(template.image)
            if (not isinstance(template.reference_size, (tuple, list))
                    or len(template.reference_size) != 2
                    or any(not isinstance(v, int) or isinstance(v, bool) or v <= 0
                           for v in template.reference_size)
                    or not isinstance(template.dpi, int) or isinstance(template.dpi, bool)
                    or template.dpi <= 0):
                raise ValueError(f"{key} 的参考几何无效")
            try:
                ok, encoded = cv2.imencode(".png", template.image)
            except cv2.error as exc:
                raise ValueError(f"{key} 无法编码为 PNG") from exc
            if not ok:
                raise ValueError(f"{key} 无法编码为 PNG")
            png = encoded.tobytes()
            if len(png) > MAX_TEMPLATE_BYTES:
                raise ValueError(f"{key} PNG 文件大小超出允许范围")
            filename = f"{key.replace('_template', '')}-{uuid4().hex}.png"
            reference = f"templates/{filename}"
            entries[key] = {
                "file": reference,
                "reference_size": list(template.reference_size),
                "dpi": template.dpi,
            }
            pending_images.append((self.templates_path / filename, png))

        document = {
            "schema_version": SCHEMA_VERSION,
            "settings": asdict(settings),
            "region": asdict(region),
            **entries,
        }
        encoded_config = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
                          + "\n").encode("utf-8")
        if len(encoded_config) > MAX_CONFIG_BYTES:
            raise ValueError("配置文件大小超出允许范围")

        # Unique template names avoid changing assets referenced by the prior
        # config. Replace JSON last, so any failure before this point preserves it.
        for destination, contents in pending_images:
            self._atomic_write(destination, contents)
        self._atomic_write(self.config_path, encoded_config)

    def save_to(self, path: Path, settings: Settings, region: Region,
                button: Template | None, success: Template | None) -> None:
        """Save a profile JSON and its templates beside the selected file."""
        store = self._store_for_profile(path)
        store.save(settings, region, button, success)

    def load(self) -> tuple[Settings, Region, Template | None, Template | None]:
        return self._load(require_existing=False)

    def _load(self, *, require_existing: bool
              ) -> tuple[Settings, Region, Template | None, Template | None]:
        if not self.config_path.exists():
            if require_existing:
                raise ValueError(f"所选配置文件不存在：{self.config_path}")
            return Settings(), Region(), None, None
        try:
            try:
                self.config_path.resolve().relative_to(self.root.resolve())
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("配置文件路径超出配置目录") from exc
            with self.config_path.open("rb") as stream:
                raw_config = stream.read(MAX_CONFIG_BYTES + 1)
            if len(raw_config) <= 0 or len(raw_config) > MAX_CONFIG_BYTES:
                raise ValueError("配置文件大小无效")
            document = json.loads(raw_config.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"无法读取配置文件 {self.config_path}：{exc}") from exc
        if not isinstance(document, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        version = document.get("schema_version")
        if isinstance(version, bool) or version != SCHEMA_VERSION:
            raise ValueError(f"不支持的配置版本 {version!r}；当前版本为 {SCHEMA_VERSION}")
        allowed_document = {"schema_version", "settings", "region", "button_template",
                            "success_template"}
        if set(document) != allowed_document:
            raise ValueError("配置文件字段不匹配；请检查版本、设置、区域和模板字段")

        try:
            settings_data = document["settings"]
            if not isinstance(settings_data, dict):
                raise ValueError("settings 必须是对象")
            allowed_settings = {item.name for item in fields(Settings)}
            unknown_settings = set(settings_data) - allowed_settings
            # v0.3 profiles retain their original required fields. New optional
            # preferences migrate in memory only; loading never rewrites a file.
            optional_settings = {"capture_method", "sound_enabled"}
            missing_settings = allowed_settings - optional_settings - set(settings_data)
            if unknown_settings or missing_settings:
                raise ValueError(
                    f"settings 字段不匹配 (缺少: {sorted(missing_settings)}, "
                    f"未知: {sorted(unknown_settings)})")
            settings = Settings(**settings_data)
            _validate_settings(settings)

            region_data = document["region"]
            if (not isinstance(region_data, dict)
                    or set(region_data) != {"x", "y", "width", "height"}):
                raise ValueError("region 必须包含 x、y、width、height")
            _validate_region_values(region_data)
            region = Region(**region_data)
            region.validate()
            button = self._load_saved_template(document.get("button_template"), "button_template")
            success = self._load_saved_template(
                document.get("success_template"), "success_template")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"配置文件内容无效：{exc}") from exc
        return settings, region, button, success

    def load_from(self, path: Path) -> tuple[Settings, Region, Template | None, Template | None]:
        """Load an explicit profile, reporting a missing selection as an error."""
        store = self._store_for_profile(path, require_existing=True)
        return store._load(require_existing=True)

    @staticmethod
    def _store_for_profile(path: Path, *, require_existing: bool = False) -> ConfigStore:
        profile_path = Path(path)
        if profile_path.suffix.lower() != ".json":
            raise ValueError("配置文件必须使用 .json 扩展名")

        try:
            profile_parent = profile_path.parent.resolve()
            resolved_profile = profile_path.resolve()
            resolved_profile.relative_to(profile_parent)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("配置文件路径必须位于所选文件的目录内") from exc

        if profile_path.is_dir():
            raise ValueError("所选配置路径必须是文件")
        if require_existing and not profile_path.is_file():
            raise ValueError(f"所选配置文件不存在或不是文件：{profile_path}")

        store = ConfigStore(profile_path.parent)
        store.config_path = profile_path
        return store

    def _load_saved_template(self, entry: Any, key: str) -> Template | None:
        if entry is None:
            return None
        if not isinstance(entry, dict):
            raise ValueError(f"{key} 必须为 null 或对象")
        if set(entry) != {"file", "reference_size", "dpi"}:
            raise ValueError(f"{key} 字段不匹配")
        reference_size = entry.get("reference_size")
        dpi = entry.get("dpi")
        if (not isinstance(reference_size, list) or len(reference_size) != 2
                or any(not isinstance(v, int) or isinstance(v, bool) or v <= 0
                       for v in reference_size)
                or not isinstance(dpi, int) or isinstance(dpi, bool) or dpi <= 0):
            raise ValueError(f"{key} 的参考几何无效")
        path = _safe_template_path(self.root, entry.get("file"))
        geometry = Geometry(0, 0, reference_size[0], reference_size[1], dpi)
        return load_template(path, geometry)

    @staticmethod
    def _atomic_write(destination: Path, contents: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp",
                                         dir=destination.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, destination)
        except BaseException:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def setup_logging(root: Path) -> None:
    """Enable small rotating text logs; screenshots and image data are never logged."""
    log_root = Path(root)
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = (log_root / "auto-ready.log").resolve()
    logger = logging.getLogger("auto_ready")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_path:
            return
    handler = RotatingFileHandler(log_path, maxBytes=1_048_576, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
