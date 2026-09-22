"""fake_waifu2x.py — 测试专用的假 waifu2x 子进程（不启动真实 exe）。

按真实 waifu2x-ncnn-vulkan 目录模式的行为模拟：
  - 从命令行解析 -i / -o / -s / -n / -f / -g；
  - 把 input 目录里的每个文件按 -s 放大、按 -f 编码后写到 output 目录，
    产物名 = 「输入名 + 输出格式扩展名」（与实测行为一致）；
  - 可注入故障：非零退出码、产物缺失、产物格式错误、产物尺寸错误、丢失透明
    通道、长时间运行（用于取消），从而覆盖批处理失败回退与取消路径。
"""

import contextlib
import subprocess
from pathlib import Path
from unittest import mock

from PIL import Image

# waifu2x -f 支持的格式 -> Pillow 格式名
FORMAT_TO_PIL = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}


class FakeWaifu2xProcess:
    """假的 waifu2x 进程（Popen 替身）；故障注入参数按「批次输入文件名」匹配。"""

    def __init__(self, cmd, returncode=0, missing=(), wrong_format=(),
                 wrong_size=(), drop_alpha=(), corrupt=(), hang=False,
                 on_communicate=None, ignore_terminate=0, pid=4242):
        """故障注入用「输入文件在排序后的序号」，避免测试依赖临时文件名规则。

        missing / wrong_format / wrong_size / drop_alpha / corrupt：序号集合；
        ignore_terminate=N：前 N 次 terminate() 假装没反应（用于验证升级 kill）。
        """
        self.command = list(cmd)
        self.pid = pid
        self.returncode = None
        self.calls = {"communicate": 0, "terminate": 0, "kill": 0, "wait": 0}
        self.alive = True
        self.outputs = []
        self.emulated = False
        self.seen_inputs = []               # 进程开始运行时输入目录里的文件名
        # 解析命令行，便于断言「一批一次进程、参数统一」
        self.input_dir = Path(self._arg("-i"))
        self.output_dir = Path(self._arg("-o"))
        self.scale = int(self._arg("-s"))
        self.noise = self._arg("-n")
        self.output_format = self._arg("-f")
        self.gpu = self._arg("-g")          # None 表示没有传 -g（auto）
        self._returncode = returncode
        self._missing = set(missing)
        self._wrong_format = set(wrong_format)
        self._wrong_size = set(wrong_size)
        self._drop_alpha = set(drop_alpha)
        self._corrupt = set(corrupt)
        self._hang = hang
        self._on_communicate = on_communicate
        self._ignore_terminate = ignore_terminate

    # --- Popen 接口 ------------------------------------------------------
    def communicate(self, timeout=None):
        self.calls["communicate"] += 1
        if self._on_communicate:
            self._on_communicate()
        if self.alive and self._hang:
            # 模拟长时间运行：在被终止之前一直超时
            raise subprocess.TimeoutExpired(self.command, timeout)
        if self.alive:
            self._emulate()
        return "", ""

    def poll(self):
        return None if self.alive else self.returncode

    def wait(self, timeout=None):
        self.calls["wait"] += 1
        if self.alive:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode

    def terminate(self):
        self.calls["terminate"] += 1
        if self._ignore_terminate > 0:
            # 模拟「收到 terminate 也不退出」，调用方应升级为 kill
            self._ignore_terminate -= 1
            return
        self._finish(-15)

    def kill(self):
        self.calls["kill"] += 1
        self._finish(-9)

    # --- 内部 ------------------------------------------------------------
    def _arg(self, flag):
        """取命令里某个开关的值；没有该开关时返回 None。"""
        if flag in self.command:
            return self.command[self.command.index(flag) + 1]
        return None

    def input_names(self):
        """批次输入目录里的文件名（模拟真实进程「看到了什么」）。"""
        if not self.input_dir.is_dir():
            return []
        return [p.name for p in sorted(self.input_dir.iterdir())]

    def _emulate(self):
        """按目录模式产出文件；被终止或故障注入时保持真实语义。"""
        self.emulated = True
        self.seen_inputs = self.input_names()      # 记下来：批次目录随后会被清理
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self._returncode == 0:
            for index, name in enumerate(self.seen_inputs):
                if index not in self._missing:
                    self._write_output(index, name)
        self._finish(self._returncode)

    def _write_output(self, index, name):
        src = self.input_dir / name
        with Image.open(src) as im:
            if index in self._wrong_size:
                size = im.size
            else:
                size = (im.width * self.scale, im.height * self.scale)
            out = im.resize(size, Image.Resampling.NEAREST)
            if index in self._drop_alpha and out.mode in ("RGBA", "LA"):
                out = out.convert("RGB")
        ext = self.output_format or Path(name).suffix.lstrip(".").lower()
        path = self.output_dir / ("%s.%s" % (Path(name).stem, ext))
        if index in self._corrupt:
            # 产物存在但内容无法读取（模拟「输出文件损坏」）
            path.write_bytes(b"not an image at all")
        elif index in self._wrong_format:
            # 名字/扩展名与预期一致，但内容其实是 PNG（模拟格式不符）
            out.save(path, "PNG")
        else:
            out.save(path, FORMAT_TO_PIL[ext])
        self.outputs.append(path)

    def _finish(self, returncode):
        self.alive = False
        self.returncode = returncode


class Waifu2xProcessRecorder:
    """记录每次 Popen 调用（每个假进程），用于断言「一批只启动一次 waifu2x」。"""

    def __init__(self, **fake_kwargs):
        self.fake_kwargs = fake_kwargs
        self.processes = []

    def __call__(self, cmd, **popen_kwargs):
        process = FakeWaifu2xProcess(cmd, **self.fake_kwargs)
        self.processes.append(process)
        return process

    @property
    def count(self):
        return len(self.processes)

    @property
    def commands(self):
        return [process.command for process in self.processes]

    def formats(self):
        return [process.output_format for process in self.processes]


@contextlib.contextmanager
def fake_exe():
    """在上下文中让 WAIFU2X_EXE 表现为「存在」。"""
    with mock.patch("upscaler.waifu2x.WAIFU2X_EXE") as exe:
        exe.exists.return_value = True
        yield exe


@contextlib.contextmanager
def fake_processes(recorder):
    """在上下文中把 upscaler.waifu2x 的 subprocess.Popen 换成记录器。"""
    with mock.patch("upscaler.waifu2x.subprocess.Popen", side_effect=recorder):
        yield recorder
