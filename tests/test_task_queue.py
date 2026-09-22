"""test_task_queue.py — GUI 处理队列模型与顺序调度的单元测试。

不创建任何窗口、不依赖 PySide6 控件：直接用 gui/task_queue.py 的纯 Python 模型，
worker 用假对象 / mock 替代（模拟 PipelineWorker 的 start / cancel / isRunning 与
四个信号回调），因此可以安全覆盖「顺序处理、失败继续、取消停止、部分失败警告、
汇总统计、窗口关闭判断」等行为，也不会启动真实 waifu2x。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gui.task_queue import (
    REJECT_DUPLICATE, REJECT_MISSING, REJECT_NOT_EPUB, STATUS_CANCELLED,
    STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING, STATUS_SKIPPED,
    STATUS_SUCCEEDED, STATUS_WARNING, EpubQueue, QueueController, QueueItem,
    normalize_path,
)


class FakeWorker:
    """假的 PipelineWorker：只实现控制器会用到的接口，不启动任何线程。"""

    def __init__(self, item):
        self.item = item
        self.started = False
        self.cancel_requested = False
        self.running = True
        self.waited = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancel_requested = True

    def isRunning(self):
        return self.running

    def wait(self):
        self.waited = True
        self.running = False


class QueueTestBase(unittest.TestCase):
    """提供真实的 .epub 临时文件（队列只做 isfile 判断，不解析内容）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = {}
        for key, name in (("a", "a.epub"), ("b", "b.epub"), ("c", "c.epub")):
            path = self.root / name
            path.write_bytes(b"epub")
            self.path[key] = path

    def tearDown(self):
        self._tmp.cleanup()

    def _queue(self, keys):
        queue = EpubQueue()
        queue.add([self.path[key] for key in keys])
        return queue


class QueueAddTest(QueueTestBase):
    """添加 / 去重 / 过滤 / 顺序。"""

    def test_add_single_item(self):
        queue = EpubQueue()
        added, rejected = queue.add([self.path["a"]])
        self.assertEqual(rejected, [])
        self.assertEqual(len(queue), 1)
        item = added[0]
        self.assertEqual(item.epub_path, str(self.path["a"]))
        self.assertEqual(item.name, "a.epub")
        self.assertEqual(item.status, STATUS_PENDING)
        self.assertTrue(item.is_finished is False)
        self.assertTrue(item.can_remove)

    def test_add_multiple_keeps_user_order(self):
        queue = EpubQueue()
        added, _ = queue.add([self.path["c"], self.path["a"], self.path["b"]])
        self.assertEqual([item.name for item in added],
                         ["c.epub", "a.epub", "b.epub"])
        self.assertEqual([item.name for item in queue.items()],
                         ["c.epub", "a.epub", "b.epub"])

    def test_duplicate_path_rejected(self):
        queue = self._queue(["a", "b"])
        added, rejected = queue.add([self.path["a"]])
        self.assertEqual(added, [])
        self.assertEqual(rejected, [(str(self.path["a"]), REJECT_DUPLICATE)])
        self.assertEqual(len(queue), 2)

    def test_duplicate_detected_after_path_normalization(self):
        queue = EpubQueue()
        queue.add([self.path["a"]])
        sneaky = self.root / "sub" / ".." / "a.epub"
        self.assertEqual(normalize_path(sneaky), normalize_path(self.path["a"]))
        added, rejected = queue.add([sneaky])
        self.assertEqual(added, [])
        self.assertEqual(rejected[0][1], REJECT_DUPLICATE)
        self.assertEqual(len(queue), 1)

    def test_same_batch_duplicate_rejected(self):
        queue = EpubQueue()
        added, rejected = queue.add([self.path["a"], self.path["a"]])
        self.assertEqual(len(added), 1)
        self.assertEqual(rejected[0][1], REJECT_DUPLICATE)

    def test_uppercase_epub_suffix_accepted(self):
        path = self.root / "UPPER.EPUB"
        path.write_bytes(b"epub")
        queue = EpubQueue()
        added, rejected = queue.add([path])
        self.assertEqual(rejected, [])
        self.assertEqual(len(added), 1)

    def test_non_epub_rejected_with_reason(self):
        other = self.root / "cover.jpg"
        other.write_bytes(b"jpg")
        queue = EpubQueue()
        added, rejected = queue.add([other])
        self.assertEqual(added, [])
        self.assertEqual(rejected, [(str(other), REJECT_NOT_EPUB)])
        self.assertEqual(len(queue), 0)

    def test_missing_file_rejected_with_reason(self):
        missing = self.root / "gone.epub"
        queue = EpubQueue()
        added, rejected = queue.add([missing])
        self.assertEqual(added, [])
        self.assertEqual(rejected, [(str(missing), REJECT_MISSING)])

    def test_mixed_batch_reports_each_rejection(self):
        other = self.root / "x.png"
        other.write_bytes(b"png")
        missing = self.root / "gone.epub"
        queue = EpubQueue()
        added, rejected = queue.add(
            [self.path["a"], other, missing, self.path["a"]])
        self.assertEqual([item.name for item in added], ["a.epub"])
        self.assertEqual([reason for _, reason in rejected],
                         [REJECT_NOT_EPUB, REJECT_MISSING, REJECT_DUPLICATE])

    def test_can_add_again_after_remove(self):
        queue = self._queue(["a"])
        self.assertTrue(queue.remove(self.path["a"]))
        added, rejected = queue.add([self.path["a"]])
        self.assertEqual(rejected, [])
        self.assertEqual(len(added), 1)


