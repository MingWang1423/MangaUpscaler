"""主窗口：EPUB 处理队列、顺序执行与进度/结果展示。

界面只负责「收集 → 展示 → 转发」：
  - 选择（多选 / 拖放）EPUB 后入队，不立即开始；
  - 队列模型与顺序调度在 gui/task_queue.py（纯 Python，可独立测试）；
  - 一次只创建一个 PipelineWorker，上一本结束（成功 / 失败 / 取消）后才创建下一个，
    因此不会同时启动多组 waifu2x 进程，也不会复用已结束的 QThread。
"""

from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from config_schema import DEFAULT_CONFIG, GPU_GUI_OPTIONS, NOISE_OPTIONS, SCALE_OPTIONS
from diagnostics import check_waifu2x_tool
from epub.builder import build_epub
from epub.reader import extract_images
from errors import classify_error, format_details
from gui.task_queue import (
    REJECT_LABELS, STATUS_CANCELLED, STATUS_FAILED, QueueController,
)
from logging_setup import get_logger, get_log_path
from resource_paths import app_icon_path
from upscaler.compressor import compress_folder, quality_target
from upscaler.waifu2x import upscale_folder
from workspace import TaskWorkspace

logger = get_logger("gui.main_window")

# 画质档位 -> 界面显示文案（仅 UI 用，不影响逻辑）
QUALITY_LABELS = {
    "original": "原画质（不压缩）",
    "4k": "4K（2160×3840）",
    "2k": "2.5K（1600×2560）",
}
# GPU -> 界面显示文案
GPU_LABELS = {
    "auto": "自动选择（推荐）",
    -1: "CPU",
    0: "GPU 0",
    1: "GPU 1",
    2: "GPU 2",
}


def load_app_icon():
    """从 resources/app.ico 载入窗口图标（QIcon）。

    路径由 resource_paths 解析：源码运行取项目根目录，PyInstaller onefile/onedir
    取运行时的资源目录，因此不依赖当前工作目录。图标缺失（或 Qt 无法解码 ICO）
    时返回空 QIcon，窗口使用系统默认图标，绝不抛异常。
    """
    path = app_icon_path()
    if not path.is_file():
        logger.warning("未找到窗口图标: %s", path)
        return QIcon()
    return QIcon(str(path))


