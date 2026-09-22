"""test_cancel_chain.py — 取消回调链路贯通测试（folder → 批次 → 进程 / 单图回退）。

全部用 mock，不启动真实 waifu2x-ncnn-vulkan.exe（子进程用 tests/fake_waifu2x.py
的替身，或直接替换进程执行器）。覆盖：
  - upscale_folder 把 cancel_check 一路下传到批处理进程与单图回退；
  - 直接超分（JPG/PNG/WebP）与静态 GIF / BMP 转换路径都能收到 cancel_check；
  - 转换过程中取消时临时转换文件被清理；
  - 处理中取消会抛 InterruptedError：终止当前批处理进程、不做 GPU→CPU 回退、
    不做「失败回退复制原图」、不启动后续批次、取消的图片不计入成功；
  - mock 的长跑 Popen：terminate 被调用，terminate 无效时升级 kill，且没有残留
    子进程；
  - 未提供 cancel_check 的旧式调用仍然可用。
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from upscaler.waifu2x import (
    _CANCEL_POLL_INTERVAL, _convert_and_upscale, _upscale_one, upscale_folder,
)
from upscaler.waifu2x import _run_waifu2x_process as real_run_process

try:  # python -m unittest discover -s tests ...
    from fake_waifu2x import Waifu2xProcessRecorder, fake_exe, fake_processes
except ImportError:  # python -m unittest tests.test_cancel_chain
    from tests.fake_waifu2x import Waifu2xProcessRecorder, fake_exe, fake_processes


class CancelFlag:
    """模拟 PipelineWorker 的取消标志：实例本身即 cancel_check 回调。"""

    def __init__(self, cancelled=False):
        self.cancelled = cancelled
        self.calls = 0

    def cancel(self):
        """对应 GUI 的「点击取消」：只翻转标志。"""
        self.cancelled = True

    def __call__(self):
        self.calls += 1
        return self.cancelled


class CancelAfter:
    """第 N 次检查时才返回 True（确定性模拟「处理到一半被取消」）。"""

    def __init__(self, after):
        self.after = after
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.calls >= self.after


def _fake_convert_upscale(input_path, output_path, scale=2, noise=3, gpu="auto",
                          cancel_check=None):
    """模拟 waifu2x 成功：读取输入 PNG 并写出 2 倍尺寸的 PNG（转换路径用）。"""
    with Image.open(input_path) as im:
        w, h = im.size
        out = im.convert("RGBA").resize((w * 2, h * 2), Image.Resampling.LANCZOS)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    out.save(output_path, "PNG")
    return True


class FolderTestBase(unittest.TestCase):
    """提供 input/output 临时目录与各类运行辅助。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()
        self.output_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **kwargs):
        return upscale_folder(self.input_dir, self.output_dir, **kwargs)

    def _make_image(self, name, width=16, height=12):
        path = self.input_dir / name
        Image.new("RGB", (width, height), (200, 30, 30)).save(path)
        return path

    def _run_with_processes(self, recorder=None, **kwargs):
        """跑一次 upscale_folder，子进程换成假替身；返回 (结果, 记录器)。"""
        recorder = recorder or Waifu2xProcessRecorder()
        with fake_exe(), fake_processes(recorder):
            result = self._run(**kwargs)
        return result, recorder

    def _run_cancelled(self, recorder=None, **kwargs):
        """进程一开始运行就触发取消（确定性）；返回记录器供断言。"""
        recorder = recorder or Waifu2xProcessRecorder()
        trigger = {"flag": False}
        recorder.fake_kwargs.update(
            hang=True, on_communicate=lambda: trigger.update(flag=True))
        with self.assertRaises(InterruptedError):
            self._run_with_processes(recorder,
                                     cancel_check=lambda: trigger["flag"], **kwargs)
        return recorder

    def _leftover_batches(self):
        """工作目录下残留的批次子目录（正常应为空）。"""
        return [p.name for p in self.root.iterdir() if p.name.startswith("batch")]



