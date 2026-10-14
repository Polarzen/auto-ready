import json
import logging
import sys

import numpy as np
import pytest

from auto_ready.config import ConfigStore, load_template, setup_logging
from auto_ready.models import Geometry, Region, Settings, Template


def make_template(name="button"):
    rng = np.random.default_rng(25)
    image = rng.integers(0, 256, size=(24, 31, 3), dtype=np.uint8)
    return Template(image, (1280, 720), 144, name)


def test_missing_config_uses_disarmed_defaults(tmp_path):
    settings, region, button, success = ConfigStore(tmp_path).load()

    assert settings == Settings()
    assert region == Region()
    assert button is None
    assert success is None


def test_v030_settings_migrate_only_in_memory(tmp_path):
    store = ConfigStore(tmp_path)
    store.save(Settings(), Region(), make_template(), None)
    document = json.loads(store.config_path.read_text(encoding="utf-8"))
    document["settings"].pop("capture_method")
    document["settings"].pop("sound_enabled")
    store.config_path.write_text(json.dumps(document), encoding="utf-8")
    before = store.config_path.read_bytes()
    settings, _, button, _ = store.load()
    assert settings.capture_method == "auto"
    assert settings.sound_enabled is True
    assert button is not None
    assert store.config_path.read_bytes() == before


def test_new_capture_preferences_round_trip_without_authority(tmp_path):
    store = ConfigStore(tmp_path)
    settings = Settings(capture_method="gdi", sound_enabled=False)
    store.save(settings, Region(), make_template(), None)
    assert store.load()[0] == settings
    data = json.loads(store.config_path.read_text(encoding="utf-8"))
    assert not ({"identity", "hwnd", "pid", "mode", "allow_activation", "session"}
                & set(data["settings"]))
    for key, value in (("capture_method", "unknown"), ("sound_enabled", "yes")):
        original = data["settings"][key]
        data["settings"][key] = value
        store.config_path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError):
            store.load()
        data["settings"][key] = original


def test_round_trip_stores_pngs_and_only_configuration(tmp_path):
    store = ConfigStore(tmp_path)
    settings = Settings(threshold=0.95, hotkey="Ctrl+Alt+F11")
    region = Region(0.1, 0.2, 0.7, 0.6)
    button, success = make_template("button"), make_template("success")

    store.save(settings, region, button, success)
    loaded_settings, loaded_region, loaded_button, loaded_success = store.load()

    assert loaded_settings == settings
    assert loaded_region == region
    assert loaded_button is not None and loaded_button.reference_size == button.reference_size
    assert loaded_success is not None and loaded_success.dpi == success.dpi
    np.testing.assert_array_equal(loaded_button.image, button.image)
    np.testing.assert_array_equal(loaded_success.image, success.image)
    document = json.loads(store.config_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert "mode" not in document and "armed" not in document
    assert "hwnd" not in document and "pid" not in document
    assert list((tmp_path / "templates").glob("*.png"))


def test_explicit_profiles_round_trip_independently_in_shared_directory(tmp_path):
    store = ConfigStore(tmp_path / "default")
    store.save(Settings(threshold=0.88), Region(), make_template("default"), None)
    default_json = store.config_path.read_bytes()
    default_assets = set(store.templates_path.glob("*.png"))
    profile_dir = tmp_path / "profiles with spaces"
    first_path = profile_dir / "配置 文件.json"
    second_path = profile_dir / "second profile.JSON"
    first_settings = Settings(threshold=0.91, hotkey="Ctrl+F8")
    second_settings = Settings(threshold=0.97, hotkey="Ctrl+F9")
    first_template = make_template("first")
    second_template = make_template("second")
    second_template.image[:] = 255 - second_template.image

    store.save_to(first_path, first_settings, Region(0.1, 0.2, 0.3, 0.4),
                  first_template, None)
    first_json = first_path.read_bytes()
    store.save_to(second_path, second_settings, Region(0.4, 0.3, 0.2, 0.1),
                  second_template, None)

    loaded_first = store.load_from(first_path)
    loaded_second = store.load_from(second_path)
    assert loaded_first[0] == first_settings
    assert loaded_first[1] == Region(0.1, 0.2, 0.3, 0.4)
    assert loaded_first[2] is not None
    np.testing.assert_array_equal(loaded_first[2].image, first_template.image)
    assert loaded_second[0] == second_settings
    assert loaded_second[1] == Region(0.4, 0.3, 0.2, 0.1)
    assert loaded_second[2] is not None
    np.testing.assert_array_equal(loaded_second[2].image, second_template.image)
    assert first_path.read_bytes() == first_json
    assert len(list((profile_dir / "templates").glob("*.png"))) == 2
    assert store.config_path.read_bytes() == default_json
    assert set(store.templates_path.glob("*.png")) == default_assets
    assert store.load()[0] == Settings(threshold=0.88)


def test_explicit_profile_load_rejects_missing_and_invalid_paths(tmp_path):
    store = ConfigStore(tmp_path)
    missing_path = tmp_path / "missing.json"

    with pytest.raises(ValueError, match="不存在或不是文件"):
        store.load_from(missing_path)
    with pytest.raises(ValueError, match=".json"):
        store.save_to(tmp_path / "profile.txt", Settings(), Region(), None, None)
    with pytest.raises(ValueError, match=".json"):
        store.load_from(tmp_path / "profile.txt")

    directory_path = tmp_path / "directory.json"
    directory_path.mkdir()
    with pytest.raises(ValueError, match="必须是文件"):
        store.save_to(directory_path, Settings(), Region(), None, None)
    with pytest.raises(ValueError, match="必须是文件"):
        store.load_from(directory_path)


def test_explicit_profile_load_errors_if_file_disappears_after_validation(
        tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    profile_path = tmp_path / "profile.json"
    store.save_to(profile_path, Settings(), Region(), None, None)
    original = ConfigStore._store_for_profile

    def remove_after_validation(path, *, require_existing=False):
        validated_store = original(path, require_existing=require_existing)
        profile_path.unlink()
        return validated_store

    monkeypatch.setattr(ConfigStore, "_store_for_profile",
                        staticmethod(remove_after_validation))

    with pytest.raises(ValueError, match="所选配置文件不存在"):
        store.load_from(profile_path)


def test_explicit_profile_rejects_json_symlink_outside_selected_directory(tmp_path):
    store = ConfigStore(tmp_path)
    selected_dir = tmp_path / "selected"
    selected_dir.mkdir()
    outside_path = tmp_path / "outside.json"
    outside_path.write_text("{}", encoding="utf-8")
    selected_path = selected_dir / "profile.json"
    try:
        selected_path.symlink_to(outside_path)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="配置文件路径必须位于所选文件的目录内"):
        store.load_from(selected_path)


def test_failed_explicit_json_replacement_preserves_selected_profile(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "default")
    profile_path = tmp_path / "profiles" / "custom.json"
    store.save_to(profile_path, Settings(), Region(), make_template(), None)
    previous = profile_path.read_bytes()
    original = ConfigStore._atomic_write

    def fail_selected_config_write(destination, contents):
        if destination == profile_path:
            raise OSError("simulated disk error")
        original(destination, contents)

    monkeypatch.setattr(ConfigStore, "_atomic_write",
                        staticmethod(fail_selected_config_write))
    with pytest.raises(OSError, match="simulated disk error"):
        store.save_to(profile_path, Settings(threshold=0.96), Region(), make_template(), None)

    assert profile_path.read_bytes() == previous
    assert store.load_from(profile_path)[0] == Settings()
    assert not store.config_path.exists()


def test_corrupt_config_has_actionable_error(tmp_path):
    store = ConfigStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    store.config_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="无法读取配置文件"):
        store.load()


