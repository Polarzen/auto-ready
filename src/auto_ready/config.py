"""Persistent user configuration and template image storage."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import asdict, dataclass, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .models import Geometry, Region, Settings, Template

SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 256 * 1024
MAX_TEMPLATE_BYTES = 8 * 1024 * 1024
MAX_TEMPLATE_DIMENSION = 4096
MAX_TEMPLATE_PIXELS = 16 * 1024 * 1024
_TEMPLATE_REF = re.compile(r"templates/[A-Za-z0-9_-]{1,100}\.png\Z")


@dataclass(frozen=True)
class ProfileMetadata:
    """Display-only labels attached to a saved profile."""

    name: str = ""
    game: str = ""

    def validate(self) -> None:
        for field_name in ("name", "game"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"profile.{field_name} 必须是字符串")
            if len(value) > 80:
                raise ValueError(f"profile.{field_name} 不能超过 80 个字符")
            if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
                raise ValueError(f"profile.{field_name} 不能包含控制字符或无效 Unicode")


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


def _safe_template_destination(root: Path, reference: str) -> Path:
    """Validate an asset destination while preserving its lexical filename."""
    _safe_template_path(root, reference)
    return root / Path(*reference.split("/"))


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
             success: Template | None, *, metadata: ProfileMetadata | None = None) -> None:
        _validate_settings(settings)
        region.validate()
        if metadata is None:
            metadata = self._existing_profile_metadata()
        elif not isinstance(metadata, ProfileMetadata):
            raise ValueError("metadata 必须是 ProfileMetadata")
        if metadata is not None:
            metadata.validate()

        entries: dict[str, dict[str, Any] | None] = {}
        pending_images: dict[Path, bytes] = {}
        existing_images = self._currently_referenced_template_images()
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
            digest = hashlib.sha256(png).hexdigest()
            filename = f"image-{digest}.png"
            content_reference = f"templates/{filename}"
            destination = _safe_template_destination(self.root, content_reference)
            asset_exists = destination.exists() or destination.is_symlink()
            previous_reference = existing_images.get(key)
            if (previous_reference is not None
                    and np.array_equal(previous_reference[1], template.image)):
                reference = previous_reference[0]
                if reference == content_reference:
                    if not asset_exists:
                        raise ValueError(
                            f"{key} 的内容寻址 PNG 不存在：{destination}")
                    self._validate_content_asset(key, destination, png)
            elif asset_exists:
                self._validate_content_asset(key, destination, png)
                reference = content_reference
            else:
                reference = content_reference
                pending_images[destination] = png
            entries[key] = {
                "file": reference,
                "reference_size": list(template.reference_size),
                "dpi": template.dpi,
            }

        document = {
            "schema_version": SCHEMA_VERSION,
            "settings": asdict(settings),
            "region": asdict(region),
            **entries,
        }
        if metadata is not None:
            document["profile"] = asdict(metadata)
        encoded_config = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
                          + "\n").encode("utf-8")
        if len(encoded_config) > MAX_CONFIG_BYTES:
            raise ValueError("配置文件大小超出允许范围")

        self.root.mkdir(parents=True, exist_ok=True)
        self.templates_path.mkdir(parents=True, exist_ok=True)
        try:
            self.templates_path.resolve().relative_to(self.root.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("模板目录必须位于配置目录内") from exc

        # Immutable content-addressed names preserve assets referenced by prior
        # configs. Replace JSON last, so failures preserve the previous profile.
        for destination, contents in pending_images.items():
            self._atomic_write(destination, contents)
        self._atomic_write(self.config_path, encoded_config)

    def save_to(self, path: Path, settings: Settings, region: Region,
                button: Template | None, success: Template | None, *,
                metadata: ProfileMetadata | None = None) -> None:
        """Save a profile JSON and its templates beside the selected file."""
        store = self._store_for_profile(path)
        store.save(settings, region, button, success, metadata=metadata)

    def load(self) -> tuple[Settings, Region, Template | None, Template | None]:
        return self._load(require_existing=False)

    def _load(self, *, require_existing: bool
              ) -> tuple[Settings, Region, Template | None, Template | None]:
        settings, region, button, success, _, _ = self._load_profile_data(
            require_existing=require_existing)
        return settings, region, button, success

    def load_profile(self, path: Path | None = None
                     ) -> tuple[Settings, Region, Template | None, Template | None,
                                ProfileMetadata]:
        """Load a profile and its display labels from one JSON snapshot."""
        if path is None:
            return self._load_profile_data(require_existing=False)[:5]
        store = self._store_for_profile(path, require_existing=True)
        return store._load_profile_data(require_existing=True)[:5]

    def read_profile_metadata(self, path: Path | None = None) -> ProfileMetadata:
        """Read validated labels without decoding referenced PNG files."""
        if path is None:
            store = self
            require_existing = False
        else:
            store = self._store_for_profile(path, require_existing=True)
            require_existing = True
        parsed = store._load_profile_data(require_existing=require_existing,
                                          decode_templates=False)
        return parsed[4]

    def _read_config_document(self, *, require_existing: bool) -> Any | None:
        if not self.config_path.exists():
            if require_existing:
                raise ValueError(f"所选配置文件不存在：{self.config_path}")
            return None
        try:
            try:
                self.config_path.resolve().relative_to(self.root.resolve())
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("配置文件路径超出配置目录") from exc
            with self.config_path.open("rb") as stream:
                raw_config = stream.read(MAX_CONFIG_BYTES + 1)
            if len(raw_config) <= 0 or len(raw_config) > MAX_CONFIG_BYTES:
                raise ValueError("配置文件大小无效")
            return json.loads(raw_config.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"无法读取配置文件 {self.config_path}：{exc}") from exc

    def _load_profile_data(self, *, require_existing: bool,
                           decode_templates: bool = True
                           ) -> tuple[Settings, Region, Template | None, Template | None,
                                      ProfileMetadata, bool]:
        document = self._read_config_document(require_existing=require_existing)
        if document is None:
            return Settings(), Region(), None, None, ProfileMetadata(), False
        if not isinstance(document, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        version = document.get("schema_version")
        if isinstance(version, bool) or version != SCHEMA_VERSION:
            raise ValueError(f"不支持的配置版本 {version!r}；当前版本为 {SCHEMA_VERSION}")
        required_document = {"schema_version", "settings", "region", "button_template",
                             "success_template"}
        if (not required_document.issubset(document)
                or set(document) - required_document - {"profile"}):
            raise ValueError("配置文件字段不匹配；请检查版本、设置、区域和模板字段")

        try:
            has_profile = "profile" in document
            metadata = self._parse_profile_metadata(document["profile"]) if has_profile else (
                ProfileMetadata())
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
            if decode_templates:
                button = self._load_saved_template(document["button_template"],
                                                   "button_template")
                success = self._load_saved_template(document["success_template"],
                                                    "success_template")
            else:
                self._validate_saved_template_entry(document["button_template"],
                                                    "button_template")
                self._validate_saved_template_entry(document["success_template"],
                                                    "success_template")
                button = success = None
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"配置文件内容无效：{exc}") from exc
        return settings, region, button, success, metadata, has_profile

    @staticmethod
    def _parse_profile_metadata(data: Any) -> ProfileMetadata:
        if not isinstance(data, dict) or set(data) != {"name", "game"}:
            raise ValueError("profile 必须只包含 name、game 字段")
        metadata = ProfileMetadata(**data)
        metadata.validate()
        return metadata

    def _existing_profile_metadata(self) -> ProfileMetadata | None:
        """Preserve a valid existing profile label object during ordinary saves."""
        try:
            parsed = self._load_profile_data(require_existing=False,
                                             decode_templates=False)
        except ValueError:
            return None
        return parsed[4] if parsed[5] else None

    def _currently_referenced_template_images(self) -> dict[str, tuple[str, np.ndarray]]:
        """Read only this profile's bounded, validated references for pixel reuse."""
        try:
            document = self._read_config_document(require_existing=False)
        except ValueError:
            return {}
        if (not isinstance(document, dict)
                or isinstance(document.get("schema_version"), bool)
                or document.get("schema_version") != SCHEMA_VERSION):
            return {}

        images: dict[str, tuple[str, np.ndarray]] = {}
        for key in ("button_template", "success_template"):
            try:
                path, reference_size, dpi = self._validate_saved_template_entry(
                    document.get(key), key)
                if path is None:
                    continue
                geometry = Geometry(0, 0, reference_size[0], reference_size[1], dpi)
                loaded = load_template(path, geometry)
            except (TypeError, ValueError):
                # A stale, malformed, or unreadable old reference is not trusted.
                continue
            images[key] = (document[key]["file"], loaded.image)
        return images

    @staticmethod
    def _validate_content_asset(key: str, destination: Path, png: bytes) -> None:
        try:
            with destination.open("rb") as stream:
                existing_png = stream.read(MAX_TEMPLATE_BYTES + 1)
        except OSError as exc:
            raise ValueError(f"{key} 的内容寻址 PNG 无法读取：{destination}") from exc
        if existing_png != png:
            raise ValueError(f"{key} 的内容寻址 PNG 已存在但内容不匹配：{destination}")

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
        path, reference_size, dpi = self._validate_saved_template_entry(entry, key)
        if path is None:
            return None
        geometry = Geometry(0, 0, reference_size[0], reference_size[1], dpi)
        return load_template(path, geometry)

    def _validate_saved_template_entry(self, entry: Any, key: str
                                       ) -> tuple[Path | None, list[int] | None, int | None]:
        if entry is None:
            return None, None, None
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
        return path, reference_size, dpi

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