class CancelForwardedTest(FolderTestBase):
    """cancel_check 必须贯通到批处理进程与单图回退，且取消不被当成失败。"""

    def test_forwarded_to_batch_process(self):
        self._make_image("a.png")
        flag = CancelFlag()
        with mock.patch("upscaler.waifu2x._run_waifu2x_process",
                        side_effect=real_run_process) as spy:
            result, recorder = self._run_with_processes(cancel_check=flag)
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(recorder.count, 1)
        self.assertIs(spy.call_args.kwargs["cancel_check"], flag)

    def test_legacy_call_without_cancel_check_still_works(self):
        self._make_image("a.png")
        with mock.patch("upscaler.waifu2x._run_waifu2x_process",
                        side_effect=real_run_process) as spy:
            result, recorder = self._run_with_processes()
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(recorder.count, 1)
        self.assertIsNone(spy.call_args.kwargs["cancel_check"])

    def test_cancel_before_first_image_raises_and_touches_nothing(self):
        self._make_image("a.png")
        recorder = Waifu2xProcessRecorder()
        with self.assertRaises(InterruptedError):
            self._run_with_processes(recorder,
                                     cancel_check=CancelFlag(cancelled=True))
        self.assertEqual(recorder.count, 0)                      # 一个进程都没启动
        self.assertEqual(list(self.output_dir.rglob("*")), [])

    def test_cancel_after_first_batch_stops_remaining_batches(self):
        self._make_image("a.jpg")
        self._make_image("b.png")

        def cancel_check():
            # 第一批（jpg，批次键排序在前）产出后「点取消」
            return (self.output_dir / "a.jpg").exists()

        recorder = Waifu2xProcessRecorder()
        with self.assertRaises(InterruptedError):
            self._run_with_processes(recorder, cancel_check=cancel_check)
        self.assertEqual(recorder.count, 1)                      # 后续批次没启动
        self.assertTrue((self.output_dir / "a.jpg").is_file())   # 第一张正常产出
        self.assertFalse((self.output_dir / "b.png").exists())   # 取消的不复制原图

    def test_cancel_during_batch_is_not_treated_as_failure(self):
        self._make_image("a.png")
        with mock.patch("upscaler.waifu2x._run_waifu2x_process",
                        side_effect=InterruptedError("用户已取消")), \
                mock.patch("upscaler.waifu2x.upscale_image") as fallback:
            with self.assertRaises(InterruptedError):
                self._run(cancel_check=CancelFlag())
        self.assertEqual(fallback.call_count, 0)                 # 取消不做失败回退
        self.assertFalse((self.output_dir / "a.png").exists())   # 不复制原图

    def test_upscale_one_forwards_cancel_check_on_convert_path(self):
        gif = self._make_image("static.gif")
        flag = CancelFlag()
        with mock.patch("upscaler.waifu2x._convert_and_upscale",
                        return_value=True) as m:
            ok = _upscale_one(gif, self.output_dir / "static.gif", 2, 3, "auto",
                              cancel_check=flag)
        self.assertTrue(ok)
        self.assertIs(m.call_args.kwargs["cancel_check"], flag)




class ConvertPathCancelTest(FolderTestBase):
    """静态 GIF / BMP 转换路径的取消行为与临时转换文件清理。"""

    def _make_static_gif(self, name="static.gif"):
        path = self.input_dir / name
        Image.new("RGB", (20, 10), (1, 2, 3)).save(path, "GIF")
        return path

    def _make_bmp(self, name="pic.bmp"):
        path = self.input_dir / name
        Image.new("RGB", (20, 10), (4, 5, 6)).save(path, "BMP")
        return path

    def _convert_dir(self, name="convert_tmp"):
        """受控的临时转换目录（替换 tempfile.mkdtemp 的返回值，便于断言清理）。"""
        path = self.root / name
        path.mkdir()
        return path

    def test_static_gif_convert_path_receives_cancel_check(self):
        self._make_static_gif()
        flag = CancelFlag()
        with mock.patch("upscaler.waifu2x._run_waifu2x_process",
                        side_effect=real_run_process) as spy:
            result, recorder = self._run_with_processes(cancel_check=flag)
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(recorder.count, 1)                      # 转换后进批次
        self.assertIs(spy.call_args.kwargs["cancel_check"], flag)
        with Image.open(self.output_dir / "static.gif") as im:
            self.assertEqual(im.size, (40, 20))                  # 转回原格式且 2 倍
            self.assertEqual(im.format, "GIF")
            self.assertFalse(getattr(im, "is_animated", False))

    def test_bmp_convert_path_receives_cancel_check(self):
        self._make_bmp()
        flag = CancelFlag()
        with mock.patch("upscaler.waifu2x._run_waifu2x_process",
                        side_effect=real_run_process) as spy:
            result, recorder = self._run_with_processes(cancel_check=flag)
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(recorder.count, 1)                      # 转换后进批次
        self.assertIs(spy.call_args.kwargs["cancel_check"], flag)
        with Image.open(self.output_dir / "pic.bmp") as im:
            self.assertEqual(im.size, (40, 20))                  # 转回原格式且 2 倍
            self.assertEqual(im.format, "BMP")

    def test_cancel_inside_png_conversion_is_not_swallowed(self):
        """_convert_for_waifu2x 内的取消必须继续抛 InterruptedError，而非返回 None。"""
        gif = self._make_static_gif()
        convert_dir = self._convert_dir()
        with mock.patch("upscaler.waifu2x.tempfile.mkdtemp",
                        return_value=str(convert_dir)):
            with self.assertRaises(InterruptedError):
                _convert_and_upscale(gif, self.output_dir / "static.gif", 2, 3,
                                     "auto", cancel_check=CancelAfter(after=2))
        self.assertFalse(convert_dir.exists())
        self.assertFalse((self.output_dir / "static.gif").exists())

    def test_cancel_after_png_conversion_cleans_temp_files(self):
        gif = self._make_static_gif()
        convert_dir = self._convert_dir()
        with mock.patch("upscaler.waifu2x.tempfile.mkdtemp",
                        return_value=str(convert_dir)):
            with self.assertRaises(InterruptedError):
                _convert_and_upscale(gif, self.output_dir / "static.gif", 2, 3,
                                     "auto", cancel_check=CancelAfter(after=3))
        self.assertFalse(convert_dir.exists())
        self.assertFalse((self.output_dir / "static.gif").exists())

    def test_cancel_during_upscale_cleans_temp_files(self):
        gif = self._make_static_gif()
        convert_dir = self._convert_dir()
        with mock.patch("upscaler.waifu2x.tempfile.mkdtemp",
                        return_value=str(convert_dir)), \
             mock.patch("upscaler.waifu2x.upscale_image",
                        side_effect=InterruptedError("用户已取消")):
            with self.assertRaises(InterruptedError):
                _convert_and_upscale(gif, self.output_dir / "static.gif", 2, 3,
                                     "auto", cancel_check=CancelFlag())
        self.assertFalse(convert_dir.exists())
        self.assertFalse((self.output_dir / "static.gif").exists())

    def test_cancel_before_convert_back_leaves_no_half_done_file(self):
        gif = self._make_static_gif()
        convert_dir = self._convert_dir()
        with mock.patch("upscaler.waifu2x.tempfile.mkdtemp",
                        return_value=str(convert_dir)), \
             mock.patch("upscaler.waifu2x.upscale_image",
                        side_effect=_fake_convert_upscale):
            with self.assertRaises(InterruptedError):
                _convert_and_upscale(gif, self.output_dir / "static.gif", 2, 3,
                                     "auto", cancel_check=CancelAfter(after=4))
        self.assertFalse(convert_dir.exists())
        self.assertFalse((self.output_dir / "static.gif").exists())

    def test_folder_cancel_in_convert_path_cleans_temp_and_skips_copy(self):
        self._make_static_gif()
        recorder = Waifu2xProcessRecorder()
        with mock.patch("upscaler.waifu2x._convert_for_waifu2x",
                        side_effect=InterruptedError("用户已取消")):
            with self.assertRaises(InterruptedError):
                self._run_with_processes(recorder, cancel_check=CancelFlag())
        self.assertEqual(recorder.count, 0)                      # 转换阶段就取消了
        self.assertFalse((self.output_dir / "static.gif").exists())
        self.assertEqual(self._leftover_batches(), [])           # 批次目录已清理


