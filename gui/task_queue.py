"""GUI 处理队列的模型与顺序调度（不依赖 PySide6，便于单元测试）。

分两层：
  - EpubQueue：纯数据队列，负责添加 / 去重 / 过滤 / 删除 / 清空 / 统计；
  - QueueController：顺序调度——一次只跑一本 EPUB，上一本结束（成功 / 失败 /
    取消）之后才通过注入的 worker_factory 创建下一个 worker（每次都新建，
    绝不复用已结束的 QThread）。

GUI 侧只做三件事：把 worker 的信号转发给 controller 的 on_* 方法、按 controller
的状态刷新界面、提供 worker_factory 与 dispose_worker 两个注入点，所以本模块
完全不 import Qt，测试也不需要创建窗口。
"""

import os
from pathlib import Path

# 队列项目状态
STATUS_PENDING = "pending"        # 待处理
STATUS_RUNNING = "running"        # 处理中
STATUS_SUCCEEDED = "succeeded"    # 成功
STATUS_WARNING = "warning"        # 完成但有部分图片失败（警告，不算完全成功）
STATUS_FAILED = "failed"          # 失败
STATUS_CANCELLED = "cancelled"    # 已取消（当前项目）
STATUS_SKIPPED = "skipped"        # 未处理（整队停止时剩下的项目）

STATUS_LABELS = {
    STATUS_PENDING: "待处理",
    STATUS_RUNNING: "处理中",
    STATUS_SUCCEEDED: "成功",
    STATUS_WARNING: "完成（有警告）",
    STATUS_FAILED: "失败",
    STATUS_CANCELLED: "已取消",
    STATUS_SKIPPED: "未处理",
}

# 终态：不会再被调度
TERMINAL_STATUSES = frozenset({
    STATUS_SUCCEEDED, STATUS_WARNING, STATUS_FAILED, STATUS_CANCELLED, STATUS_SKIPPED,
})
# 成功类状态（含「完成但部分失败」的警告）
SUCCESS_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_WARNING})
# 未处理类状态（从未真正开始过）
UNPROCESSED_STATUSES = frozenset({STATUS_PENDING, STATUS_SKIPPED})

EPUB_SUFFIX = ".epub"

# 添加被拒的原因
REJECT_NOT_EPUB = "not_epub"
REJECT_DUPLICATE = "duplicate"
REJECT_MISSING = "missing"
REJECT_LABELS = {
    REJECT_NOT_EPUB: "不是 EPUB 文件",
    REJECT_DUPLICATE: "已在队列中",
    REJECT_MISSING: "文件不存在",
}


def normalize_path(path) -> str:
    """把路径规范成「同一文件只有一个键」：相对路径、大小写、上跳都归一。"""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


class QueueItem:
    """队列中的一个 EPUB 任务（纯数据，界面只读它）。"""

    def __init__(self, epub_path):
        self.epub_path = os.fspath(epub_path)
        self.key = normalize_path(self.epub_path)
        self.status = STATUS_PENDING
        self.output_path = ""       # 成功后的输出 EPUB 路径
        self.error = ""             # 简短错误摘要（列表显示）
        self.details = ""           # 详细错误（供「复制错误详情」）
        self.summary = ""           # 统计摘要
        self.progress_text = ""     # 当前处理文本（阶段 + 当前图片进度）
        self.progress_done = 0
        self.progress_total = 0
        self.cache_path = ""        # 取消/失败时保留的缓存目录
        self.failed_images = 0      # 完成但有 N 张图片失败

    @property
    def name(self):
        return Path(self.epub_path).name

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_running(self):
        return self.status == STATUS_RUNNING

    @property
    def is_finished(self):
        return self.status in TERMINAL_STATUSES

    @property
    def can_remove(self):
        """正在处理的项目不能删除；待处理与已结束的都可以删除。"""
        return self.status != STATUS_RUNNING

    @property
    def display_detail(self):
        """列表第三列的内容：优先输出路径，其次错误摘要，最后摘要/进度。"""
        if self.output_path:
            if self.status in SUCCESS_STATUSES and self.failed_images:
                return "%s（%d 张图片失败）" % (self.output_path, self.failed_images)
            return self.output_path
        return self.error or self.summary or self.progress_text

    def reset_for_rerun(self):
        """把项目恢复成待处理（重新加入同一路径时用）。"""
        self.status = STATUS_PENDING
        self.output_path = ""
        self.error = ""
        self.details = ""
        self.summary = ""
        self.progress_text = ""
        self.progress_done = 0
        self.progress_total = 0
        self.cache_path = ""
        self.failed_images = 0


