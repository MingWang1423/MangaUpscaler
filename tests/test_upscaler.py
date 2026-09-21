"""test_upscaler.py — 验证 upscale_folder 的智能倍率选择与跳过逻辑。

不真正调用 waifu2x-ncnn-vulkan.exe，用 mock 替代超分，重点验证 target
（短边/长边）下的智能倍率选择、已达目标时跳过并字节级复制，以及原画质模式。
"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from upscaler.waifu2x import upscale_folder


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_image(dir_path: Path, name: str, width: int, height: int) -> Path:
    path = dir_path / name
    Image.new("RGB", (width, height), (200, 30, 30)).save(path)
    return path


class SmartScaleSkipTest(unittest.TestCase):
    """智能倍率选择 + 已达目标跳过时字节级复制。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, target=None, scale=2):
        with mock.patch("upscaler.waifu2x.upscale_image", return_value=True) as m:
            result = upscale_folder(
                self.input_dir, self.output_dir, scale=scale, target=target,
            )
        return result, m

    def test_original_mode_upscales_all(self):
        _make_image(self.input_dir, "small.png", 100, 100)
        _make_image(self.input_dir, "big.png", 5000, 3000)
        result, m = self._run(target=None, scale=2)
        self.assertEqual(result, (2, 0, 0, 0))
        self.assertEqual(m.call_count, 2)  # 原画质：全部按用户倍率超分

    def test_over_target_skips_and_copies_bytes(self):
        p = _make_image(self.input_dir, "big.png", 5000, 3000)  # 已超 4K
        result, m = self._run(target=(2160, 3840), scale=2)
        self.assertEqual(result, (0, 1, 0, 0))
        self.assertEqual(m.call_count, 0)
        self.assertEqual(_sha256(p), _sha256(self.output_dir / "big.png"))

    def test_under_target_upscales(self):
        _make_image(self.input_dir, "small.png", 100, 100)
        result, m = self._run(target=(2160, 3840), scale=2)
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(m.call_count, 1)

    def test_uses_two_x_when_sufficient(self):
        _make_image(self.input_dir, "page.png", 2000, 3000)  # 2× 已够，不选 4×
        result, m = self._run(target=(2160, 3840), scale=4)
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(m.call_args.kwargs["scale"], 2)

    def test_single_failure_preserves_and_continues(self):
        _make_image(self.input_dir, "a.png", 100, 100)
        p_b = _make_image(self.input_dir, "b.png", 200, 200)
        with mock.patch("upscaler.waifu2x.upscale_image",
                        side_effect=[True, False]) as m:
            result = upscale_folder(self.input_dir, self.output_dir)
        self.assertEqual(result, (1, 0, 0, 1))  # 1 成功，1 失败
        self.assertEqual(m.call_count, 2)        # 两张都处理了（失败继续）
        self.assertEqual(_sha256(p_b), _sha256(self.output_dir / "b.png"))


if __name__ == "__main__":
    unittest.main()
