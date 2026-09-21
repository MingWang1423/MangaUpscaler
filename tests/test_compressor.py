"""test_compressor.py — 验证 compress_folder 的三档缩放 + 同格式重编码。

覆盖：大/小 JPEG 的 4k/2k 缩放、带透明 PNG 的透明保持、original 档字节级
复制、目录结构保持、多帧 GIF 原样复制。
"""

import hashlib
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from upscaler.compressor import compress_folder


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CompressFolderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, quality):
        return compress_folder(self.input_dir, self.output_dir, quality=quality)

    def _make_jpeg(self, name, w, h):
        p = self.input_dir / name
        Image.new("RGB", (w, h), (200, 30, 30)).save(p, "JPEG")
        return p

    def test_large_jpeg_4k_resized(self):
        self._make_jpeg("big.jpg", 5000, 3000)
        self.assertEqual(self._run("4k"), (1, 0))
        with Image.open(self.output_dir / "big.jpg") as im:
            self.assertEqual(im.size, (3600, 2160))   # 横版双页：宽不再被压到 2160
            self.assertEqual(im.format, "JPEG")      # 仍是 JPEG，非 PNG 伪装

    def test_large_jpeg_2k_resized(self):
        self._make_jpeg("big.jpg", 5000, 3000)
        self.assertEqual(self._run("2k"), (1, 0))
        with Image.open(self.output_dir / "big.jpg") as im:
            self.assertEqual(im.size, (2560, 1536))   # 横版双页按 2560×1600 自适应
            self.assertEqual(im.format, "JPEG")

    def test_small_jpeg_4k_unchanged(self):
        self._make_jpeg("small.jpg", 1000, 800)
        self.assertEqual(self._run("4k"), (1, 0))
        with Image.open(self.output_dir / "small.jpg") as im:
            self.assertEqual(im.size, (1000, 800))   # 图在框内：不缩放
            self.assertEqual(im.format, "JPEG")

    def test_transparent_png_4k_keeps_alpha(self):
        p = self.input_dir / "trans.png"
        im = Image.new("RGBA", (5000, 3000), (0, 0, 0, 0))
        draw = ImageDraw.Draw(im)
        draw.rectangle([2500, 0, 4999, 2999], fill=(0, 255, 0, 255))  # 右半不透明
        im.save(p)
        self.assertEqual(self._run("4k"), (1, 0))
        with Image.open(self.output_dir / "trans.png") as out:
            self.assertEqual(out.format, "PNG")      # 仍是 PNG
            self.assertEqual(out.mode, "RGBA")       # 仍有 alpha 通道
            self.assertEqual(out.size, (3600, 2160))  # 横版：宽不再被压到 2160
            self.assertEqual(out.getchannel("A").getextrema(), (0, 255))  # 透明不丢

    def test_original_copies_bytes(self):
        p = self._make_jpeg("orig.jpg", 5000, 3000)
        self.assertEqual(self._run("original"), (1, 0))
        self.assertEqual(_sha256(p), _sha256(self.output_dir / "orig.jpg"))

    def test_directory_structure_preserved(self):
        sub = self.input_dir / "OEBPS" / "Images"
        sub.mkdir(parents=True)
        p = sub / "page.png"
        Image.new("RGB", (1000, 800), (10, 20, 30)).save(p)
        self.assertEqual(self._run("4k"), (1, 0))
        self.assertTrue((self.output_dir / "OEBPS" / "Images" / "page.png").is_file())

    def test_multi_frame_gif_copied(self):
        p = self.input_dir / "anim.gif"
        f1 = Image.new("RGB", (100, 100), (255, 0, 0))
        f2 = Image.new("RGB", (100, 100), (0, 255, 0))
        f1.save(p, save_all=True, append_images=[f2], format="GIF",
                duration=100, loop=0)
        self.assertEqual(self._run("4k"), (1, 0))
        self.assertEqual(_sha256(p), _sha256(self.output_dir / "anim.gif"))  # 原样复制
        with Image.open(self.output_dir / "anim.gif") as im:
            self.assertTrue(im.is_animated)


if __name__ == "__main__":
    unittest.main()