class EpubQueue:
    """EPUB 处理队列：保持用户添加顺序，同一路径只允许出现一次。"""

    def __init__(self):
        self._items = []

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def items(self):
        """按添加顺序返回全部项目（元组，避免外部就地修改）。"""
        return tuple(self._items)

    def keys(self):
        return {item.key for item in self._items}

    def index_of(self, path):
        """返回项目在队列中的位置；不存在返回 -1。"""
        key = normalize_path(path)
        for index, item in enumerate(self._items):
            if item.key == key:
                return index
        return -1

    def add(self, paths, exists=None):
        """批量添加；返回 (新增的项目列表, [(路径, 被拒原因), ...])。

        过滤规则：非 .epub、重复路径、不存在的文件。保持用户给定顺序，
        同一批里的重复也会被识别（第二个相同路径算重复）。
        exists 可注入（默认 os.path.isfile），便于测试伪造文件系统。
        """
        exists = exists or os.path.isfile
        added, rejected = [], []
        for raw in paths:
            path = os.fspath(raw)
            if not path:
                continue
            if Path(path).suffix.lower() != EPUB_SUFFIX:
                rejected.append((path, REJECT_NOT_EPUB))
            elif normalize_path(path) in self.keys():
                rejected.append((path, REJECT_DUPLICATE))
            elif not exists(path):
                rejected.append((path, REJECT_MISSING))
            else:
                item = QueueItem(path)
                self._items.append(item)
                added.append(item)
        return added, rejected

    def remove(self, path) -> bool:
        """删除指定项目；正在处理的项目不允许删除（返回 False）。"""
        index = self.index_of(path)
        if index < 0 or not self._items[index].can_remove:
            return False
        del self._items[index]
        return True

    def remove_all(self, paths):
        """批量删除；返回 (删除数量, 被拒绝的路径列表)。"""
        removed, refused = 0, []
        for path in paths:
            if self.remove(path):
                removed += 1
            else:
                refused.append(os.fspath(path))
        return removed, refused

    def pending_count(self) -> int:
        """「待处理」项目数量（运行中可被清除的就是这些）。"""
        return sum(1 for item in self._items if item.status == STATUS_PENDING)

    def removable_count(self) -> int:
        """当前可删除的项目数量（处理中的项目不可删除，不计入）。"""
        return sum(1 for item in self._items if item.can_remove)

    def clear_pending(self) -> int:
        """只清空「待处理」项目，返回删除数量；处理中/已结束的项目不动。

        运行中用它：已完成的记录要留给用户看，当前处理中的项目更要保留。
        """
        before = len(self._items)
        self._items = [item for item in self._items
                       if item.status != STATUS_PENDING]
        return before - len(self._items)

    def clear_all(self) -> int:
        """清空全部可删除项目（含已完成/失败/已取消/未处理的历史记录）。

        空闲时用它；万一还有任务在跑，那一个（can_remove 为 False）也会被保留。
        返回实际删除数量。
        """
        before = len(self._items)
        self._items = [item for item in self._items if not item.can_remove]
        return before - len(self._items)

    def next_pending(self):
        """按添加顺序返回下一个待处理项目；没有则返回 None。"""
        for item in self._items:
            if item.status == STATUS_PENDING:
                return item
        return None

    def mark_pending_as_skipped(self) -> int:
        """把剩余「待处理」项目标记为未处理（整队取消时用），绝不标记为成功。"""
        count = 0
        for item in self._items:
            if item.status == STATUS_PENDING:
                item.status = STATUS_SKIPPED
                item.progress_text = ""
                count += 1
        return count

    def counts(self) -> dict:
        """各状态的项目数量。"""
        counts = {status: 0 for status in STATUS_LABELS}
        for item in self._items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def succeeded_count(self) -> int:
        """成功本数（含「完成但有警告」）。"""
        counts = self.counts()
        return counts[STATUS_SUCCEEDED] + counts[STATUS_WARNING]

    def unprocessed_count(self) -> int:
        """还没有真正开始过的本数。"""
        counts = self.counts()
        return counts[STATUS_PENDING] + counts[STATUS_SKIPPED]

    def summary(self) -> str:
        """整队汇总：成功 / 失败 / 取消 / 未处理（另附警告本数）。"""
        counts = self.counts()
        lines = [
            "成功：%d 本" % self.succeeded_count(),
            "失败：%d 本" % counts[STATUS_FAILED],
            "取消：%d 本" % counts[STATUS_CANCELLED],
            "未处理：%d 本" % self.unprocessed_count(),
        ]
        if counts[STATUS_WARNING]:
            lines.append("（其中 %d 本完成但有部分图片失败）" % counts[STATUS_WARNING])
        return "\n".join(lines)



