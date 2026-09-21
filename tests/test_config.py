"""test_config.py — 配置严格校验、损坏/缺失字段回退、原子写入。"""

import json
import tempfile
import unittest
from pathlib import Path

from config_schema import DEFAULT_CONFIG, sanitize_config
from main import load_config, save_config


class SanitizeConfigTest(unittest.TestCase):
    def test_valid_config_unchanged(self):
        data = {"scale": 4, "noise": -1, "quality": "2k", "gpu": 1,
                "output_dir": "D:/out"}
        config, changed = sanitize_config(data, "output")
        self.assertFalse(changed)
        self.assertEqual(config, {"scale": 4, "noise": -1, "quality": "2k",
                                  "gpu": 1, "output_dir": "D:/out"})

    def test_invalid_scale(self):
        for bad in (3, 0, "2", 2.0, True, None):
            config, changed = sanitize_config({"scale": bad}, "output")
            self.assertEqual(config["scale"], DEFAULT_CONFIG["scale"])
            self.assertTrue(changed)

    def test_invalid_noise(self):
        for bad in (4, -2, True, False, "1", None):
            config, changed = sanitize_config({"noise": bad}, "output")
            self.assertEqual(config["noise"], DEFAULT_CONFIG["noise"])
            self.assertTrue(changed)

    def test_invalid_quality(self):
        for bad in ("foo", 99, "", None):
            config, changed = sanitize_config({"quality": bad}, "output")
            self.assertEqual(config["quality"], DEFAULT_CONFIG["quality"])
            self.assertTrue(changed)

    def test_legacy_quality_migrated(self):
        config, changed = sanitize_config({"quality": "balanced"}, "output")
        self.assertEqual(config["quality"], "4k")
        self.assertTrue(changed)
        config, _ = sanitize_config({"quality": "small"}, "output")
        self.assertEqual(config["quality"], "2k")

    def test_invalid_gpu(self):
        for bad in (True, False, "0", -2, 2.5, "", None):
            config, changed = sanitize_config({"gpu": bad}, "output")
            self.assertEqual(config["gpu"], "auto")
            self.assertTrue(changed)

    def test_gpu_auto_default(self):
        config, changed = sanitize_config({}, "output")
        self.assertEqual(config["gpu"], "auto")
        self.assertTrue(changed)

    def test_gpu_auto_accepted(self):
        data = {"scale": 2, "noise": 3, "quality": "4k", "gpu": "auto",
                "output_dir": "D:/out"}
        config, changed = sanitize_config(data, "output")
        self.assertEqual(config["gpu"], "auto")
        self.assertFalse(changed)

    def test_gpu_legacy_int_accepted(self):
        for value in (-1, 0, 1, 2):
            data = {"scale": 2, "noise": 3, "quality": "4k", "gpu": value,
                    "output_dir": "D:/out"}
            config, changed = sanitize_config(data, "output")
            self.assertEqual(config["gpu"], value)
            self.assertFalse(changed)

    def test_gpu_nonnegative_accepted(self):
        data = {"scale": 2, "noise": 3, "quality": "4k", "gpu": 5,
                "output_dir": "D:/out"}
        config, changed = sanitize_config(data, "output")
        self.assertEqual(config["gpu"], 5)
        self.assertFalse(changed)

    def test_invalid_output_dir(self):
        for bad in ("", "   ", 123, None):
            config, changed = sanitize_config({"output_dir": bad}, "output")
            self.assertEqual(config["output_dir"], "output")
            self.assertTrue(changed)

    def test_bool_not_valid_number(self):
        # True/False 是 int 子类，但不能被当成有效数字配置
        config, changed = sanitize_config(
            {"scale": True, "noise": True, "gpu": False}, "output"
        )
        self.assertEqual(config["scale"], DEFAULT_CONFIG["scale"])
        self.assertEqual(config["noise"], DEFAULT_CONFIG["noise"])
        self.assertEqual(config["gpu"], DEFAULT_CONFIG["gpu"])
        self.assertTrue(changed)


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_returns_defaults(self):
        path = self.root / "nope.json"
        config = load_config(path)
        self.assertEqual(config["scale"], DEFAULT_CONFIG["scale"])
        self.assertFalse(path.exists())

    def test_corrupt_json_falls_back_and_repairs(self):
        path = self.root / "config.json"
        path.write_text("{ not valid json", encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config["scale"], DEFAULT_CONFIG["scale"])
        self.assertTrue(path.exists())
        repaired = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["scale"], DEFAULT_CONFIG["scale"])

    def test_non_dict_json_repaired(self):
        path = self.root / "config.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config["scale"], DEFAULT_CONFIG["scale"])

    def test_missing_field_repaired(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"scale": 4}), encoding="utf-8")
        config = load_config(path)
        self.assertEqual(config["scale"], 4)
        self.assertEqual(config["noise"], DEFAULT_CONFIG["noise"])
        self.assertEqual(config["gpu"], DEFAULT_CONFIG["gpu"])
        repaired = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("gpu", repaired)


class SaveConfigAtomicTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_atomic_write_no_temp_leftover(self):
        path = self.root / "config.json"
        save_config({"scale": 4, "noise": 1, "quality": "2k", "gpu": -1,
                     "output_dir": "D:/x"}, path)
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["scale"], 4)
        leftovers = [p for p in self.root.iterdir() if ".tmp-" in p.name]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
