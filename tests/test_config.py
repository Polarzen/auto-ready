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