class QueueEditTest(QueueTestBase):
    """删除 / 清空 / 下一项。"""

    def test_remove_pending_item(self):
        queue = self._queue(["a", "b"])
        self.assertTrue(queue.remove(self.path["a"]))
        self.assertEqual([item.name for item in queue.items()], ["b.epub"])
        self.assertFalse(queue.remove(self.path["a"]))       # 已经删掉了

    def test_cannot_remove_running_item(self):
        queue = self._queue(["a", "b"])
        queue.items()[0].status = STATUS_RUNNING
        self.assertFalse(queue.remove(self.path["a"]))
        self.assertEqual(len(queue), 2)

    def test_remove_finished_item(self):
        queue = self._queue(["a", "b"])
        queue.items()[0].status = STATUS_SUCCEEDED
        self.assertTrue(queue.remove(self.path["a"]))
        self.assertEqual([item.name for item in queue.items()], ["b.epub"])

    def test_remove_all_reports_refused_paths(self):
        queue = self._queue(["a", "b"])
        queue.items()[1].status = STATUS_RUNNING
        removed, refused = queue.remove_all([self.path["a"], self.path["b"]])
        self.assertEqual(removed, 1)
        self.assertEqual(refused, [str(self.path["b"])])

    def test_clear_pending_keeps_finished_and_running(self):
        queue = self._queue(["a", "b", "c"])
        items = queue.items()
        items[0].status = STATUS_SUCCEEDED
        items[1].status = STATUS_RUNNING
        cleared = queue.clear_pending()
        self.assertEqual(cleared, 1)                          # 只有 c 是待处理
        self.assertEqual([item.name for item in queue.items()],
                         ["a.epub", "b.epub"])

    def test_next_pending_follows_order_and_can_be_empty(self):
        queue = self._queue(["a", "b"])
        self.assertEqual(queue.next_pending().name, "a.epub")
        queue.items()[0].status = STATUS_FAILED
        self.assertEqual(queue.next_pending().name, "b.epub")
        queue.items()[1].status = STATUS_SKIPPED
        self.assertIsNone(queue.next_pending())

    def test_mark_pending_as_skipped_never_marks_success(self):
        queue = self._queue(["a", "b", "c"])
        queue.items()[0].status = STATUS_SUCCEEDED
        count = queue.mark_pending_as_skipped()
        self.assertEqual(count, 2)
        self.assertEqual(queue.succeeded_count(), 1)
        self.assertEqual(queue.unprocessed_count(), 2)
        self.assertEqual(queue.counts()[STATUS_SUCCEEDED], 1)
        self.assertEqual(queue.counts()[STATUS_SKIPPED], 2)


