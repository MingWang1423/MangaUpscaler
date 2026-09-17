import os
import shutil
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

from epub.builder import build_epub
from epub.reader import extract_images
from upscaler.waifu2x import upscale_folder

# 配置默认值与可选范围（设置界面与流水线共用）
DEFAULT_CONFIG = {"scale": 2, "noise": 3}
SCALE_OPTIONS = (2, 4)
NOISE_OPTIONS = (-1, 0, 1, 2, 3)


class PipelineWorker(QThread):
    """在子线程里顺序执行「提取图片 -> 放大图片 -> 打包 EPUB -> 清理缓存」。

    取消采用协作式：cancel() 只把标志位置 True，子线程在每个阶段开始前以及
    底层耗时循环的间隙检查该标志，发现被取消就主动抛 InterruptedError 退出。
    绝不使用 terminate() 之类的强制手段，避免留下写了一半的文件。
    """

    progress = Signal(str, int, int)  # (提示文本, 已完成, 总数)；总数 0 表示不确定
    succeeded = Signal(str)           # 全部成功，参数为新 EPUB 的完整路径
    cancelled = Signal()              # 用户取消
    failed = Signal(str)              # 失败，参数为错误信息

    EXTRACTED_DIR = "temp/extracted"
    UPSCALED_DIR = "temp/upscaled"

    def __init__(self, epub_path, scale=2, noise=3, output_dir=None, parent=None):
        super().__init__(parent)
        self._epub_path = epub_path
        self._scale = scale
        self._noise = noise
        self._output_dir = output_dir
        self._is_cancelled = False

    def cancel(self) -> None:
        """请求取消：只设置标志位，真正的退出由子线程在检查点完成。"""
        self._is_cancelled = True

    def _check_cancelled(self) -> None:
        """检查点：被取消就抛 InterruptedError，交由 run() 的兜底处理。"""
        if self._is_cancelled:
            raise InterruptedError("用户已取消")

    def run(self) -> None:
        """子线程入口：串联底层函数，只通过信号与主线程通信。"""
        # 记录打包前的状态：失败/取消时清掉这次写了一半的残缺输出（只删本次新产生的）
        output_dir = self._output_dir or "output"
        output_filename = f"{Path(self._epub_path).stem}_upscaled.epub"
        pending_output = Path(output_dir) / output_filename
        output_existed = pending_output.exists()

        try:
            # 阶段1：提取图片
            self._check_cancelled()
            self.progress.emit("正在提取图片...", 0, 0)
            images = extract_images(self._epub_path)
            if not images:
                raise RuntimeError("EPUB 内没有可提取的图片")

            # 阶段2：放大图片
            self._check_cancelled()
            print(f"放大参数: scale={self._scale}, noise={self._noise}")
            success, failed_count = upscale_folder(
                self.EXTRACTED_DIR,
                self.UPSCALED_DIR,
                scale=self._scale,
                noise=self._noise,
                progress_callback=self._on_upscale_progress,
                cancel_check=lambda: self._is_cancelled,
            )
            if failed_count > 0:
                raise RuntimeError(
                    f"放大失败 {failed_count} 张图片（共 {success + failed_count} 张）"
                )

            # 阶段3：打包 EPUB
            self._check_cancelled()
            os.makedirs(output_dir, exist_ok=True)   # 双保险（build_epub 内部也会建父目录）
            output_path = build_epub(
                self._epub_path,
                self.UPSCALED_DIR,
                output_epub_path=os.path.join(output_dir, output_filename),
                progress_callback=self._on_build_progress,
                cancel_check=lambda: self._is_cancelled,
            )

            # 阶段4：清理临时文件（只有全部成功才会走到这里）
            self._check_cancelled()
            self.progress.emit("正在清理临时文件...", 0, 0)
            shutil.rmtree(self.EXTRACTED_DIR, ignore_errors=True)
            shutil.rmtree(self.UPSCALED_DIR, ignore_errors=True)
        except InterruptedError:
            self._discard_pending_output(pending_output, output_existed)
            self.cancelled.emit()
            return
        except Exception as exc:  # 兜底：任何异常都要让界面恢复可用
            self._discard_pending_output(pending_output, output_existed)
            self.failed.emit(str(exc))
            return

        self.succeeded.emit(output_path)

    @staticmethod
    def _discard_pending_output(pending_output, output_existed) -> None:
        """删除本次流水线写了一半的输出 EPUB（打包阶段才会生成）。

        只删"本次新产生"的文件：打包前就已存在的产物（上一次成功的结果）不动。
        失败或取消时保留 temp 目录以便排查，但没有必要留下残缺的成品。
        """
        if output_existed or not pending_output.exists():
            return
        try:
            pending_output.unlink()
        except OSError as exc:
            print(f"警告: 无法删除未完成的输出文件 {pending_output}: {exc}")

    def _on_upscale_progress(self, done: int, total: int) -> None:
        self.progress.emit(f"正在放大：{done}/{total} 张", done, total)

    def _on_build_progress(self, done: int, total: int) -> None:
        self.progress.emit(f"正在打包：{done}/{total} 张", done, total)


