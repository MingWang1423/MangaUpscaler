"""test_app_icon.py — 资源路径与窗口图标（resources/app.ico）测试。

两部分：
  - 路径解析（纯 Python）：源码运行 / PyInstaller onefile·onedir 模拟、与当前工作
    目录无关、文件缺失时安全降级；
  - 集成检查：仓库里确实有 resources/app.ico、spec 里 EXE(icon=...) 与 datas 都
    配好了、offscreen Qt 下 QIcon / 主窗口图标能真正加载（缺 Qt 时自动跳过）。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import resource_paths
from resource_paths import (
    APP_ICON_RELATIVE_PATH, RESOURCES_DIR, app_icon_path, resource_path,
    resource_root,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # Qt 部分无需显示环境

try:
    from PySide6.QtWidgets import QApplication
except Exception:                                        # pragma: no cover - 环境缺 Qt
    QApplication = None

if QApplication is not None:
    _APP = QApplication.instance() or QApplication([])
    from gui import main_window as mw
    QT_AVAILABLE = True
else:                                                    # pragma: no cover
    mw = None
    QT_AVAILABLE = False

SPEC_PATH = Path(__file__).resolve().parent.parent / "MangaUpscaler.spec"


class ResourcePathTest(unittest.TestCase):
    """resource_root / resource_path / app_icon_path 的行为。"""

    def test_constants_point_to_resources_folder(self):
        self.assertEqual(RESOURCES_DIR, "resources")
        self.assertEqual(APP_ICON_RELATIVE_PATH, "resources/app.ico")

    def test_resource_root_is_the_project_root_in_source_runs(self):
        # 源码运行：以 resource_paths.py 所在目录（项目根）为根
        self.assertEqual(resource_root(), Path(resource_paths.__file__).resolve().parent)
        self.assertTrue((resource_root() / "main.py").is_file())

    def test_app_icon_path_is_absolute_and_under_resources(self):
        path = app_icon_path()
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, "app.ico")
        self.assertEqual(path.parent.name, RESOURCES_DIR)
        self.assertEqual(path, resource_path("resources", "app.ico"))

    def test_repository_icon_exists_and_is_not_empty(self):
        path = app_icon_path()
        self.assertTrue(path.is_file(), f"缺少图标文件: {path}")
        self.assertGreater(path.stat().st_size, 0)

    def test_path_does_not_depend_on_current_directory(self):
        before = app_icon_path()
        with tempfile.TemporaryDirectory() as tmp:
            original = os.getcwd()
            os.chdir(tmp)
            try:
                after = app_icon_path()
            finally:
                os.chdir(original)
        self.assertEqual(before, after)

    def test_slash_and_backslash_are_equivalent(self):
        self.assertEqual(resource_path("resources/app.ico"),
                         resource_path("resources\\app.ico"))
        self.assertEqual(resource_path("./resources/app.ico"),
                         resource_path("resources/app.ico"))

    def test_missing_file_returns_path_without_raising(self):
        path = resource_path("resources/definitely-missing.ico")
        self.assertTrue(path.is_absolute())
        self.assertFalse(path.is_file())

    def test_frozen_onedir_and_onefile_use_meipass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sys, "frozen", True, create=True), \
                    mock.patch.object(sys, "_MEIPASS", tmp, create=True):
                self.assertEqual(resource_root(), Path(tmp))
                self.assertEqual(app_icon_path(),
                                 Path(tmp) / "resources" / "app.ico")

    def test_frozen_without_meipass_falls_back_to_executable_dir(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", None, create=True):
            expected = Path(sys.executable).resolve().parent
            self.assertEqual(resource_root(), expected)
            self.assertEqual(app_icon_path(),
                             expected / "resources" / "app.ico")


class SpecConfigurationTest(unittest.TestCase):
    """PyInstaller spec 里 exe 图标与资源打包都配好了。"""

    def setUp(self):
        self.text = SPEC_PATH.read_text(encoding="utf-8")

    def test_spec_exists(self):
        self.assertTrue(SPEC_PATH.is_file())

    def test_exe_icon_is_configured(self):
        self.assertIn('icon="resources/app.ico"', self.text)

    def test_icon_is_bundled_as_data_for_runtime(self):
        # 运行时窗口图标要能从包内的 resources/app.ico 读到
        self.assertIn('_APP_RESOURCES = ("resources/app.ico",)', self.text)
        self.assertIn("datas=_waifu2x_datas() + _app_datas()", self.text)

    def test_spec_icon_path_matches_the_helper_constant(self):
        self.assertIn(f'"{APP_ICON_RELATIVE_PATH}"', self.text)

    # --- 直接执行 spec 里的资源打包片段（无需安装 PyInstaller） ------------
    def _datas_snippet(self):
        """截取 spec 中 _APP_RESOURCES / _app_datas 两段定义，避开 Analysis/EXE。"""
        start = self.text.index("_APP_RESOURCES = ")
        end = self.text.index("a = Analysis(")
        return self.text[start:end]

    def _run_datas_snippet(self, snippet):
        namespace = {"Path": Path}
        exec(compile(snippet, "MangaUpscaler.spec", "exec"), namespace)  # noqa: S102
        return namespace["_app_datas"]()

    def test_app_datas_bundles_icon_into_resources_dir(self):
        # datas 的第二项是目标目录，因此最终落在 <包内>/resources/app.ico
        self.assertEqual(self._run_datas_snippet(self._datas_snippet()),
                         [("resources/app.ico", "resources")])

    def test_app_datas_fails_loudly_when_icon_is_missing(self):
        snippet = self._datas_snippet().replace('("resources/app.ico",)',
                                               '("resources/missing.ico",)')
        with self.assertRaises(SystemExit):
            self._run_datas_snippet(snippet)


@unittest.skipUnless(QT_AVAILABLE, "无法创建 Qt 应用（缺少 PySide6 或无显示环境）")
class WindowIconTest(unittest.TestCase):
    """QIcon / 主窗口图标的加载（offscreen，不显示窗口）。"""

    def test_load_app_icon_returns_a_valid_icon(self):
        icon = mw.load_app_icon()
        self.assertFalse(icon.isNull())
        self.assertTrue(icon.availableSizes())

    def test_main_window_uses_the_app_icon(self):
        window = mw.MainWindow({"output_dir": "out"})
        self.addCleanup(window.deleteLater)
        icon = window.windowIcon()
        self.assertFalse(icon.isNull())
        self.assertTrue(icon.availableSizes())

    def test_missing_icon_degrades_to_null_icon(self):
        missing = resource_path("resources/definitely-missing.ico")
        with mock.patch.object(mw, "app_icon_path", return_value=missing):
            icon = mw.load_app_icon()
        self.assertTrue(icon.isNull())          # 不抛异常，退回系统默认图标


if __name__ == "__main__":
    unittest.main()