class QueueSummaryTest(QueueTestBase):
    """统计与展示字段。"""

    def test_counts_and_summary_lines(self):
        queue = self._queue(["a", "b", "c"])
        items = queue.items()
        items[0].status = STATUS_SUCCEEDED
        items[1].status = STATUS_FAILED
        self.assertEqual(queue.counts()[STATUS_SUCCEEDED], 1)
        self.assertEqual(queue.counts()[STATUS_FAILED], 1)
        self.assertEqual(queue.counts()[STATUS_PENDING], 1)
        summary = queue.summary()
        self.assertIn("成功：1 本", summary)
        self.assertIn("失败：1 本", summary)
        self.assertIn("取消：0 本", summary)
        self.assertIn("未处理：1 本", summary)

    def test_warning_counted_as_success_with_note(self):
        queue = self._queue(["a"])
        item = queue.items()[0]
        item.status = STATUS_WARNING
        item.failed_images = 2
        self.assertEqual(queue.succeeded_count(), 1)
        summary = queue.summary()
        self.assertIn("成功：1 本", summary)
        self.assertIn("1 本完成但有部分图片失败", summary)

    def test_display_detail_prefers_output_then_error(self):
        item = QueueItem(self.path["a"])
        item.status = STATUS_RUNNING
        item.progress_text = "正在放大：3/10 张"
        self.assertEqual(item.display_detail, "正在放大：3/10 张")
        item.error = "磁盘空间不足"
        self.assertEqual(item.display_detail, "磁盘空间不足")
        item.output_path = "out/a_upscaled.epub"
        self.assertEqual(item.display_detail, "out/a_upscaled.epub")
        item.status = STATUS_WARNING
        item.failed_images = 3
        self.assertEqual(item.display_detail, "out/a_upscaled.epub（3 张图片失败）")

    def test_status_labels_and_reset_for_rerun(self):
        item = QueueItem(self.path["a"])
        item.status = STATUS_CANCELLED
        self.assertEqual(item.status_label, "已取消")
        item.output_path = "x"
        item.error = "y"
        item.failed_images = 1
        item.reset_for_rerun()
        self.assertEqual(item.status, STATUS_PENDING)
        self.assertEqual(item.status_label, "待处理")
        self.assertEqual(item.output_path, "")
        self.assertEqual(item.error, "")
        self.assertEqual(item.failed_images, 0)

    def test_normalize_path_is_absolute_and_stable(self):
        normalized = normalize_path(self.path["a"])
        self.assertTrue(Path(normalized).is_absolute())
        self.assertEqual(normalized, normalize_path(self.path["a"]))



class ControllerTestBase(QueueTestBase):
    """准备控制器与假 worker，并记录回调次数/顺序。"""

    def setUp(self):
        super().setUp()
        self.workers = []
        self.disposed = []
        self.events = []          # 记录 create / dispose 的先后顺序
        self.changes = 0
        self.finished = []
        self.controller = None

    def _controller(self, factory=None):
        def default_factory(item):
            worker = FakeWorker(item)
            self.workers.append(worker)
            self.events.append(("create", item.name))
            return worker

        def dispose(worker):
            self.disposed.append(worker)
            self.events.append(("dispose", worker.item.name))
            worker.wait()

        self.controller = QueueController(
            factory or default_factory, dispose_worker=dispose,
            on_changed=self._on_changed, on_finished=self.finished.append)
        return self.controller

    def _on_changed(self):
        self.changes += 1

    def _enqueue(self, keys):
        added, rejected = self.controller.add([self.path[key] for key in keys])
        self.assertEqual(rejected, [])
        return added

    def _worker(self, index=-1):
        return self.workers[index]