class LongRunningSubprocessCancelTest(FolderTestBase):
    """mock 的长跑批处理进程：取消要立即终止它，且不做任何回退。"""

    def test_cancel_terminates_current_batch_process(self):
        self._make_image("a.png")
        recorder = self._run_cancelled()
        self.assertEqual(recorder.count, 1)                      # 只启动了当前这一批
        process = recorder.processes[0]
        self.assertEqual(process.calls["terminate"], 1)           # 先正常终止
        self.assertEqual(process.calls["kill"], 0)                # 无需强杀
        self.assertFalse(process.alive)                           # 无残留子进程
        self.assertFalse((self.output_dir / "a.png").exists())    # 不做失败回退复制
        self.assertEqual(self._leftover_batches(), [])            # 批次目录已清理

    def test_cancel_skips_cpu_fallback_and_later_processes(self):
        self._make_image("a.png")
        self._make_image("b.jpg")
        with mock.patch("upscaler.waifu2x.upscale_image") as fallback:
            recorder = self._run_cancelled()
        self.assertEqual(recorder.count, 1)                      # 无重试、无后续批次
        self.assertEqual(fallback.call_count, 0)                 # 不做单图失败回退
        self.assertEqual(list(self.output_dir.rglob("*")), [])

    def test_kill_escalation_when_terminate_is_ignored(self):
        self._make_image("a.png")
        recorder = Waifu2xProcessRecorder(ignore_terminate=1)
        self._run_cancelled(recorder)
        process = recorder.processes[0]
        self.assertEqual(process.calls["terminate"], 1)
        self.assertEqual(process.calls["kill"], 1)                # 超时后强制终止
        self.assertFalse(process.alive)

    def test_flag_flipped_while_process_runs_is_honoured(self):
        """模拟 PipelineWorker.cancel()：子进程运行期间由其它线程翻转标志。"""
        self._make_image("a.png")
        flag = CancelFlag()
        recorder = Waifu2xProcessRecorder(hang=True)
        timer = threading.Timer(0.05, flag.cancel)
        timer.start()
        start = time.time()
        try:
            with self.assertRaises(InterruptedError):
                self._run_with_processes(recorder, gpu="auto", cancel_check=flag)
        finally:
            timer.cancel()
        elapsed = time.time() - start
        process = recorder.processes[0]
        self.assertEqual(process.calls["terminate"], 1)
        self.assertFalse(process.alive)
        # 轮询周期内就会响应取消，不会等到子进程自己结束
        self.assertLess(elapsed, 5 * _CANCEL_POLL_INTERVAL)


if __name__ == "__main__":
    unittest.main()

