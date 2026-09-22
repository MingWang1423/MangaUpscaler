"""test_image_formats.py — 验证图片格式统一处理策略。

覆盖：动态 GIF 多帧及字节保持、静态 GIF 转换后超分、BMP 转换后超分、
SVG 原样保留、损坏图片回退、JPG/PNG/WebP 原有流程回归，以及压缩阶段的
SVG / 损坏图片回退。超分全部走批处理，子进程用 tests/fake_waifu2x.py 的替身，
不真正调用 waifu2x exe。
"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from upscaler.compressor import compress_folder
from upscaler.waifu2x import upscale_folder

try:  # python -m unittest discover -s tests ...
    from fake_waifu2x import Waifu2xProcessRecorder, fake_exe, fake_processes
except ImportError:  # python -m unittest tests.test_image_formats
    from tests.fake_waifu2x import Waifu2xProcessRecorder, fake_exe, fake_processes


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UpscaleFormatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, recorder=None, **kwargs):
        """跑一次 upscale_folder（子进程用假替身）；返回 (结果, 进程记录器)。"""
        recorder = recorder or Waifu2xProcessRecorder()
        with fake_exe(), fake_processes(recorder):
            result = upscale_folder(self.input_dir, self.output_dir, **kwargs)
        return result, recorder

    def test_jpg_png_webp_upscaled(self):
        Image.new("RGB", (10, 10), (1, 1, 1)).save(self.input_dir / "a.jpg", "JPEG")
        Image.new("RGB", (10, 10), (2, 2, 2)).save(self.input_dir / "b.png", "PNG")
        Image.new("RGB", (10, 10), (3, 3, 3)).save(self.input_dir / "c.webp", "WEBP")

        with mock.patch("upscaler.waifu2x.upscale_image") as fallback:
            result, recorder = self._run()

        self.assertEqual(result, (3, 0, 0, 0))
        self.assertEqual(recorder.count, 3)              # 三种格式各一批
        self.assertEqual(fallback.call_count, 0)         # 全部走批次，无需回退
        # 输出扩展名与输入扩展名保持一致
        self.assertEqual(sorted(p.name for p in self.output_dir.iterdir()),
                         ["a.jpg", "b.png", "c.webp"])
        for name in ("a.jpg", "b.png", "c.webp"):
            self.assertTrue((self.output_dir / name).is_file())

    def test_animated_gif_preserved_bytes(self):
        p = self.input_dir / "anim.gif"
        f1 = Image.new("RGB", (100, 100), (255, 0, 0))
        f2 = Image.new("RGB", (100, 100), (0, 255, 0))
        f1.save(p, save_all=True, append_images=[f2], format="GIF",
                duration=100, loop=0)

        result, recorder = self._run()

        self.assertEqual(result, (0, 0, 1, 0))       # 原样复制，不超分
        self.assertEqual(recorder.count, 0)          # 不进入 waifu2x 批次
        self.assertEqual(_sha256(p), _sha256(self.output_dir / "anim.gif"))
        with Image.open(self.output_dir / "anim.gif") as im:
            self.assertTrue(im.is_animated)
            self.assertEqual(im.n_frames, 2)

    def test_static_gif_upscaled(self):
        p = self.input_dir / "static.gif"
        Image.new("RGB", (50, 40), (10, 20, 30)).save(p, "GIF")

        result, recorder = self._run()

        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(recorder.count, 1)          # 先转 PNG 再进批次
        with Image.open(self.output_dir / "static.gif") as im:
            self.assertEqual(im.format, "GIF")
            self.assertEqual(im.size, (100, 80))
            self.assertFalse(getattr(im, "is_animated", False))

    def test_bmp_upscaled(self):
        p = self.input_dir / "pic.bmp"
        Image.new("RGB", (60, 30), (1, 2, 3)).save(p, "BMP")

        result, recorder = self._run()

        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(recorder.count, 1)          # 先转 PNG 再进批次
        with Image.open(self.output_dir / "pic.bmp") as im:
            self.assertEqual(im.format, "BMP")
            self.assertEqual(im.size, (120, 60))

    def test_svg_preserved(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
        p = self.input_dir / "img.svg"
        p.write_bytes(svg)

        result, recorder = self._run()

        self.assertEqual(result, (0, 0, 1, 0))
        self.assertEqual(recorder.count, 0)          # 不进入 waifu2x 批次
        self.assertEqual((self.output_dir / "img.svg").read_bytes(), svg)

    def test_corrupt_image_fallback(self):
        p = self.input_dir / "broken.jpg"
        p.write_bytes(b"not a real jpeg")

        result, recorder = self._run()

        self.assertEqual(result, (0, 0, 0, 1))       # 失败，回退原样复制
        self.assertEqual(recorder.count, 0)          # 损坏图片不进批次
        self.assertEqual((self.output_dir / "broken.jpg").read_bytes(),
                         b"not a real jpeg")


class CompressFormatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_svg_preserved(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
        self.input_dir.joinpath("img.svg").write_bytes(svg)
        self.assertEqual(compress_folder(self.input_dir, self.output_dir, "4k"),
                         (1, 0))
        self.assertEqual((self.output_dir / "img.svg").read_bytes(), svg)

    def test_corrupt_image_fallback(self):
        p = self.input_dir / "broken.png"
        p.write_bytes(b"not a real png")
        self.assertEqual(compress_folder(self.input_dir, self.output_dir, "4k"),
                         (0, 1))
        self.assertEqual((self.output_dir / "broken.png").read_bytes(),
                         b"not a real png")


if __name__ == "__main__":
    unittest.main()
