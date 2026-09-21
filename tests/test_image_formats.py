"""test_image_formats.py — 验证图片格式统一处理策略。

覆盖：动态 GIF 多帧及字节保持、静态 GIF 转换后超分、BMP 转换后超分、
SVG 原样保留、损坏图片回退、JPG/PNG/WebP 原有流程回归，以及压缩阶段的
SVG / 损坏图片回退。所有超分都用 mock 替代，不真正调用 waifu2x exe。
"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from upscaler.compressor import compress_folder
from upscaler.waifu2x import upscale_folder


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_upscale(input_path, output_path, scale=2, noise=3, gpu="auto"):
    """模拟 waifu2x：把输入放大 2 倍并输出 PNG（用于 BMP/静态 GIF 的转换路径）。"""
    with Image.open(input_path) as im:
        w, h = im.size
        out = im.convert("RGBA").resize((w * 2, h * 2), Image.Resampling.LANCZOS)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    out.save(output_path, "PNG")
    return True


class UpscaleFormatTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **kwargs):
        return upscale_folder(self.input_dir, self.output_dir, **kwargs)

    def test_jpg_png_webp_upscaled(self):
        Image.new("RGB", (10, 10), (1, 1, 1)).save(self.input_dir / "a.jpg", "JPEG")
        Image.new("RGB", (10, 10), (2, 2, 2)).save(self.input_dir / "b.png", "PNG")
        Image.new("RGB", (10, 10), (3, 3, 3)).save(self.input_dir / "c.webp", "WEBP")

        def _touch(input_path, output_path, scale=2, noise=3, gpu="auto"):
            Path(output_path).write_bytes(b"x")
            return True

        with mock.patch("upscaler.waifu2x.upscale_image", side_effect=_touch) as m:
            result = self._run()

        self.assertEqual(result, (3, 0, 0, 0))
        self.assertEqual(m.call_count, 3)
        # 直接交给 waifu2x：输出扩展名与输入扩展名保持一致
        suffixes = sorted(Path(c.args[1]).suffix for c in m.call_args_list)
        self.assertEqual(suffixes, [".jpg", ".png", ".webp"])
        for name in ("a.jpg", "b.png", "c.webp"):
            self.assertTrue((self.output_dir / name).is_file())

    def test_animated_gif_preserved_bytes(self):
        p = self.input_dir / "anim.gif"
        f1 = Image.new("RGB", (100, 100), (255, 0, 0))
        f2 = Image.new("RGB", (100, 100), (0, 255, 0))
        f1.save(p, save_all=True, append_images=[f2], format="GIF",
                duration=100, loop=0)

        with mock.patch("upscaler.waifu2x.upscale_image", return_value=True) as m:
            result = self._run()

        self.assertEqual(result, (0, 0, 1, 0))       # 原样复制，不超分
        self.assertEqual(m.call_count, 0)
        self.assertEqual(_sha256(p), _sha256(self.output_dir / "anim.gif"))
        with Image.open(self.output_dir / "anim.gif") as im:
            self.assertTrue(im.is_animated)
            self.assertEqual(im.n_frames, 2)

    def test_static_gif_upscaled(self):
        p = self.input_dir / "static.gif"
        Image.new("RGB", (50, 40), (10, 20, 30)).save(p, "GIF")

        with mock.patch("upscaler.waifu2x.upscale_image",
                        side_effect=_fake_upscale) as m:
            result = self._run()

        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(m.call_count, 1)
        with Image.open(self.output_dir / "static.gif") as im:
            self.assertEqual(im.format, "GIF")
            self.assertEqual(im.size, (100, 80))
            self.assertFalse(getattr(im, "is_animated", False))

    def test_bmp_upscaled(self):
        p = self.input_dir / "pic.bmp"
        Image.new("RGB", (60, 30), (1, 2, 3)).save(p, "BMP")

        with mock.patch("upscaler.waifu2x.upscale_image",
                        side_effect=_fake_upscale) as m:
            result = self._run()

        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(m.call_count, 1)
        with Image.open(self.output_dir / "pic.bmp") as im:
            self.assertEqual(im.format, "BMP")
            self.assertEqual(im.size, (120, 60))

    def test_svg_preserved(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
        p = self.input_dir / "img.svg"
        p.write_bytes(svg)

        with mock.patch("upscaler.waifu2x.upscale_image", return_value=True) as m:
            result = self._run()

        self.assertEqual(result, (0, 0, 1, 0))
        self.assertEqual(m.call_count, 0)
        self.assertEqual((self.output_dir / "img.svg").read_bytes(), svg)

    def test_corrupt_image_fallback(self):
        p = self.input_dir / "broken.jpg"
        p.write_bytes(b"not a real jpeg")

        with mock.patch("upscaler.waifu2x.upscale_image", return_value=False) as m:
            result = self._run()

        self.assertEqual(result, (0, 0, 0, 1))       # 失败，回退原样复制
        self.assertEqual(m.call_count, 1)
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
