"""test_builder.py — 验证 build_epub 的原子写入与 TaskWorkspace 的目录隔离。

覆盖：成功后临时文件被替换且无 .tmp.epub 残留；已有同名输出 + 新任务失败时
旧输出内容不变；两个任务使用不同缓存目录；清理只作用于本任务目录。
"""

import tempfile
import unittest
import zipfile
from pathlib import Path

from epub.builder import build_epub
from epub.reader import arcname_to_parts, build_local_names, extract_images
from workspace import TaskWorkspace


def _make_epub(path: Path, entries=None):
    """创建一个最小可用的 EPUB。entries 为 {arcname: bytes}。"""
    with zipfile.ZipFile(path, "w") as zf:
        mimetype = zipfile.ZipInfo("mimetype")
        mimetype.compress_type = zipfile.ZIP_STORED
        zf.writestr(mimetype, b"application/epub+zip")
        for name, data in (entries or {}).items():
            zf.writestr(name, data)
    return path


def _temp_leftovers(output_dir: Path):
    """返回 output_dir 下残留的 .tmp.epub 临时文件名列表。"""
    return [p.name for p in output_dir.glob("*.tmp.epub")]


class BuildEpubAtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_success_replaces_and_leaves_no_temp(self):
        src = self.root / "book.epub"
        _make_epub(src, {"OEBPS/Images/cover.jpg": b"original-image"})

        upscaled = self.root / "upscaled"
        target = upscaled / "OEBPS" / "Images" / "cover.jpg"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"upscaled-image")

        out_dir = self.root / "out"
        out_dir.mkdir()
        final = out_dir / "book_upscaled.epub"

        result = build_epub(str(src), str(upscaled), output_epub_path=str(final))

        self.assertEqual(Path(result), final.resolve())
        self.assertTrue(final.is_file())
        self.assertEqual(_temp_leftovers(out_dir), [])
        with zipfile.ZipFile(final) as zf:
            self.assertEqual(zf.read("OEBPS/Images/cover.jpg"), b"upscaled-image")

    def test_failure_keeps_existing_output_unchanged(self):
        src = self.root / "book.epub"
        _make_epub(src, {"OEBPS/Images/cover.jpg": b"original-image"})

        upscaled = self.root / "upscaled"
        upscaled.mkdir()

        out_dir = self.root / "out"
        out_dir.mkdir()
        final = out_dir / "book_upscaled.epub"
        old_bytes = b"previous-successful-output"
        final.write_bytes(old_bytes)

        with self.assertRaises(InterruptedError):
            build_epub(
                str(src), str(upscaled), output_epub_path=str(final),
                cancel_check=lambda: True,
            )

        # 旧输出原样保留，且没有残留临时文件
        self.assertEqual(final.read_bytes(), old_bytes)
        self.assertEqual(_temp_leftovers(out_dir), [])


class TaskWorkspaceTest(unittest.TestCase):
    def test_two_tasks_use_distinct_workspaces(self):
        a = TaskWorkspace()
        b = TaskWorkspace()
        try:
            self.assertNotEqual(a.root, b.root)
            for ws in (a, b):
                self.assertTrue((ws.root / "extracted").is_dir())
                self.assertTrue((ws.root / "upscaled").is_dir())
                self.assertTrue((ws.root / "compressed").is_dir())
        finally:
            a.cleanup()
            b.cleanup()
        self.assertFalse(a.root.exists())
        self.assertFalse(b.root.exists())

    def test_cleanup_only_removes_own_root(self):
        a = TaskWorkspace()
        b = TaskWorkspace()
        marker = b.root / "marker.txt"
        marker.write_text("x")
        a.cleanup()
        try:
            self.assertFalse(a.root.exists())
            self.assertTrue(b.root.exists())
            self.assertTrue(marker.exists())
        finally:
            b.cleanup()

    def test_keeps_cache_when_not_cleaned(self):
        ws = TaskWorkspace()
        root = ws.root
        # 失败/取消时不调用 cleanup，缓存目录应保留下来供排错
        self.assertTrue(root.exists())
        self.assertTrue((root / "extracted").is_dir())
        ws.cleanup()
        self.assertFalse(root.exists())