def test_config_rejects_template_path_traversal(tmp_path):
    store = ConfigStore(tmp_path)
    store.save(Settings(), Region(), make_template(), None)
    document = json.loads(store.config_path.read_text(encoding="utf-8"))
    document["button_template"]["file"] = "templates/../../outside.png"
    store.config_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="模板文件名无效"):
        store.load()


def test_template_validation_fails_closed_if_recognition_is_unavailable(
        tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    monkeypatch.setitem(sys.modules, "auto_ready.recognition", None)

    with pytest.raises(ModuleNotFoundError):
        store.save(Settings(), Region(), make_template(), None)


def test_failed_json_replacement_preserves_existing_config(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.save(Settings(), Region(), make_template(), None)
    previous = store.config_path.read_bytes()
    original = ConfigStore._atomic_write

    def fail_config_write(destination, contents):
        if destination.name == "config.json":
            raise OSError("simulated disk error")
        original(destination, contents)

    monkeypatch.setattr(ConfigStore, "_atomic_write", staticmethod(fail_config_write))
    with pytest.raises(OSError, match="simulated disk error"):
        store.save(Settings(threshold=0.96), Region(), make_template(), None)

    assert store.config_path.read_bytes() == previous
    assert store.load()[0] == Settings()


def test_load_template_rejects_oversized_dimensions_before_decode(tmp_path):
    path = tmp_path / "large.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
                     + (5000).to_bytes(4, "big") + (10).to_bytes(4, "big")
                     + b"\x08\x02\x00\x00\x00")

    with pytest.raises(ValueError, match="图像尺寸超出"):
        load_template(path, Geometry(0, 0, 100, 100, 96))


def test_setup_logging_uses_a_rotating_text_log(tmp_path):
    logger = logging.getLogger("auto_ready")
    old_handlers = set(logger.handlers)
    old_level, old_propagate = logger.level, logger.propagate
    setup_logging(tmp_path)
    try:
        logger.info("simulator state changed")
        log_path = tmp_path / "auto-ready.log"
        assert log_path.is_file()
        assert "simulator state changed" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in set(logger.handlers) - old_handlers:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(old_level)
        logger.propagate = old_propagate