class QueueController:
    """顺序调度队列：一次只跑一本 EPUB，上一本结束后才创建下一个 worker。

    注入点（GUI 与测试都通过它们接入，因此本模块不认识 Qt）：
      worker_factory(item) -> worker：必须提供 start() / cancel()；
      dispose_worker(worker)：可选，回收已结束的 worker（GUI 里 wait + deleteLater）；
      on_changed()：队列或进度有变化时回调（GUI 刷新列表、按钮、进度）；
      on_finished(summary)：整队停止（全部完成 / 被取消）时回调一次（GUI 显示汇总）。

    GUI 把 worker 的 success/failure/cancel/progress 信号转发到这里的 on_* 方法，
    这些方法都在主线程执行，因此控制流不会跨线程。
    """

    def __init__(self, worker_factory, dispose_worker=None, on_changed=None,
                 on_finished=None):
        self._queue = EpubQueue()
        self._factory = worker_factory
        self._dispose = dispose_worker
        self._on_changed = on_changed or (lambda: None)
        self._on_finished = on_finished or (lambda summary: None)
        self._worker = None
        self._current = None
        self._stopping = False
        self._close_requested = False

    # --- 只读状态 ---------------------------------------------------------
    @property
    def queue(self):
        return self._queue

    @property
    def items(self):
        return self._queue.items()

    @property
    def running_item(self):
        """当前正在处理的项目；没有在跑时为 None。"""
        return self._current

    @property
    def running_worker(self):
        return self._worker

    @property
    def is_running(self):
        return self._current is not None

    def current_position(self):
        """返回 (第几本, 共几本)；没有在处理中的项目时第几本为 0。"""
        total = len(self._queue)
        if self._current is None:
            return (0, total)
        index = self._queue.index_of(self._current.epub_path)
        return (index + 1 if index >= 0 else 0, total)

    def summary(self):
        return self._queue.summary()

    def counts(self):
        return self._queue.counts()

    # --- 队列编辑（转发给模型，并通知界面刷新） ---------------------------
    def add(self, paths, exists=None):
        added, rejected = self._queue.add(paths, exists=exists)
        if added or rejected:
            self._on_changed()
        return added, rejected

    def remove(self, path) -> bool:
        removed = self._queue.remove(path)
        if removed:
            self._on_changed()
        return removed

    def remove_all(self, paths):
        removed, refused = self._queue.remove_all(paths)
        if removed:
            self._on_changed()
        return removed, refused

    def clear_pending(self) -> int:
        """只清空「待处理」项目（运行中用它）；返回实际清除数量。"""
        cleared = self._queue.clear_pending()
        if cleared:
            self._on_changed()
        return cleared

    def clear(self) -> int:
        """清空队列：空闲时清除全部项目，运行中只清除「待处理」项目。

        返回实际清除数量（0 表示没有可清除的项目，界面据此给出提示而不是静默）。
        """
        cleared = (self._queue.clear_pending() if self.is_running
                   else self._queue.clear_all())
        if cleared:
            self._on_changed()
        return cleared

    def clearable_count(self) -> int:
        """当前「清空队列」真正能清掉的项目数（运行中只算待处理项目）。

        界面据此计算清空按钮的启用状态——不因为处于运行状态就无条件禁用。
        """
        if self.is_running:
            return self._queue.pending_count()
        return self._queue.removable_count()

    # --- 启动与取消 -------------------------------------------------------
    def start(self) -> bool:
        """开始顺序处理；已在运行或没有待处理项目时返回 False（不重复启动）。"""
        if self.is_running:
            return False
        if self._queue.next_pending() is None:
            return False
        self._stopping = False
        self._start_next()
        return True

    def cancel_current(self) -> bool:
        """只取消正在运行的 worker：不启动下一本，剩余项目保持待处理。"""
        worker = self._worker
        if worker is None or not self.is_running:
            return False
        self._stopping = True
        worker.cancel()
        return True

    def cancel_all(self) -> int:
        """取消整队：先取消当前任务，剩余待处理项目标记为未处理。

        返回被标记为未处理的项目数量。绝不把未处理项目记成成功。
        """
        self._stopping = True
        skipped = self._queue.mark_pending_as_skipped()
        worker = self._worker
        if worker is not None and self.is_running:
            worker.cancel()          # 终态由 on_cancelled 收尾
        else:
            self._on_changed()
            self._finish_queue()
        return skipped

    # --- 窗口关闭 ---------------------------------------------------------
    def request_close(self) -> str:
        """窗口关闭请求，返回 'close' / 'ask' / 'wait'。

        - 没有任务在跑：'close'，直接关；
        - 有任务在跑且还没问过：'ask'，由界面弹窗询问；
        - 已经确认过取消：'wait'，等 worker 真正结束后界面再关闭。
        """
        if not self.is_running:
            return "close"
        if self._close_requested:
            return "wait"
        self._close_requested = True
        return "ask"

    def decline_close(self) -> None:
        """用户在询问里选了「不退出」：撤销等待标记，界面继续可用。"""
        self._close_requested = False

    def close_confirmed(self) -> int:
        """用户确认「取消并退出」：取消整队，等 worker 结束后再关闭窗口。"""
        return self.cancel_all()



    # --- worker 终态回调（GUI 在主线程转发） ------------------------------
    def on_progress(self, text, done=0, total=0):
        """当前项目的阶段与图片进度（done/total 始终是真实的单本进度）。"""
        item = self._current
        if item is None:
            return
        item.progress_text = text
        item.progress_done = done
        item.progress_total = total
        self._on_changed()

    def on_succeeded(self, output_path, summary="", failed_images=0):
        """当前项目完成；failed_images > 0 时记为「完成但有警告」。"""
        item = self._current
        if item is None:
            return
        item.output_path = output_path
        item.summary = summary
        item.failed_images = int(failed_images or 0)
        item.progress_text = ""
        if item.failed_images:
            item.status = STATUS_WARNING
            item.error = "有 %d 张图片处理失败，已原样保留" % item.failed_images
        else:
            item.status = STATUS_SUCCEEDED
            item.error = ""
        self._finish_item()
        self._on_changed()
        self._advance()

    def on_failed(self, message, suggestion="", details=""):
        """当前项目失败；失败不影响后续项目。"""
        item = self._current
        if item is None:
            return
        item.status = STATUS_FAILED
        item.error = message
        item.details = details or "\n".join(part for part in (message, suggestion) if part)
        item.progress_text = ""
        self._finish_item()
        self._on_changed()
        self._advance()

    def on_cancelled(self, cache_path=""):
        """当前项目被取消：停止整队，不再自动开始下一本。"""
        item = self._current
        if item is None:
            return
        item.status = STATUS_CANCELLED
        item.cache_path = cache_path or ""
        item.error = "已取消"
        item.details = ("处理已取消；临时缓存已保留，可用于排错：%s" % cache_path
                        if cache_path else "处理已取消")
        item.progress_text = ""
        self._finish_item()
        self._stopping = True
        self._on_changed()
        self._finish_queue()

    # --- 内部调度 ---------------------------------------------------------
    def _start_next(self):
        """启动下一个待处理项目（每次都新建 worker，绝不复用已结束的对象）。"""
        item = None if self._stopping else self._queue.next_pending()
        if item is None:
            self._current = None
            self._finish_queue()
            return
        item.status = STATUS_RUNNING
        item.progress_text = "正在准备..."
        item.progress_done = 0
        item.progress_total = 0
        item.error = ""
        item.details = ""
        item.output_path = ""
        item.summary = ""
        self._current = item
        self._on_changed()
        worker = self._factory(item)
        self._worker = worker
        worker.start()

    def _finish_item(self):
        """回收当前 worker：先让它真正结束，再创建下一个，避免残留线程/子进程。"""
        worker = self._worker
        self._worker = None
        self._current = None
        if worker is not None and self._dispose is not None:
            self._dispose(worker)

    def _finish_queue(self):
        """整队停止：不再启动任何新任务，并汇报汇总。"""
        self._stopping = False
        self._on_changed()
        self._on_finished(self._queue.summary())

    def _advance(self):
        """当前项目结束后的调度：正常结束继续下一本，被取消则停止整队。"""
        if self._stopping:
            self._finish_queue()
        else:
            self._start_next()