class EpubBuilderFormatTest(unittest.TestCase):
    """mimetype 条目、嵌套目录替换、非 ZIP 输入。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _upscaled_with(self, rel, data):
        upscaled = self.root / "upscaled"
        target = upscaled / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return upscaled

    def _build(self, src, upscaled):
        out = self.root / "out" / "book_upscaled.epub"
        out.parent.mkdir()
        build_epub(str(src), str(upscaled), output_epub_path=str(out))
        return out

    def test_mimetype_is_first_and_stored(self):
        src = self.root / "book.epub"
        _make_epub(src, {"OEBPS/Images/cover.jpg": b"img"})
        upscaled = self._upscaled_with("OEBPS/Images/cover.jpg", b"img")
        out = self._build(src, upscaled)
        with zipfile.ZipFile(out) as zf:
            infos = zf.infolist()
            self.assertEqual(infos[0].filename, "mimetype")
            self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)

    def test_missing_mimetype_is_patched(self):
        src = self.root / "book.epub"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("OEBPS/Images/cover.jpg", b"img")
        upscaled = self._upscaled_with("OEBPS/Images/cover.jpg", b"img")
        out = self._build(src, upscaled)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.read("mimetype"), b"application/epub+zip")

    def test_nested_directory_image_replaced(self):
        src = self.root / "book.epub"
        _make_epub(src, {"OEBPS/Images/ch1/p01.jpg": b"old"})
        upscaled = self._upscaled_with("OEBPS/Images/ch1/p01.jpg", b"new")
        out = self._build(src, upscaled)
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.read("OEBPS/Images/ch1/p01.jpg"), b"new")

    def test_non_zip_source_raises(self):
        bad = self.root / "bad.epub"
        bad.write_text("not a zip", encoding="utf-8")
        upscaled = self.root / "upscaled"
        upscaled.mkdir()
        with self.assertRaises(zipfile.BadZipFile):
            build_epub(str(bad), str(upscaled),
                       output_epub_path=str(self.root / "out" / "x.epub"))


class EpubReaderPathTest(unittest.TestCase):
    """路径安全与跨目录 / 大小写映射。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_dangerous_paths_rejected(self):
        for bad in ("../evil.jpg", "..\\evil.jpg", "/abs.jpg",
                    "C:/evil.jpg", "C:\\evil.jpg", ""):
            self.assertIsNone(arcname_to_parts(bad))

    def test_normal_path_accepted(self):
        self.assertEqual(arcname_to_parts("OEBPS/Images/a.jpg"),
                         ["OEBPS", "Images", "a.jpg"])

    def test_cross_dir_same_name_kept_separate(self):
        mapping = build_local_names(["a/page.jpg", "b/page.jpg"])
        self.assertEqual(mapping["a/page.jpg"].as_posix(), "a/page.jpg")
        self.assertEqual(mapping["b/page.jpg"].as_posix(), "b/page.jpg")
        self.assertNotEqual(mapping["a/page.jpg"], mapping["b/page.jpg"])

    def test_case_collision_not_overwritten(self):
        mapping = build_local_names(["Cover.jpg", "cover.jpg"])
        self.assertNotEqual(mapping["Cover.jpg"], mapping["cover.jpg"])
        self.assertEqual(mapping["Cover.jpg"].as_posix(), "Cover.jpg")
        self.assertEqual(mapping["cover.jpg"].as_posix(), "cover__2.jpg")

    def test_non_zip_extract_raises(self):
        bad = self.root / "bad.epub"
        bad.write_text("not a zip", encoding="utf-8")
        with self.assertRaises(zipfile.BadZipFile):
            extract_images(str(bad), str(self.root / "out"))


if __name__ == "__main__":
    unittest.main()