class PipelineWorker(QThread):
    """在子线程里顺序执行「提取图片 -> 放大图片 -> 压缩图片 -> 打包 EPUB」。

    每次任务使用独立的工作目录（TaskWorkspace），成功时自动清理；失败或取消时
    保留缓存目录供排错，并通过信号把缓存路径回传给界面。
    取消采用协作式：cancel() 只把标志位置 True，子线程在每个阶段开始前以及
    底层耗时循环的间隙检查该标志，发现被取消就主动抛 InterruptedError 退出。
    绝不使用 terminate() 之类的强制手段，避免留下写了一半的文件。
    """

    progress = Signal(str, int, int)  # (提示文本, 已完成, 总数)；总数 0 表示不确定
    succeeded = Signal(str, str)      # 全部成功，参数为 (新 EPUB 完整路径, 统计摘要)
    cancelled = Signal(str)           # 用户取消，参数为保留的缓存目录路径（可为空）
    failed = Signal(str, str, str)    # 失败，参数为 (友好信息, 建议, 详细信息)

    def __init__(self, epub_path, scale=2, noise=3, quality="4k", gpu="auto",
                 output_dir=None, parent=None):
        super().__init__(parent)
        self._epub_path = epub_path
        self._scale = scale
        self._noise = noise
        self._quality = quality
        self._gpu = gpu
        self._output_dir = output_dir
        self._is_cancelled = False
        # 最近一次 run() 里失败（含原样保留）的图片数；信号语义保持不变，
        # 上层据此区分「完全成功」与「完成但有部分图片失败」。
        self.failure_count = 0

    def cancel(self) -> None:
        """请求取消：只设置标志位，真正的退出由子线程在检查点完成。"""
        self._is_cancelled = True

    def _check_cancelled(self) -> None:
        """检查点：被取消就抛 InterruptedError，交由 run() 的兜底处理。"""
        if self._is_cancelled:
            raise InterruptedError("用户已取消")

    def run(self) -> None:
        """子线程入口：串联底层函数，只通过信号与主线程通信。"""
        workspace = None
        cache_path = ""
        output_path = ""

        try:
            # 每次任务一个独立工作目录（extracted/upscaled/compressed），不依赖 CWD
            workspace = TaskWorkspace()
            cache_path = str(workspace.root)

            # 阶段1：提取图片
            self._check_cancelled()
            self.progress.emit("正在提取图片...", 0, 0)
            images = extract_images(self._epub_path, str(workspace.extracted), clean=False)
            if not images:
                raise RuntimeError("EPUB 内没有可提取的图片")

            # 阶段2：放大图片
            self._check_cancelled()
            logger.info("放大参数: scale=%s, noise=%s, quality=%s, gpu=%s",
                        self._scale, self._noise, self._quality, self._gpu)
            upscaled, skipped, copied, up_failed = upscale_folder(
                str(workspace.extracted),
                str(workspace.upscaled),
                scale=self._scale,
                noise=self._noise,
                target=quality_target(self._quality),
                progress_callback=self._on_upscale_progress,
                cancel_check=lambda: self._is_cancelled,
                clean=False,
                gpu=self._gpu,
            )

            # 阶段3：压缩图片
            self._check_cancelled()
            logger.info("压缩参数: quality=%s", self._quality)
            _, compressed_failed = compress_folder(
                str(workspace.upscaled),
                str(workspace.compressed),
                quality=self._quality,
                progress_callback=self._on_compress_progress,
                cancel_check=lambda: self._is_cancelled,
                clean=False,
            )

            # 阶段4：打包 EPUB（build_epub 内部先写临时文件，成功后原子替换正式输出）
            self._check_cancelled()
            output_dir = Path(self._output_dir) if self._output_dir else Path("output")
            output_filename = f"{Path(self._epub_path).stem}_upscaled.epub"
            output_path = build_epub(
                self._epub_path,
                str(workspace.compressed),
                output_epub_path=str(output_dir / output_filename),
                progress_callback=self._on_build_progress,
                cancel_check=lambda: self._is_cancelled,
            )

            total_failed = up_failed + compressed_failed
            summary = (f"超分 {upscaled} 张，跳过 {skipped} 张，"
                       f"原样复制 {copied} 张，失败 {total_failed} 张")
            self.failure_count = total_failed
        except InterruptedError:
            self.cancelled.emit(cache_path)
            return
        except Exception as exc:  # 兜底：任何异常都要让界面恢复可用
            logger.exception("流水线失败")
            _, title, message, suggestion = classify_error(exc)
            friendly = f"{title}：{message}"
            if cache_path:
                friendly += f"\n临时缓存已保留：{cache_path}"
            self.failed.emit(friendly, suggestion, format_details(exc))
            return

        # 成功：清理本次任务的工作目录（最终 EPUB 在输出目录，不受影响）
        if workspace is not None:
            workspace.cleanup()
        self.succeeded.emit(output_path, summary)

    def _on_upscale_progress(self, done: int, total: int) -> None:
        self.progress.emit(f"正在放大：{done}/{total} 张", done, total)

    def _on_build_progress(self, done: int, total: int) -> None:
        self.progress.emit(f"正在打包：{done}/{total} 张", done, total)

    def _on_compress_progress(self, done: int, total: int) -> None:
        self.progress.emit(f"正在压缩图片：{done}/{total} 张", done, total)