class SettingsDialog(QDialog):
    """放大倍数与降噪等级的设置对话框。

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

        self._select_by_data(self.scale_combo, config.get("scale"))
        self._select_by_data(self.noise_combo, config.get("noise"))

        form = QFormLayout()
        form.addRow("放大倍数", self.scale_combo)
        form.addRow("降噪等级", self.noise_combo)

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
        """返回当前选择（itemData 里存的是 int，而不是显示文本）。"""
        return {
            "scale": self.scale_combo.currentData(),
            "noise": self.noise_combo.currentData(),
        }


class MainWindow(QMainWindow):
    """主窗口：选好 EPUB 后一键跑完整条流水线，运行期间可随时取消。"""

    def __init__(self, config=None, save_config_callback=None,
                 default_output_dir=None) -> None:
        super().__init__()
        self.setWindowTitle("MangaUpscaler")
        # config 由 main.py 注入（已带默认值兜底）；保存/默认输出目录回调也由入口注入
        self.config = dict(config) if config else dict(DEFAULT_CONFIG)
        self._save_config = save_config_callback or (lambda _config: None)
        self._get_default_output_dir = default_output_dir or (lambda: "output")
        self.selected_epub_path = None
        self.pipeline_worker = None

        central_widget = QWidget(self)
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout(central_widget)
        self.select_button = QPushButton("选择 EPUB")
        self.select_button.clicked.connect(self._on_select_epub)
        layout.addWidget(self.select_button)

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

        # 取消按钮：平时隐藏，只在流水线运行期间出现
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.cancel_button.setVisible(False)
        layout.addWidget(self.cancel_button)

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
            self.config = dialog.get_config()
            self._save_config(self.config)      # 落盘交给 main.py 注入的回调
            print(f"配置已保存: {self.config}")

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
        print(f"输出目录已设置: {chosen}")

    def _on_select_epub(self) -> None:
        """选好文件后立即启动流水线，无需再点第二次。"""
        if self.pipeline_worker is not None and self.pipeline_worker.isRunning():
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择 EPUB 文件", "", "EPUB 文件 (*.epub)"
        )
        if not file_path:
            return
        self.selected_epub_path = file_path
        print(f"已选择文件: {file_path}")

        # 输出目录：以输入框为准，空则回退默认；手动编辑的值也一并落盘
        output_dir = self.output_dir_edit.text().strip() or self._get_default_output_dir()
        self.output_dir_edit.setText(output_dir)
        if output_dir != self.config.get("output_dir"):
            self.config["output_dir"] = output_dir
            self._save_config(self.config)

        self.progress_bar.setRange(0, 0)   # 提取阶段还没有总数，先显示忙碌动画
        self.progress_bar.setVisible(True)
        self.status_label.setText("正在提取图片...")
        self.status_label.setVisible(True)
        self._set_running(True)

        self.pipeline_worker = PipelineWorker(
            file_path, scale=self.config["scale"], noise=self.config["noise"],
            output_dir=output_dir,
        )
        self.pipeline_worker.progress.connect(self._on_pipeline_progress)
        self.pipeline_worker.succeeded.connect(self._on_pipeline_succeeded)
        self.pipeline_worker.cancelled.connect(self._on_pipeline_cancelled)
        self.pipeline_worker.failed.connect(self._on_pipeline_failed)
        self.pipeline_worker.start()

    def _on_cancel_clicked(self) -> None:
        """请求取消：立即禁用按钮防重复点击，真正的退出由子线程完成。"""
        worker = self.pipeline_worker
        if worker is not None and worker.isRunning():
            self.cancel_button.setEnabled(False)
            worker.cancel()

    def _on_pipeline_progress(self, text: str, done: int, total: int) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(True)
        if total > 0:
            self.progress_bar.setRange(0, total)   # 每个阶段开始时重新设定上限
            self.progress_bar.setValue(done)
        else:
            self.progress_bar.setRange(0, 0)       # 不确定进度：显示忙碌动画
        self.progress_bar.setVisible(True)

    def _on_pipeline_succeeded(self, output_path: str) -> None:
        self._finish_pipeline()
        QMessageBox.information(self, "完成", f"新 EPUB 已生成：\n{output_path}")

    def _on_pipeline_cancelled(self) -> None:
        self._finish_pipeline()
        QMessageBox.information(self, "提示", "处理已取消。临时文件已保留在 temp 目录。")

    def _on_pipeline_failed(self, message: str) -> None:
        self._finish_pipeline()
        QMessageBox.critical(
            self, "错误", f"处理失败：{message}\n临时文件已保留在 temp 目录。"
        )

    def _finish_pipeline(self) -> None:
        """三种终态（成功/取消/失败）的统一收尾：回收线程并恢复按钮状态。"""
        self._release_pipeline_worker()
        self._set_running(False)
        self.progress_bar.setVisible(False)
        self.progress_bar.reset()
        self.status_label.setVisible(False)

    def _release_pipeline_worker(self) -> None:
        """等子线程真正结束后再回收 worker 对象。"""
        worker = self.pipeline_worker
        self.pipeline_worker = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()

    def _set_running(self, running: bool) -> None:
        """运行期间禁用"选择 EPUB"并显示"取消"；结束后反过来。"""
        self.select_button.setEnabled(not running)
        self.cancel_button.setVisible(running)
        self.cancel_button.setEnabled(True)   # 每次进入运行态都重新可用
