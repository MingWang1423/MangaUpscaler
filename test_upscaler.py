"""test_upscaler.py — 验证 upscale_folder 的「智能跳过超分」判断逻辑。

Batch 2 专用：不真正调用 waifu2x-ncnn-vulkan.exe，用 mock 替代超分，
重点验证 skip_if_larger_than 的边界判定与跳过时字节级复制。
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


class SkipIfLargerThanTest(unittest.TestCase):
    """skip_if_larger_than 的边界判定 + 跳过时字节级复制。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, skip_if_larger_than):
        with mock.patch("upscaler.waifu2x.upscale_image", return_value=True) as m:
            result = upscale_folder(
                self.input_dir, self.output_dir,
                skip_if_larger_than=skip_if_larger_than,
            )
        return result, m.call_count

    def test_none_upscales_all(self):
        _make_image(self.input_dir, "small.png", 100, 100)
        _make_image(self.input_dir, "big.png", 5000, 3000)
        result, call_count = self._run(None)
        self.assertEqual(result, (2, 0, 0))
        self.assertEqual(call_count, 2)  # None：全部走超分

    def test_fits_in_box_upscales(self):
        _make_image(self.input_dir, "page01.png", 2000, 1000)  # 两边都不超
        result, call_count = self._run((2160, 3840))
        self.assertEqual(result, (1, 0, 0))
        self.assertEqual(call_count, 1)

    def test_width_exceeds_skips(self):
        p = _make_image(self.input_dir, "page02.png", 5000, 2000)  # 宽超、高不超
        result, call_count = self._run((2160, 3840))
        self.assertEqual(result, (0, 0, 1))
        self.assertEqual(call_count, 0)
        self.assertEqual(_sha256(p), _sha256(self.output_dir / "page02.png"))

    def test_height_exceeds_skips(self):
        p = _make_image(self.input_dir, "page03.png", 2000, 4000)  # 高超、宽不超
        result, call_count = self._run((2160, 3840))
        self.assertEqual(result, (0, 0, 1))
        self.assertEqual(call_count, 0)
        self.assertEqual(_sha256(p), _sha256(self.output_dir / "page03.png"))

    def test_both_exceed_skips(self):
        p = _make_image(self.input_dir, "page04.png", 3000, 4000)  # 两边都超
        result, call_count = self._run((2160, 3840))
        self.assertEqual(result, (0, 0, 1))
        self.assertEqual(call_count, 0)
        self.assertEqual(_sha256(p), _sha256(self.output_dir / "page04.png"))


if __name__ == "__main__":
    unittest.main()
