"""test_queue_gui.py — 「清空队列」按钮的界面行为测试。

使用 offscreen Qt（不显示窗口、不启动事件循环）与假的 PipelineWorker / 信号回调，
不运行真实 waifu2x。覆盖：空闲时清全部、运行中只清待处理、表格与按钮状态刷新、
没有可清除项目时的提示、单击只清一次、不删除输入/输出文件。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 无显示环境也能跑

try:
    from PySide6.QtCore import QObject, Signal
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


class FakeWorker(QObject):
    """假的 PipelineWorker：只提供信号与 start/cancel 接口，不启动线程。"""

    progress = Signal(str, int, int)
    succeeded = Signal(str, str)
    cancelled = Signal(str)
    failed = Signal(str, str, str)

    def __init__(self, item):
        super().__init__()
        self.item = item
        self.failure_count = 0
        self.started = False
        self.cancel_requested = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancel_requested = True

    def isRunning(self):
        return False

    def wait(self):
        pass

    def deleteLater(self):
        pass


class FakeBox:
    """替换 QMessageBox：记录提示内容，绝不弹模态窗口。"""

    Critical = Warning = Information = 0
    ActionRole = RejectRole = 0
    Yes = 1
    No = 0

    messages = []
    buttons = []

    def __init__(self, *args, **kwargs):
        self.text = ""

    @classmethod
    def reset(cls):
        cls.messages = []
        cls.buttons = []

    def setIcon(self, *args):
        pass

    def setWindowTitle(self, title):
        pass

    def setText(self, text):
        self.text = text

    def setInformativeText(self, text):
        pass

    def addButton(self, label, role=None):
        FakeBox.buttons.append(label)
        return label

    def exec(self):
        return 0

    def clickedButton(self):
        return None

    @staticmethod
    def information(parent, title, text, *args):
        FakeBox.messages.append((title, text))
        return 0

    @staticmethod
    def question(*args, **kwargs):
        return FakeBox.No

    @staticmethod
    def warning(*args, **kwargs):
        return 0

    @staticmethod
    def critical(*args, **kwargs):
        return 0


@unittest.skipUnless(QT_AVAILABLE, "无法创建 Qt 应用（缺少 PySide6 或无显示环境）")
class ClearQueueGuiTest(unittest.TestCase):
    """清空队列的界面行为（窗口从不 show()，只验证状态与刷新）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = {}
        for name in ("a.epub", "b.epub", "c.epub"):
            path = self.root / name
            path.write_bytes(b"epub")
            self.paths[name[0]] = path
        FakeBox.reset()
        patcher = mock.patch("gui.main_window.QMessageBox", FakeBox)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.workers = []
        self.window = mw.MainWindow({"scale": 2, "noise": 3, "quality": "4k",
                                     "gpu": "auto",
                                     "output_dir": str(self.root / "out")})
        self.addCleanup(self.window.deleteLater)
        # 用假 worker 替换真实 PipelineWorker：不启动线程、不跑 waifu2x
        self.window._create_worker = self._fake_create_worker
        self.window.controller._factory = self._fake_create_worker

    # --- 辅助 -------------------------------------------------------------
    def _fake_create_worker(self, item):
        worker = FakeWorker(item)
        self.workers.append(worker)
        worker.progress.connect(self.window._on_worker_progress)
        worker.succeeded.connect(self.window._on_worker_succeeded)
        worker.cancelled.connect(self.window._on_worker_cancelled)
        worker.failed.connect(self.window._on_worker_failed)
        return worker

    def _enqueue(self, keys=("a", "b", "c")):
        added, rejected = self.window.controller.add(
            [str(self.paths[key]) for key in keys])
        self.assertEqual(rejected, [])
        return added

    def _rows(self):
        return [self.window.queue_table.item(row, 0).text()
                for row in range(self.window.queue_table.rowCount())]

    def _statuses(self):
        return [self.window.queue_table.item(row, 1).text()
                for row in range(self.window.queue_table.rowCount())]

    def _finish_success(self, name):
        self.window._on_worker_succeeded(str(self.root / "out" / name), "失败 0 张")

    def _click_clear(self):
        self.window.clear_button.click()

    # --- 空闲时：清空全部 -------------------------------------------------
    def test_clear_button_disabled_when_queue_is_empty(self):
        self.assertFalse(self.window.clear_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled())

    def test_clear_removes_all_pending_rows_and_refreshes_view(self):
        self._enqueue()
        self.assertEqual(len(self._rows()), 3)
        self.assertTrue(self.window.clear_button.isEnabled())

        self._click_clear()

        self.assertEqual(len(self.window.controller.items), 0)
        self.assertEqual(self._rows(), [])                     # 表格已刷新
        self.assertIn("队列为空", self.window.queue_label.text())
        self.assertFalse(self.window.start_button.isEnabled())  # 开始处理变灰
        self.assertFalse(self.window.clear_button.isEnabled())  # 没有可清除的项目
        self.assertEqual(self.window.status_label.text(), "")   # 当前任务标签重置

    def test_clear_removes_finished_history_rows(self):
        self._enqueue(("a", "b"))
        self.window.controller.start()
        self._finish_success("a.epub")
        self._finish_success("b.epub")
        self.assertEqual(self._statuses(), ["成功", "成功"])
        self.assertFalse(self.window.controller.is_running)

        self._click_clear()

        self.assertEqual(self._rows(), [])
        self.assertEqual(len(self.window.controller.items), 0)
        self.assertIn("队列为空", self.window.queue_label.text())

    def test_clear_clears_selection(self):
        self._enqueue()
        self.window.queue_table.selectAll()
        self.assertTrue(self.window.queue_table.selectionModel().selectedRows())
        self._click_clear()
        self.assertEqual(self.window.queue_table.selectionModel().selectedRows(), [])

    def test_clear_does_not_delete_input_or_output_files(self):
        output_dir = self.root / "out"
        output_dir.mkdir()
        produced = output_dir / "a_upscaled.epub"
        produced.write_bytes(b"result")
        before = {path: path.read_bytes() for path in self.paths.values()}
        before[produced] = b"result"

        self._enqueue()
        self._click_clear()

        for path, content in before.items():
            self.assertTrue(path.exists(), str(path))
            self.assertEqual(path.read_bytes(), content)

    def test_single_click_clears_once(self):
        self._enqueue()
        with mock.patch.object(self.window.controller, "clear",
                               wraps=self.window.controller.clear) as spy:
            self._click_clear()
        self.assertEqual(spy.call_count, 1)                    # 只连一次、只执行一次
        self.assertEqual(len(self.window.controller.items), 0)

    def test_clear_with_nothing_to_clear_shows_message(self):
        # 没有可清除项目时按钮就被禁用（不会静默表现成「按钮坏了」）
        self.assertFalse(self.window.clear_button.isEnabled())
        # 即使被程序调用（例如状态刚变化还没刷新），也给出明确提示
        self.window._on_clear_queue()
        self.assertIn(("提示", "没有可清除的任务。"), FakeBox.messages)

    def test_start_button_state_follows_pending_items(self):
        self._enqueue(("a",))
        self.assertTrue(self.window.start_button.isEnabled())
        self._click_clear()
        self.assertFalse(self.window.start_button.isEnabled())
        self._enqueue(("a",))
        self.assertTrue(self.window.start_button.isEnabled())


    # --- 运行中：只清待处理，当前任务不受影响 -----------------------------
    def test_clear_while_running_keeps_running_item(self):
        self._enqueue()
        self.window.controller.start()
        self.assertEqual(self._rows(), ["a.epub", "b.epub", "c.epub"])
        self.assertEqual(self._statuses()[0], "处理中")
        self.assertTrue(self.window.clear_button.isEnabled())   # 运行中也可清

        self._click_clear()

        self.assertEqual(self._rows(), ["a.epub"])              # 只留处理中的
        self.assertEqual(self._statuses(), ["处理中"])
        self.assertTrue(self.window.controller.is_running)       # 当前任务继续
        self.assertFalse(self.window.start_button.isEnabled())   # 不重复开始
        self.assertFalse(self.window.clear_button.isEnabled())   # 已无可清除项目
        self.assertTrue(self.window.cancel_current_button.isEnabled())

    def test_cleared_pending_items_never_start_later(self):
        self._enqueue()
        self.window.controller.start()
        self._click_clear()
        self._finish_success("a.epub")                           # 当前这本完成
        self.assertEqual(len(self.workers), 1)                   # 被清掉的不再启动
        self.assertFalse(self.window.controller.is_running)
        self.assertEqual(self._rows(), ["a.epub"])

    def test_clear_while_running_keeps_history_rows(self):
        self._enqueue()
        self.window.controller.start()
        self._finish_success("a.epub")                           # a 成功、b 处理中
        self.assertEqual(self._statuses(), ["成功", "处理中", "待处理"])

        self._click_clear()

        self.assertEqual(self._rows(), ["a.epub", "b.epub"])     # 历史与当前都保留
        self.assertEqual(self._statuses(), ["成功", "处理中"])
        self.assertIn("已完成 1 本", self.window.queue_label.text())

