"""test_diagnostics.py — 输出目录探针清理与诊断结果汇总。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from diagnostics import check_output_dir, startup_checks


class CheckOutputDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_writable_dir_no_leftover(self):
        out = self.root / "out"
        ok, err = check_output_dir(str(out))
        self.assertTrue(ok, err)
        self.assertEqual(list(out.iterdir()), [])  # 无探针残留

    def test_existing_user_file_not_deleted(self):
        out = self.root / "out"
        out.mkdir()
        user_file = out / "keep.txt"
        user_file.write_text("keep", encoding="utf-8")
        ok, _ = check_output_dir(str(out))
        self.assertTrue(ok)
        self.assertTrue(user_file.exists())  # 用户文件保留
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["keep.txt"])

    def test_failure_cleans_probe(self):
        out = self.root / "out"
        out.mkdir()
        # write_text 抛异常：finally 仍会尝试清理探针，且不残留文件
        with mock.patch("pathlib.Path.write_text", side_effect=OSError("磁盘写入失败")):
            ok, _ = check_output_dir(str(out))
        self.assertFalse(ok)
        self.assertEqual(list(out.iterdir()), [])  # 无残留

    def test_failure_does_not_delete_dir(self):
        blocker = self.root / "blocker"
        blocker.write_text("x")
        ok, _ = check_output_dir(str(blocker / "sub"))
        self.assertFalse(ok)
        self.assertTrue(blocker.is_file())  # 目录/文件未被删除


class StartupChecksTest(unittest.TestCase):
    def test_multiple_issues_aggregated(self):
        with mock.patch("diagnostics.check_waifu2x_tool", return_value=["缺模型"]), \
             mock.patch("diagnostics.check_output_dir", return_value=(False, "目录不可写")):
            issues = startup_checks({"output_dir": "/bad"})
        self.assertEqual(len(issues), 2)
        titles = [i["title"] for i in issues]
        self.assertIn("缺少 waifu2x 组件", titles)
        self.assertIn("输出目录不可用", titles)


if __name__ == "__main__":
    unittest.main()