class SettingsDialog(QDialog):
    """放大倍数、降噪等级与画质档位的设置对话框。

    只负责收集用户选择，不碰文件：落盘由 MainWindow 通过注入的 save_config
    回调完成，避免 gui 层反向依赖入口模块（main.py 运行时模块名是 __main__）。
    """

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")

        self.scale_combo = QComboBox()
        for scale in SCALE_OPTIONS:
            self.scale_combo.addItem(f"{scale}x", scale)

        self.noise_combo = QComboBox()
        for noise in NOISE_OPTIONS:
            self.noise_combo.addItem(str(noise), noise)

        self.quality_combo = QComboBox()
        for key, label in QUALITY_LABELS.items():
            self.quality_combo.addItem(label, key)

        self.gpu_combo = QComboBox()
        for gpu in GPU_GUI_OPTIONS:
            self.gpu_combo.addItem(GPU_LABELS.get(gpu, str(gpu)), gpu)

        self._select_by_data(self.scale_combo, config.get("scale"))
        self._select_by_data(self.noise_combo, config.get("noise"))
        self._select_by_data(self.quality_combo, config.get("quality"))
        self._select_by_data(self.gpu_combo, config.get("gpu"))

        form = QFormLayout()
        form.addRow("放大倍数", self.scale_combo)
        form.addRow("降噪等级", self.noise_combo)
        form.addRow("画质档位", self.quality_combo)
        form.addRow("GPU", self.gpu_combo)

        save_button = QPushButton("保存")
        save_button.clicked.connect(self.accept)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)        # 按钮靠右，符合常见对话框习惯
        buttons.addWidget(save_button)
        buttons.addWidget(cancel_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(buttons)

    @staticmethod
    def _select_by_data(combo, value) -> None:
        """按 itemData 选中；配置值不在候选范围内时保持默认的第一项。"""
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def get_config(self) -> dict:
        """返回当前选择（itemData 里存的是配置值，而非显示文本）。"""
        return {
            "scale": self.scale_combo.currentData(),
            "noise": self.noise_combo.currentData(),
            "quality": self.quality_combo.currentData(),
            "gpu": self.gpu_combo.currentData(),
        }


class MainWindow(QMainWindow):
    """主窗口：把多个 EPUB 加入队列，按顺序一本一本地跑完整条流水线。

    界面只负责「收集 → 展示 → 转发」：
      - 选择（多选 / 拖放）EPUB 后只入队，不立即开始；
      - 队列模型与顺序调度放在 gui/task_queue.py（纯 Python，可独立测试）；
      - 一次只创建一个 PipelineWorker，上一本结束（成功 / 失败 / 取消）后才创建
        下一个，所以不会同时启动多组 waifu2x 进程，也不会复用已结束的 QThread。
    """

    def __init__(self, config=None, save_config_callback=None,
                 default_output_dir=None) -> None:
        super().__init__()
        self.setWindowTitle("MangaUpscaler")
        self.setWindowIcon(load_app_icon())     # 窗口/任务栏图标（resources/app.ico）
        # config 由 main.py 注入（已带默认值兜底）；保存/默认输出目录回调也由入口注入
        self.config = dict(config) if config else dict(DEFAULT_CONFIG)
        self._save_config = save_config_callback or (lambda _config: None)
        self._get_default_output_dir = default_output_dir or (lambda: "output")
        self.selected_epub_path = None
        self._closing = False
        self._cancel_requested = False     # 已请求「取消当前」，按钮保持禁用
        self._last_error_details = ""
        self.controller = QueueController(
            worker_factory=self._create_worker,
            dispose_worker=self._dispose_worker,
            on_changed=self._on_queue_changed,
            on_finished=self._on_queue_finished,
        )

        self._build_ui()
        self.setAcceptDrops(True)          # 支持把一个或多个 EPUB 拖进窗口
        self._on_queue_changed()

    def _build_ui(self) -> None:
        """搭建界面：队列列表 + 队列操作 + 原有输出目录/设置/进度区。"""
        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # 队列操作行
        queue_row = QHBoxLayout()
        self.add_button = QPushButton("添加 EPUB")
        self.add_button.clicked.connect(self._on_add_epubs)
        queue_row.addWidget(self.add_button)
        self.remove_button = QPushButton("删除选中")
        self.remove_button.clicked.connect(self._on_remove_selected)
        queue_row.addWidget(self.remove_button)
        self.clear_button = QPushButton("清空队列")
        self.clear_button.clicked.connect(self._on_clear_queue)
        queue_row.addWidget(self.clear_button)
        queue_row.addStretch(1)
        layout.addLayout(queue_row)

        # 待处理列表：文件名 / 状态 / 输出路径或错误摘要
        self.queue_table = QTableWidget(0, 3)
        self.queue_table.setHorizontalHeaderLabels(["文件", "状态", "输出 / 错误"])
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.queue_table.verticalHeader().setVisible(False)
        header = self.queue_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        layout.addWidget(self.queue_table, 1)

        # 队列状态：第 x/n 本 + 已完成/失败/待处理本数
        self.queue_label = QLabel("队列为空")
        layout.addWidget(self.queue_label)

        # 输出路径行：标签 + 可编辑输入框 + 浏览按钮
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("输出路径："))
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setText(
            self.config.get("output_dir") or self._get_default_output_dir()
        )
        output_row.addWidget(self.output_dir_edit, 1)
        self.browse_button = QPushButton("浏览...")
        self.browse_button.clicked.connect(self._on_browse_output_dir)
        output_row.addWidget(self.browse_button)
        layout.addLayout(output_row)

        # 队列控制行：一次只跑一本；取消当前不会自动开始下一本
        action_row = QHBoxLayout()
        self.start_button = QPushButton("开始处理")
        self.start_button.clicked.connect(self._on_start_clicked)
        action_row.addWidget(self.start_button)
        self.cancel_current_button = QPushButton("取消当前")
        self.cancel_current_button.clicked.connect(self._on_cancel_current_clicked)
        action_row.addWidget(self.cancel_current_button)
        self.cancel_queue_button = QPushButton("取消队列")
        self.cancel_queue_button.clicked.connect(self._on_cancel_queue_clicked)
        action_row.addWidget(self.cancel_queue_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        # 状态标签：次要样式（灰色小字），放在进度条上方，用来说明"正在处理什么"
        self.status_label = QLabel("")
        self.status_label.setVisible(False)
        status_font = self.status_label.font()
        if status_font.pointSizeF() > 0:      # 比默认小一号，其余沿用系统默认字体
            status_font.setPointSizeF(status_font.pointSizeF() - 1)
        self.status_label.setFont(status_font)
        self.status_label.setStyleSheet("color: gray;")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # 设置按钮：放在底部右下角，不干扰一键处理的主流程
        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        self.settings_button = QPushButton("设置")
        self.settings_button.clicked.connect(self._on_settings_clicked)
        bottom_row.addWidget(self.settings_button)
        layout.addLayout(bottom_row)

    def _on_settings_clicked(self) -> None:
        """打开设置对话框；点「保存」才更新内存配置并落盘。"""
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.Accepted:
            self.config.update(dialog.get_config())  # 更新 scale/noise/quality/gpu
            self._save_config(self.config)      # 落盘交给 main.py 注入的回调
            logger.info("配置已保存: %s", self.config)

    def _on_browse_output_dir(self) -> None:
        """浏览选择输出目录：更新输入框并立即落盘。"""
        chosen = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self.output_dir_edit.text()
        )
        if not chosen:
            return
        self.output_dir_edit.setText(chosen)
        self.config["output_dir"] = chosen
        self._save_config(self.config)
        logger.info("输出目录已设置: %s", chosen)

    def _check_can_process(self) -> bool:
        """开始处理前检查核心 waifu2x 组件；缺失时阻止任务并给出一次清晰提示。"""
        missing = check_waifu2x_tool()
        if not missing:
            return True
        QMessageBox.critical(
            self, "无法开始处理",
            "缺少 waifu2x 组件，暂时无法处理：\n" + "\n".join(missing)
            + "\n\n请下载完整包，或将 waifu2x-ncnn-vulkan 放到 tools/waifu2x-ncnn-vulkan/。"
        )
        return False

    # --- 添加 / 删除 EPUB ---------------------------------------------------
    def _on_add_epubs(self) -> None:
        """多选 EPUB：只加入队列，不立即开始处理。"""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "选择 EPUB 文件", "", "EPUB 文件 (*.epub)"
        )
        if not file_paths:
            return
        self._enqueue_paths(file_paths)

    def _enqueue_paths(self, paths) -> None:
        """过滤后入队，并把被拒绝的文件原因一次性告诉用户。"""
        added, rejected = self.controller.add(paths)
        if added:
            self.selected_epub_path = added[0].epub_path
            logger.info("已加入队列: %s", [item.epub_path for item in added])
        if rejected:
            lines = ["%s（%s）" % (Path(path).name, REJECT_LABELS.get(reason, reason))
                     for path, reason in rejected]
            QMessageBox.information(
                self, "部分文件未加入队列",
                "以下文件被跳过：\n" + "\n".join(lines)
            )

    def dragEnterEvent(self, event) -> None:
        """拖放进入：只接受至少包含一个 .epub 的拖放。"""
        if self._dropped_epubs(event) and not self.controller.is_running:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        """拖放落下：把所有 .epub 加入队列（过滤交给队列模型统一处理）。"""
        paths = self._dropped_epubs(event)
        if paths:
            event.acceptProposedAction()
            self._enqueue_paths(paths)

    @staticmethod
    def _dropped_epubs(event):
        """从拖放数据里挑出 .epub 路径（其余交给队列模型报「不是 EPUB 文件」）。"""
        if not event.mimeData().hasUrls():
            return []
        return [url.toLocalFile() for url in event.mimeData().urls()
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".epub")]

    def _selected_paths(self):
        """列表里选中项目对应的 EPUB 路径（表格行序与队列顺序一致）。"""
        items = self.controller.items
        return [items[row].epub_path for row in sorted(self._selected_rows())
                if 0 <= row < len(items)]

    def _selected_rows(self):
        return {index.row() for index in self.queue_table.selectionModel().selectedRows()}

    def _on_remove_selected(self) -> None:
        """删除选中的项目；正在处理的项目会被模型拒绝（不会被删掉）。"""
        paths = self._selected_paths()
        if not paths:
            return
        removed, refused = self.controller.remove_all(paths)
        if refused:
            QMessageBox.information(
                self, "无法删除",
                "正在处理的项目不能删除，其余项目已删除。"
            )
        logger.info("已从队列删除 %d 项", removed)

    def _on_clear_queue(self) -> None:
        """清空队列：空闲时清掉全部项目；运行中只清掉「待处理」项目。

        清空后统一刷新界面（表格 / 状态标签 / 进度 / 按钮），并清掉选中状态，
        避免选中行指向已被删除的项目。没有任何可清除项目时给出明确提示，
        不静默表现得像按钮坏了。只动队列数据，绝不删除输入 EPUB 或输出文件。
        """
        if self.controller.clearable_count() <= 0:
            self._show_nothing_to_clear()
            return
        cleared = self.controller.clear()
        if not cleared:
            self._show_nothing_to_clear()
            return
        logger.info("已从队列清除 %d 个项目（运行中只清待处理）", cleared)
        self.queue_table.clearSelection()      # 清空选中状态
        self._refresh_queue_table()
        self._update_queue_summary()
        self._update_progress()
        self._update_button_states()

    def _show_nothing_to_clear(self) -> None:
        """没有可清除项目时的明确反馈，并同步一次按钮状态。"""
        self._update_button_states()
        QMessageBox.information(self, "提示", "没有可清除的任务。")

    # --- 开始 / 取消 -------------------------------------------------------
    def _on_start_clicked(self) -> None:
        """开始顺序处理队列；运行期间重复点击无效。"""
        if self.controller.is_running:
            return
        if not self._check_can_process():
            return
        self._persist_output_dir()
        if not self.controller.start():
            QMessageBox.information(self, "提示", "队列里没有待处理的项目。")

    def _on_cancel_current_clicked(self) -> None:
        """只取消当前正在处理的那一本；不再自动开始下一本。"""
        if not self.controller.cancel_current():
            return
        self._cancel_requested = True                  # 防重复点击
        self.cancel_current_button.setEnabled(False)
        logger.info("已请求取消当前项目")

    def _on_cancel_queue_clicked(self) -> None:
        """取消整队：先取消当前任务，剩余待处理项目标记为未处理。"""
        skipped = self.controller.cancel_all()
        logger.info("已请求取消整个队列，剩余 %d 项标记为未处理", skipped)

    def _persist_output_dir(self) -> str:
        """以输入框为准确定输出目录，变化时落盘（沿用原有行为）。"""
        output_dir = self.output_dir_edit.text().strip() or self._get_default_output_dir()
        self.output_dir_edit.setText(output_dir)
        if output_dir != self.config.get("output_dir"):
            self.config["output_dir"] = output_dir
            self._save_config(self.config)
        return output_dir

    # --- worker 生命周期 ---------------------------------------------------
    def _create_worker(self, item):
        """为队列项目创建新的 PipelineWorker（绝不复用已结束的 worker）。"""
        logger.info("开始处理 (%s): %s", item.name, item.epub_path)
        self._cancel_requested = False     # 新的一本开始，「取消当前」重新可用
        worker = PipelineWorker(
            item.epub_path, scale=self.config["scale"], noise=self.config["noise"],
            quality=self.config["quality"], gpu=self.config.get("gpu", "auto"),
            output_dir=self._persist_output_dir(),
        )
        worker.progress.connect(self._on_worker_progress)
        worker.succeeded.connect(self._on_worker_succeeded)
        worker.cancelled.connect(self._on_worker_cancelled)
        worker.failed.connect(self._on_worker_failed)
        return worker

    def _dispose_worker(self, worker) -> None:
        """等子线程真正结束后再回收（不留后台线程，也就不会残留 waifu2x 子进程）。"""
        worker.wait()
        worker.deleteLater()

    # --- 单本进度与终态（转发布到队列控制器） ------------------------------
    def _on_worker_progress(self, text: str, done: int, total: int) -> None:
        self.controller.on_progress(text, done, total)

    def _on_worker_succeeded(self, output_path: str, summary: str) -> None:
        worker = self.controller.running_worker
        failed_images = getattr(worker, "failure_count", 0)
        self.controller.on_succeeded(output_path, summary, failed_images)

    def _on_worker_cancelled(self, cache_path: str) -> None:
        self.controller.on_cancelled(cache_path)

    def _on_worker_failed(self, message: str, suggestion: str, details: str) -> None:
        self.controller.on_failed(message, suggestion, details)


    # --- 队列视图刷新 ------------------------------------------------------
    def _on_queue_changed(self) -> None:
        """队列或当前进度有变化：统一刷新列表、标签、进度与按钮状态。"""
        self._refresh_queue_table()
        self._update_queue_summary()
        self._update_progress()
        self._update_button_states()

    def _refresh_queue_table(self) -> None:
        """重建列表行：文件名 / 状态 / 输出路径或错误摘要。"""
        items = self.controller.items
        self.queue_table.setRowCount(len(items))
        for row, item in enumerate(items):
            values = (item.name, item.status_label, item.display_detail)
            for column, value in enumerate(values):
                cell = self.queue_table.item(row, column)
                if cell is None:
                    cell = QTableWidgetItem()
                    self.queue_table.setItem(row, column, cell)
                cell.setText(value)

    def _update_queue_summary(self) -> None:
        """显示「正在处理第 x/n 本」与已完成/失败/取消/待处理本数。"""
        counts = self.controller.counts()
        done, total = self.controller.current_position()
        parts = []
        if self.controller.is_running and total:
            parts.append("正在处理第 %d/%d 本：%s" % (done, total,
                                                    self.controller.running_item.name))
        elif total:
            parts.append("队列共 %d 本" % total)
        else:
            parts.append("队列为空")
        succeeded = self.controller.queue.succeeded_count()
        parts.append("已完成 %d 本" % succeeded)
        parts.append("失败 %d 本" % counts[STATUS_FAILED])
        parts.append("取消 %d 本" % counts[STATUS_CANCELLED])
        parts.append("待处理 %d 本" % self.controller.queue.unprocessed_count())
        self.queue_label.setText("　".join(parts))

    def _update_progress(self) -> None:
        """当前这一本的进度：始终使用真实的 done/total；没有总数时用不确定进度。

        队列级进度只按「书籍完成数」表达（见 _update_queue_summary），
        不伪造总体百分比；队列为空或空闲时隐藏并重置进度条。
        """
        item = self.controller.running_item
        if item is None:
            self.status_label.setText("")
            self.status_label.setVisible(False)
            self.progress_bar.setVisible(False)
            self.progress_bar.reset()
            return
        self.status_label.setText(item.progress_text or "正在准备...")
        self.status_label.setVisible(True)
        self.progress_bar.setVisible(True)
        if item.progress_total > 0:
            self.progress_bar.setRange(0, item.progress_total)
            self.progress_bar.setValue(item.progress_done)
        else:
            self.progress_bar.setRange(0, 0)      # 总数未知：不确定进度


    # --- 整队结束后的汇总与收尾操作 -----------------------------------------
    def _on_queue_finished(self, summary: str) -> None:
        """整队结束：显示汇总，并提供打开输出/复制错误/查看日志等操作。"""
        self._cancel_requested = False
        self._update_button_states(running=False)
        failed = [item for item in self.controller.items
                  if item.status == STATUS_FAILED]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning if failed else QMessageBox.Information)
        box.setWindowTitle("队列处理完成")
        box.setText(summary)
        if failed:
            box.setInformativeText("失败：%s" % "、".join(item.name for item in failed))
        open_dir_btn = box.addButton("打开输出目录", QMessageBox.ActionRole)
        open_file_btn = box.addButton("打开选中项目的输出文件", QMessageBox.ActionRole)
        copy_btn = box.addButton("复制错误详情", QMessageBox.ActionRole)
        log_btn = box.addButton("查看日志目录", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is open_dir_btn:
            self._open_output_dir()
        elif clicked is open_file_btn:
            self._open_selected_output()
        elif clicked is copy_btn:
            self._copy_error_details()
        elif clicked is log_btn:
            self._open_log_dir()

        if self._closing:
            # 用户确认过「取消并退出」：worker 已真正结束，现在才真正关闭窗口
            self.close()

    def _open_output_dir(self) -> None:
        """打开当前设置的输出目录。"""
        output_dir = Path(self._persist_output_dir())
        if not output_dir.exists():
            QMessageBox.information(self, "提示", "输出目录还不存在：\n%s" % output_dir)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output_dir)))

    def _open_selected_output(self) -> None:
        """打开选中项目的输出 EPUB（没有输出时给出提示）。"""
        paths = self._selected_paths()
        for item in self.controller.items:
            if paths and item.epub_path == paths[0] and item.output_path:
                QDesktopServices.openUrl(QUrl.fromLocalFile(item.output_path))
                return
        QMessageBox.information(self, "提示", "选中的项目还没有生成输出文件。")

    def _copy_error_details(self) -> None:
        """把失败项目的详细错误复制到剪贴板。"""
        details = [item.details or item.error for item in self.controller.items
                   if item.status == STATUS_FAILED and (item.details or item.error)]
        if not details and self._last_error_details:
            details = [self._last_error_details]
        if not details:
            QMessageBox.information(self, "提示", "没有可复制的错误详情。")
            return
        QApplication.clipboard().setText("\n\n".join(details))
        logger.info("错误详情已复制到剪贴板")

    def _open_log_dir(self) -> None:
        """打开日志目录（沿用原有行为）。"""
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(get_log_path().parent)))

    # --- 运行状态与窗口关闭 -------------------------------------------------
    def _update_button_states(self, running=None) -> None:
        """按「队列实际状态」统一计算所有按钮的启用/禁用。

        - 运行中禁掉会影响当前任务的设置（添加 / 设置 / 浏览 / 输出目录）；
        - 「开始处理」只在空闲且确实有「待处理」项目时可用（清空队列后自动变灰）；
        - 「清空队列」按「是否存在可清除项目」计算，不因为处于运行状态就无条件禁用
          （运行中只要有待处理项目就仍然可以清除它们）。
        """
        running = self.controller.is_running if running is None else running
        for widget in (self.add_button, self.settings_button, self.browse_button):
            widget.setEnabled(not running)
        self.output_dir_edit.setEnabled(not running)
        self.start_button.setEnabled(
            not running and self.controller.queue.pending_count() > 0)
        self.clear_button.setEnabled(self.controller.clearable_count() > 0)
        self.remove_button.setEnabled(self.controller.queue.removable_count() > 0)
        self.cancel_current_button.setEnabled(running and not self._cancel_requested)
        self.cancel_queue_button.setEnabled(running or bool(self.controller.items))

    def closeEvent(self, event) -> None:
        """关窗：没有任务直接关；有任务时询问是否取消并退出。

        用户确认取消后不会阻塞在这里等：先请求取消整队，等 PipelineWorker 真正
        结束（子进程被终止、线程回收）后再由 _on_queue_finished 关闭窗口，
        因此不会遗留 waifu2x 子进程，也不会留下写了一半的输出。
        """
        action = self.controller.request_close()
        if action == "close":
            event.accept()
            return
        if action == "wait":
            event.ignore()          # 已在取消过程中，等 worker 结束
            return

        reply = QMessageBox.question(
            self, "任务正在运行",
            "队列还在处理，是否取消当前任务并退出？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            self.controller.decline_close()
            event.ignore()
            return
        logger.info("用户选择取消并退出，等待 worker 结束")
        self._closing = True
        self.controller.close_confirmed()
        event.ignore()