class ControllerStartTest(ControllerTestBase):
    """启动行为：一次一本、不重复启动、每次新建 worker。"""

    def test_start_runs_only_first_item(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.assertTrue(self.controller.start())
        self.assertEqual(len(self.workers), 1)
        self.assertTrue(self._worker().started)
        items = self.controller.items
        self.assertEqual(items[0].status, STATUS_RUNNING)
        self.assertEqual(items[0].progress_text, "正在准备...")
        self.assertEqual(items[1].status, STATUS_PENDING)
        self.assertTrue(self.controller.is_running)
        self.assertEqual(self.controller.current_position(), (1, 2))

    def test_start_ignored_while_running(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.assertFalse(self.controller.start())      # 不允许重复点击「开始处理」
        self.assertEqual(len(self.workers), 1)

    def test_start_without_pending_returns_false(self):
        self._controller()
        self.assertFalse(self.controller.start())
        self.assertEqual(self.workers, [])
        self.assertEqual(self.finished, [])            # 什么都没跑，不报汇总

    def test_start_after_all_finished_without_pending_is_false(self):
        self._controller()
        self._enqueue(["a"])
        self.controller.start()
        self.controller.on_succeeded("out/a.epub", "超分 1 张")
        self.assertFalse(self.controller.is_running)
        self.assertFalse(self.controller.start())

    def test_progress_updates_item_and_notifies(self):
        self._controller()
        self._enqueue(["a"])
        self.controller.start()
        before = self.changes
        self.controller.on_progress("正在放大：3/10 张", 3, 10)
        item = self.controller.running_item
        self.assertEqual(item.progress_text, "正在放大：3/10 张")
        self.assertEqual((item.progress_done, item.progress_total), (3, 10))
        self.assertGreater(self.changes, before)

    def test_progress_without_running_item_is_ignored(self):
        self._controller()
        self._enqueue(["a"])
        self.controller.on_progress("不应崩溃", 1, 2)
        self.assertEqual(self.controller.items[0].progress_text, "")

    def test_new_worker_for_each_item_never_reused(self):
        self._controller()
        self._enqueue(["a", "b", "c"])
        self.controller.start()
        first = self._worker()
        self.controller.on_succeeded("out/a.epub", "")
        second = self._worker()
        self.assertIsNot(first, second)                 # 不复用已结束的 worker
        self.assertTrue(second.started)
        self.assertEqual(len(self.workers), 2)
        self.assertEqual(self.controller.current_position(), (2, 3))

    def test_dispose_called_before_next_worker_created(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.controller.on_succeeded("out/a.epub", "")
        self.assertEqual(self.events, [("create", "a.epub"),
                                       ("dispose", "a.epub"),
                                       ("create", "b.epub")])
        self.assertTrue(self.disposed[0].waited)        # 等线程真正结束再回收

    def test_current_position_without_running_item(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.assertEqual(self.controller.current_position(), (0, 2))

    def test_worker_factory_receives_the_queue_item(self):
        self._controller()
        added = self._enqueue(["a"])
        self.controller.start()
        self.assertIs(self._worker().item, added[0])



class ControllerSequenceTest(ControllerTestBase):
    """顺序推进：成功继续、失败继续、完成后汇总一次。"""

    def test_success_starts_next_item(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.controller.on_succeeded("out/a.epub", "超分 10 张，失败 0 张")
        items = self.controller.items
        self.assertEqual(items[0].status, STATUS_SUCCEEDED)
        self.assertEqual(items[0].output_path, "out/a.epub")
        self.assertEqual(items[0].summary, "超分 10 张，失败 0 张")
        self.assertEqual(items[1].status, STATUS_RUNNING)
        self.assertIs(self.controller.running_item, items[1])

    def test_failure_continues_with_next_item(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.controller.on_failed("处理失败：磁盘空间不足", "请清理磁盘后重试",
                                  "Traceback ...")
        items = self.controller.items
        self.assertEqual(items[0].status, STATUS_FAILED)
        self.assertEqual(items[0].error, "处理失败：磁盘空间不足")
        self.assertIn("Traceback", items[0].details)
        self.assertEqual(items[1].status, STATUS_RUNNING)
        self.assertEqual(len(self.workers), 2)           # 继续下一本

    def test_partial_failure_marks_warning_not_plain_success(self):
        self._controller()
        self._enqueue(["a"])
        self.controller.start()
        self.controller.on_succeeded("out/a.epub", "超分 8 张，失败 2 张",
                                     failed_images=2)
        item = self.controller.items[0]
        self.assertEqual(item.status, STATUS_WARNING)
        self.assertEqual(item.status_label, "完成（有警告）")
        self.assertIn("2 张图片失败", item.display_detail)
        self.assertIn("完成但有部分图片失败", self.finished[0])

    def test_all_success_finishes_queue_with_summary_once(self):
        self._controller()
        self._enqueue(["a", "b", "c"])
        self.controller.start()
        self.controller.on_succeeded("out/a.epub", "")
        self.controller.on_succeeded("out/b.epub", "")
        self.controller.on_succeeded("out/c.epub", "")
        self.assertFalse(self.controller.is_running)
        self.assertEqual(len(self.workers), 3)
        self.assertEqual(len(self.finished), 1)          # 只汇总一次
        self.assertIn("成功：3 本", self.finished[0])
        self.assertIn("未处理：0 本", self.finished[0])
        self.assertEqual(self.controller.current_position(), (0, 3))

    def test_terminal_callbacks_without_running_item_are_ignored(self):
        self._controller()
        self._enqueue(["a"])
        self.controller.on_succeeded("out/a.epub", "")
        self.controller.on_failed("boom")
        self.controller.on_cancelled("cache")
        self.assertEqual(self.controller.items[0].status, STATUS_PENDING)
        self.assertEqual(self.finished, [])

    def test_success_then_failure_summary_counts_both(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.controller.on_succeeded("out/a.epub", "")
        self.controller.on_failed("失败")
        summary = self.controller.summary()
        self.assertIn("成功：1 本", summary)
        self.assertIn("失败：1 本", summary)
        self.assertEqual(self.finished[-1], summary)



class ControllerCancelTest(ControllerTestBase):
    """取消当前 / 取消整队：不启动后续任务，未完成的项目不得记为成功。"""

    def test_cancel_current_stops_queue_and_keeps_remaining_pending(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.assertTrue(self.controller.cancel_current())
        self.assertTrue(self._worker().cancel_requested)     # 只取消当前 worker
        self.controller.on_cancelled("cache/a")              # worker 真正结束
        items = self.controller.items
        self.assertEqual(items[0].status, STATUS_CANCELLED)
        self.assertEqual(items[0].cache_path, "cache/a")
        self.assertEqual(items[1].status, STATUS_PENDING)    # 不自动开始下一本
        self.assertEqual(len(self.workers), 1)
        self.assertFalse(self.controller.is_running)
        self.assertEqual(len(self.finished), 1)
        self.assertIn("未处理：1 本", self.finished[0])
        self.assertIn("成功：0 本", self.finished[0])

    def test_cancel_current_returns_false_when_idle(self):
        self._controller()
        self._enqueue(["a"])
        self.assertFalse(self.controller.cancel_current())

    def test_cancel_all_cancels_running_and_marks_remaining_skipped(self):
        self._controller()
        self._enqueue(["a", "b", "c"])
        self.controller.start()
        self.assertEqual(self.controller.cancel_all(), 2)     # b/c 标记未处理
        self.assertTrue(self._worker().cancel_requested)
        items = self.controller.items
        self.assertEqual(items[0].status, STATUS_RUNNING)     # 等 worker 收尾
        self.assertEqual(items[1].status, STATUS_SKIPPED)
        self.assertEqual(items[2].status, STATUS_SKIPPED)
        self.controller.on_cancelled("")
        self.assertEqual(items[0].status, STATUS_CANCELLED)
        self.assertEqual(len(self.workers), 1)                # 没有后续任务
        self.assertEqual(self.controller.counts()[STATUS_SUCCEEDED], 0)
        self.assertIn("未处理：2 本", self.finished[-1])

    def test_cancel_all_when_idle_marks_pending_skipped(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.assertEqual(self.controller.cancel_all(), 2)
        self.assertEqual(self.workers, [])
        self.assertEqual(self.controller.counts()[STATUS_SKIPPED], 2)
        self.assertEqual(self.controller.counts()[STATUS_SUCCEEDED], 0)
        self.assertEqual(len(self.finished), 1)

    def test_cancel_after_first_book_failure_stops_the_rest(self):
        """第一本失败后仍在继续，此时取消当前：后续项目不再自动开始。"""
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.controller.on_failed("失败")
        self.assertEqual(len(self.workers), 2)                 # 已开始第二本
        self.controller.cancel_current()
        self.controller.on_cancelled("")
        self.assertEqual(self.controller.items[1].status, STATUS_CANCELLED)
        self.assertEqual(len(self.workers), 2)
        self.assertFalse(self.controller.is_running)

    def test_cancelled_item_is_never_counted_as_success(self):
        self._controller()
        self._enqueue(["a"])
        self.controller.start()
        self.controller.cancel_current()
        self.controller.on_cancelled("")
        summary = self.controller.summary()
        self.assertIn("成功：0 本", summary)
        self.assertIn("取消：1 本", summary)

    def test_remove_pending_allowed_but_running_item_protected(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.assertFalse(self.controller.remove(self.path["a"]))   # 正在处理
        self.assertTrue(self.controller.remove(self.path["b"]))    # 待处理可删
        self.assertEqual(len(self.controller.items), 1)



class CloseRequestTest(ControllerTestBase):
    """窗口关闭判断（不创建窗口，只测状态机）。"""

    def test_close_without_running_tasks(self):
        self._controller()
        self._enqueue(["a"])
        self.assertEqual(self.controller.request_close(), "close")

    def test_close_asks_then_waits_while_running(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.assertEqual(self.controller.request_close(), "ask")
        self.assertEqual(self.controller.request_close(), "wait")

    def test_decline_close_allows_asking_again(self):
        self._controller()
        self._enqueue(["a"])
        self.controller.start()
        self.assertEqual(self.controller.request_close(), "ask")
        self.controller.decline_close()
        self.assertEqual(self.controller.request_close(), "ask")

    def test_confirm_close_cancels_queue_and_waits_for_worker(self):
        self._controller()
        self._enqueue(["a", "b"])
        self.controller.start()
        self.assertEqual(self.controller.request_close(), "ask")
        self.assertEqual(self.controller.close_confirmed(), 1)   # b 标记未处理
        self.assertTrue(self._worker().cancel_requested)
        self.assertEqual(self.controller.request_close(), "wait")
        self.controller.on_cancelled("")                         # worker 真正结束
        self.assertEqual(self.controller.request_close(), "close")
        self.assertEqual(len(self.workers), 1)

    def test_close_when_idle_after_cancel_is_direct(self):
        self._controller()
        self._enqueue(["a"])
        self.controller.cancel_all()
        self.assertEqual(self.controller.request_close(), "close")


class MockWorkerTest(ControllerTestBase):
    """用 mock 模拟 PipelineWorker 的接口（信号由 GUI 转发，这里直接调回调）。"""

    def test_mock_pipeline_worker_started_once_per_item(self):
        self._controller()
        created = []

        def factory(item):
            worker = mock.Mock(spec=["start", "cancel", "isRunning", "wait"])
            worker.isRunning.return_value = True
            worker.item = item
            created.append(worker)
            return worker

        controller = QueueController(factory, on_finished=self.finished.append)
        controller.add([self.path["a"], self.path["b"]])
        self.assertTrue(controller.start())
        self.assertEqual(len(created), 1)
        created[0].start.assert_called_once_with()
        # 模拟 PipelineWorker 发出成功信号 -> GUI 转发给控制器
        controller.on_succeeded("out/a.epub", "超分 1 张", failed_images=0)
        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        # 模拟发出取消信号
        controller.on_cancelled("cache")
        self.assertEqual(len(created), 2)                        # 取消后不再新建
        self.assertEqual(controller.items[0].status, STATUS_SUCCEEDED)
        self.assertEqual(controller.items[1].status, STATUS_CANCELLED)

    def test_mock_worker_cancel_forwarded(self):
        self._controller()
        worker = mock.Mock(spec=["start", "cancel", "isRunning"])
        controller = QueueController(lambda item: worker)
        controller.add([self.path["a"], self.path["b"]])
        controller.start()
        controller.cancel_current()
        worker.cancel.assert_called_once_with()

    def test_queue_edits_notify_the_view(self):
        self._controller()
        self._enqueue(["a"])
        first = self.changes
        self.controller.remove(self.path["a"])
        self.assertGreater(self.changes, first)
        self._enqueue(["a", "b"])
        self.assertEqual(self.controller.clear_pending(), 2)
        self.assertEqual(len(self.controller.items), 0)

    def test_add_rejections_do_not_change_queue(self):
        self._controller()
        other = self.root / "cover.png"
        other.write_bytes(b"png")
        added, rejected = self.controller.add([other, self.root / "gone.epub"])
        self.assertEqual(added, [])
        self.assertEqual(len(rejected), 2)
        self.assertEqual(len(self.controller.items), 0)




class ClearQueueTest(QueueTestBase):
    """清空队列：clear_all（空闲）/ clear_pending（运行中）与可清除数量。"""

    def test_clear_all_removes_pending_items_and_returns_count(self):
        queue = self._queue(["a", "b", "c"])
        self.assertEqual(queue.pending_count(), 3)
        self.assertEqual(queue.removable_count(), 3)
        self.assertEqual(queue.clear_all(), 3)
        self.assertEqual(len(queue), 0)
        self.assertEqual(queue.pending_count(), 0)
        self.assertEqual(queue.removable_count(), 0)

    def test_clear_all_removes_history_items_too(self):
        queue = self._queue(["a", "b", "c"])
        items = queue.items()
        items[0].status = STATUS_SUCCEEDED
        items[1].status = STATUS_FAILED
        items[2].status = STATUS_CANCELLED
        self.assertEqual(queue.pending_count(), 0)
        self.assertEqual(queue.clear_all(), 3)      # 空闲时历史记录也能清掉
        self.assertEqual(len(queue), 0)

    def test_clear_all_keeps_running_item(self):
        queue = self._queue(["a", "b"])
        queue.items()[0].status = STATUS_RUNNING
        self.assertEqual(queue.removable_count(), 1)
        self.assertEqual(queue.clear_all(), 1)
        self.assertEqual([item.name for item in queue.items()], ["a.epub"])

    def test_clear_pending_only_removes_pending(self):
        queue = self._queue(["a", "b", "c"])
        items = queue.items()
        items[0].status = STATUS_SUCCEEDED
        items[1].status = STATUS_RUNNING
        self.assertEqual(queue.pending_count(), 1)
        self.assertEqual(queue.clear_pending(), 1)
        self.assertEqual([item.name for item in queue.items()],
                         ["a.epub", "b.epub"])

    def test_clear_when_empty_returns_zero(self):
        queue = EpubQueue()
        self.assertEqual(queue.clear_all(), 0)
        self.assertEqual(queue.clear_pending(), 0)
        self.assertEqual(queue.removable_count(), 0)
        self.assertEqual(queue.pending_count(), 0)

    def test_can_add_same_path_again_after_clear(self):
        queue = self._queue(["a"])
        self.assertEqual(queue.clear_all(), 1)
        added, rejected = queue.add([self.path["a"]])
        self.assertEqual(rejected, [])
        self.assertEqual(len(added), 1)


class ControllerClearTest(ControllerTestBase):
    """控制器的清空接口：空闲清全部、运行中只清待处理，并计算按钮可用性。"""

    def test_clear_when_idle_removes_everything(self):
        self._controller()
        self._enqueue(["a", "b", "c"])
        self.controller.items[0].status = STATUS_SUCCEEDED
        self.assertEqual(self.controller.clearable_count(), 3)
        self.assertEqual(self.controller.clear(), 3)
        self.assertEqual(len(self.controller.items), 0)

    def test_clear_with_nothing_to_clear_returns_zero(self):
        self._controller()
        self.assertEqual(self.controller.clearable_count(), 0)
        self.assertEqual(self.controller.clear(), 0)

    def test_clear_while_running_only_removes_pending(self):
        self._controller()
        self._enqueue(["a", "b", "c"])
        self.controller.start()                      # a 处理中
        self.assertEqual(self.controller.clearable_count(), 2)   # 只算待处理
        self.assertEqual(self.controller.clear(), 2)
        items = self.controller.items
        self.assertEqual(items[0].status, STATUS_RUNNING)        # 当前项目保留
        self.assertEqual(len(items), 1)
        self.assertTrue(self.controller.is_running)

    def test_cleared_pending_items_are_not_started_later(self):
        self._controller()
        self._enqueue(["a", "b", "c"])
        self.controller.start()
        self.controller.clear()
        self.controller.on_succeeded("out/a.epub", "")           # 当前这本结束
        self.assertEqual(len(self.workers), 1)                   # 被清掉的不再启动
        self.assertFalse(self.controller.is_running)
        self.assertEqual(len(self.controller.items), 1)

    def test_clearable_count_during_running_ignores_history(self):
        self._controller()
        self._enqueue(["a", "b", "c"])
        self.controller.items[0].status = STATUS_FAILED       # 一条历史记录
        self.controller.start()                               # b 处理中（a 已结束）
        self.assertEqual(self.controller.running_item.name, "b.epub")
        self.assertEqual(self.controller.clearable_count(), 1)   # 只算待处理的 c
        self.assertEqual(self.controller.clear_pending(), 1)
        # 历史记录与当前处理中的项目都保留
        self.assertEqual([item.name for item in self.controller.items],
                         ["a.epub", "b.epub"])

    def test_clear_notifies_view_once(self):
        self._controller()
        self._enqueue(["a", "b"])
        before = self.changes
        self.assertEqual(self.controller.clear(), 2)
        self.assertEqual(self.changes - before, 1)               # 只刷新一次

if __name__ == "__main__":
    unittest.main()

