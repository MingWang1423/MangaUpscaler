"""test_batch_pipeline.py — 批处理接入 upscale_folder 的端到端（mock）测试。

只 mock waifu2x 子进程（tests/fake_waifu2x.py），不启动真实 exe。覆盖：
  - upscale_folder 真正调用 batch_planner.plan_batches；
  - 同参图片只启动一次 waifu2x 进程（20 张图片的性能验证）；
  - 不同倍率 / noise / 格式分别启动不同批次；
  - 批次临时输入名、嵌套目录与同名图片的相对路径还原；
  - JPG / PNG / WebP / BMP / 静态 GIF 的格式与尺寸正确；
  - SVG、多帧 GIF 不进入 waifu2x；
  - 批次失败（非零 / 产物缺失 / 格式不符 / 尺寸异常 / 丢透明通道）逐张回退；
  - 取消终止当前批处理进程、不触发 CPU 回退、不复制原图、不启动后续批次；
  - 进度只回调一次且推进到 total；四元组统计保持兼容。
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from upscaler.batch_planner import plan_batches as real_plan_batches
from upscaler.waifu2x import _CANCEL_POLL_INTERVAL, upscale_folder

try:  # python -m unittest discover -s tests ...
    from fake_waifu2x import Waifu2xProcessRecorder, fake_exe, fake_processes
except ImportError:  # python -m unittest tests.test_batch_pipeline
    from tests.fake_waifu2x import Waifu2xProcessRecorder, fake_exe, fake_processes


class BatchPipelineTestBase(unittest.TestCase):
    """GUI 同款目录布局：<任务工作目录>/extracted 与 /upscaled。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "extracted"
        self.output_dir = self.root / "upscaled"
        self.input_dir.mkdir()
        self.output_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, rel, size=(40, 20), color=(10, 20, 30)):
        path = self.input_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path)
        return path

    def _make_alpha(self, rel, size=(24, 12)):
        path = self.input_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", size, (0, 0, 0, 0)).save(path)
        return path

    def _make_animated_gif(self, rel="anim.gif"):
        path = self.input_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        frames = [Image.new("RGB", (40, 40), (255, 0, 0)),
                  Image.new("RGB", (40, 40), (0, 255, 0))]
        frames[0].save(path, save_all=True, append_images=frames[1:], format="GIF",
                       duration=100, loop=0)
        return path

    def _make_svg(self, rel="img.svg"):
        path = self.input_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
        return path

    def _make_broken(self, rel="broken.jpg"):
        path = self.input_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a real jpeg")
        return path

    def _run(self, recorder=None, progress=None, **kwargs):
        """跑一次 upscale_folder；返回 (结果, 进程记录器, 进度回调记录)。"""
        recorder = recorder or Waifu2xProcessRecorder()
        progress = [] if progress is None else progress
        with fake_exe(), fake_processes(recorder):
            result = upscale_folder(
                self.input_dir, self.output_dir,
                progress_callback=lambda done, total: progress.append((done, total)),
                **kwargs)
        return result, recorder, progress

    def _size(self, rel, output=True):
        """读取产物尺寸（output=False 时读原图）。"""
        base = self.output_dir if output else self.input_dir
        with Image.open(base / rel) as im:
            return im.size, (im.format or "").lower()


class PlannerIntegrationTest(BatchPipelineTestBase):
    """upscale_folder 必须真正使用 batch_planner 的规划结果。"""

    def test_upscale_folder_calls_plan_batches(self):
        self._make("a.png")
        recorder = Waifu2xProcessRecorder()
        with mock.patch("upscaler.waifu2x.plan_batches",
                        side_effect=real_plan_batches) as spy, \
                fake_exe(), fake_processes(recorder):
            upscale_folder(self.input_dir, self.output_dir, scale=2, noise=1,
                           gpu=0, target=(2160, 3840))
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(Path(spy.call_args.args[0]), self.input_dir)
        self.assertEqual(spy.call_args.kwargs["scale"], 2)
        self.assertEqual(spy.call_args.kwargs["noise"], 1)
        self.assertEqual(spy.call_args.kwargs["gpu"], 0)
        self.assertEqual(spy.call_args.kwargs["target"], (2160, 3840))

    def test_batch_only_uses_workspace_root(self):
        """批次临时目录建在「当前任务工作目录」下，绝不在项目根目录。"""
        self._make("a.png")
        result, recorder, _ = self._run()
        self.assertEqual(result, (1, 0, 0, 0))
        process = recorder.processes[0]
        batch_dir = process.input_dir.parent
        # 同一目录在 Windows 上可能表现为长路径（_batch_root_for 的 resolve()）或
        # 8.3 短路径（RUNNER~1），字符串比较会误判；用 samefile 按文件系统对象
        # 比较，语义仍是「批次临时目录建在当前任务工作目录下」。
        self.assertTrue(os.path.samefile(batch_dir.parent, self.root),
                        f"{batch_dir.parent} 不在任务工作目录 {self.root} 下")
        self.assertEqual(process.input_dir.name, "input")
        self.assertEqual(process.output_dir.name, "output")
        self.assertTrue(process.input_dir.is_absolute())
        self.assertEqual(process.calls["communicate"], 1)
        # 批次结束后目录被清理，工作目录本身保留
        self.assertEqual([p.name for p in self.root.iterdir()
                          if p.name.startswith("batch")], [])

    def test_batch_root_override_is_respected(self):
        self._make("a.png")
        batch_root = self.root / "custom_batches"
        batch_root.mkdir()
        result, recorder, _ = self._run(batch_root=batch_root)
        self.assertEqual(result, (1, 0, 0, 0))
        process = recorder.processes[0]
        self.assertEqual(process.input_dir.parent.parent, batch_root)
        self.assertTrue(batch_root.exists())                    # 调用方给的目录不动
        self.assertEqual(list(batch_root.iterdir()), [])        # 但批次子目录被清理

    def test_batch_command_shape_and_gpu_args(self):
        self._make("a.png")
        for gpu, expected in (("auto", None), (-1, "-1"), (2, "2")):
            recorder = Waifu2xProcessRecorder()
            result, recorder, _ = self._run(recorder=recorder, gpu=gpu, scale=4,
                                            noise=2)
            self.assertEqual(result, (1, 0, 0, 0))
            process = recorder.processes[0]
            self.assertEqual(process.gpu, expected)             # auto 不传 -g
            self.assertEqual(process.scale, 4)
            self.assertEqual(process.noise, "2")
            self.assertEqual(process.output_format, "png")
            self.assertEqual(len(process.seen_inputs), 1)       # 目录输入，1 个文件
            self.assertTrue(process.seen_inputs[0].startswith("w2x_"))
            self.assertTrue(process.input_dir.is_absolute())
            self.assertTrue(process.output_dir.is_absolute())   # 目录输出


class ProcessCountTest(BatchPipelineTestBase):
    """批次划分与进程启动次数。"""

    def test_same_parameters_start_one_process_for_many_images(self):
        for index in range(20):
            self._make("page_%02d.png" % index)
        result, recorder, progress = self._run()
        self.assertEqual(result, (20, 0, 0, 0))                # 全部成功
        self.assertEqual(recorder.count, 1)                    # 20 张只启动 1 次
        self.assertLess(recorder.count, 20)
        self.assertEqual(len(recorder.processes[0].seen_inputs), 20)
        self.assertEqual(progress[-1], (20, 20))

    def test_different_batches_start_separate_processes(self):
        for index in range(20):
            self._make("page_%02d.png" % index)
        self._make("cover.jpg")
        self._make("pic.bmp", size=(60, 30))
        result, recorder, _ = self._run()
        self.assertEqual(result, (22, 0, 0, 0))
        self.assertEqual(recorder.count, 3)                     # png / jpg / bmp
        self.assertLess(recorder.count, 22)
        self.assertEqual(sorted(recorder.formats()), ["jpg", "png", "png"])

    def test_different_scale_and_noise_get_distinct_batches(self):
        self._make("medium.png", size=(1200, 1800))             # 目标下 2× 够
        self._make("small.png", size=(800, 1200))               # 目标下需要 4×
        result, recorder, _ = self._run(scale=4, target=(2160, 3840))
        self.assertEqual(result, (2, 0, 0, 0))
        self.assertEqual(recorder.count, 2)
        self.assertEqual(sorted(p.scale for p in recorder.processes), [2, 4])

        # 不同 noise：同参图片仍是一批，但整批用新的 noise 重新起进程
        recorder = Waifu2xProcessRecorder()
        result, recorder, _ = self._run(recorder=recorder, noise=0)
        self.assertEqual(result, (2, 0, 0, 0))
        self.assertEqual(recorder.count, 1)
        self.assertEqual(recorder.processes[0].noise, "0")

    def test_same_basename_in_nested_dirs_shares_batch_without_collision(self):
        self._make("dir-a/page.png", size=(40, 20), color=(1, 1, 1))
        self._make("dir-b/page.png", size=(80, 20), color=(2, 2, 2))
        result, recorder, _ = self._run()
        self.assertEqual(result, (2, 0, 0, 0))
        self.assertEqual(recorder.count, 1)
        names = recorder.processes[0].seen_inputs
        self.assertEqual(len(names), 2)                          # 同名但都准备了
        self.assertEqual(len(set(names)), 2)                     # 且互不覆盖
        self.assertTrue(all(name.startswith("w2x_") for name in names))
        self.assertEqual(self._size("dir-a/page.png")[0], (80, 40))
        self.assertEqual(self._size("dir-b/page.png")[0], (160, 40))



class FormatAndPathTest(BatchPipelineTestBase):
    """格式保持与「按临时名映射还原原始相对路径」。"""

    def test_jpg_png_webp_outputs_are_real_formats(self):
        self._make("a.png", size=(40, 20))
        self._make("b.jpg", size=(50, 30))
        self._make("c.webp", size=(60, 40))
        result, recorder, _ = self._run()
        self.assertEqual(result, (3, 0, 0, 0))
        self.assertEqual(recorder.count, 3)
        self.assertEqual(self._size("a.png"), ((80, 40), "png"))
        self.assertEqual(self._size("b.jpg"), ((100, 60), "jpeg"))
        self.assertEqual(self._size("c.webp"), ((120, 80), "webp"))

    def test_static_gif_and_bmp_are_converted_back(self):
        self._make("pic.bmp", size=(60, 30))
        self._make("static.gif", size=(50, 25))
        result, recorder, _ = self._run()
        self.assertEqual(result, (2, 0, 0, 0))
        # 两张都先转 PNG 再进批次，最终再转回原格式
        self.assertEqual(recorder.count, 2)
        self.assertEqual(sorted(recorder.formats()), ["png", "png"])
        self.assertEqual(self._size("pic.bmp"), ((120, 60), "bmp"))
        self.assertEqual(self._size("static.gif"), ((100, 50), "gif"))
        with Image.open(self.output_dir / "static.gif") as im:
            self.assertFalse(getattr(im, "is_animated", False))

    def test_png_alpha_is_kept(self):
        self._make_alpha("alpha.png")
        result, recorder, _ = self._run()
        self.assertEqual(result, (1, 0, 0, 0))
        with Image.open(self.output_dir / "alpha.png") as im:
            self.assertEqual(im.mode, "RGBA")
            self.assertEqual(im.size, (48, 24))

    def test_nested_paths_and_names_are_restored(self):
        self._make("OEBPS/Images/Chapter 1/p1.png", size=(20, 10))
        self._make("OEBPS/Images/Chapter 2/p1.png", size=(30, 15))
        self._make("OEBPS/Text/cover.png", size=(40, 20))
        result, recorder, _ = self._run()
        self.assertEqual(result, (3, 0, 0, 0))
        self.assertEqual(recorder.count, 1)                     # 参数相同 → 同批
        self.assertEqual(self._size("OEBPS/Images/Chapter 1/p1.png")[0], (40, 20))
        self.assertEqual(self._size("OEBPS/Images/Chapter 2/p1.png")[0], (60, 30))
        self.assertEqual(self._size("OEBPS/Text/cover.png")[0], (80, 40))
        self.assertEqual(sorted(p.name for p in self.output_dir.rglob("*.png")),
                         ["cover.png", "p1.png", "p1.png"])

    def test_svg_and_animated_gif_never_start_a_process(self):
        self._make_svg("img.svg")
        self._make_animated_gif("anim.gif")
        self._make("page.png")
        svg_before = (self.input_dir / "img.svg").read_bytes()
        gif_before = (self.input_dir / "anim.gif").read_bytes()
        result, recorder, progress = self._run()
        self.assertEqual(result, (1, 0, 2, 0))                  # 1 超分 + 2 原样复制
        self.assertEqual(recorder.count, 1)                     # 只有 png 那批
        self.assertTrue(all(name.endswith(".png")
                            for name in recorder.processes[0].input_names()))
        self.assertEqual((self.output_dir / "img.svg").read_bytes(), svg_before)
        self.assertEqual((self.output_dir / "anim.gif").read_bytes(), gif_before)
        self.assertEqual(progress[-1], (3, 3))

    def test_unreadable_image_counted_failed_and_copied(self):
        self._make_broken("broken.jpg")
        result, recorder, _ = self._run()
        self.assertEqual(result, (0, 0, 0, 1))
        self.assertEqual(recorder.count, 0)                     # 不进 any 批次
        self.assertEqual((self.output_dir / "broken.jpg").read_bytes(),
                         b"not a real jpeg")

    def test_inputs_are_never_modified(self):
        self._make("a.png")
        self._make("pic.bmp", size=(60, 30))
        self._make("static.gif", size=(50, 25))
        before = {p.relative_to(self.input_dir).as_posix(): p.read_bytes()
                  for p in self.input_dir.rglob("*") if p.is_file()}
        self._run()
        after = {p.relative_to(self.input_dir).as_posix(): p.read_bytes()
                 for p in self.input_dir.rglob("*") if p.is_file()}
        self.assertEqual(after, before)



class BatchFallbackTest(BatchPipelineTestBase):
    """批次失败 / 产物不合格时必须逐张回退单图处理，绝不整批算成功。"""

    def _fallback_patch(self, ok=True, writer=None):
        """替换单图处理入口，记录调用参数（回退路径用）。"""
        def _fake(input_path, output_path, scale=2, noise=3, gpu="auto",
                  cancel_check=None):
            if writer is not None:
                writer(input_path, output_path)
            return ok
        return mock.patch("upscaler.waifu2x.upscale_image", side_effect=_fake)

    def _write_dest(self, input_path, output_path):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(input_path) as im:
            im.resize((im.width * 2, im.height * 2)).save(path)

    def test_missing_output_falls_back_per_image(self):
        self._make("a.png", size=(40, 20))
        self._make("b.png", size=(60, 30))
        recorder = Waifu2xProcessRecorder(missing=(0,))
        with self._fallback_patch(ok=True, writer=self._write_dest) as fallback:
            result, recorder, _ = self._run(recorder=recorder)
        self.assertEqual(recorder.count, 1)
        self.assertEqual(fallback.call_count, 1)                # 只有缺失的那张回退
        self.assertEqual(result, (2, 0, 0, 0))                  # 回退成功仍算成功
        self.assertEqual(sorted([self._size("a.png")[0], self._size("b.png")[0]]),
                         [(80, 40), (120, 60)])

    def test_batch_returncode_nonzero_not_counted_as_success(self):
        self._make("a.png")
        self._make("b.png")
        recorder = Waifu2xProcessRecorder(returncode=1)
        with self._fallback_patch(ok=False) as fallback:
            result, recorder, _ = self._run(recorder=recorder)
        self.assertEqual(recorder.count, 1)
        self.assertEqual(fallback.call_count, 2)                # 逐张回退
        self.assertEqual(result, (0, 0, 0, 2))                  # 不算成功，计入失败
        # 失败回退时原图被保留（沿用既有语义），且不产生半成品
        self.assertEqual((self.output_dir / "a.png").read_bytes(),
                         (self.input_dir / "a.png").read_bytes())

    def test_wrong_format_or_size_output_falls_back(self):
        # 用 JPG 批次：wrong_format 会把 PNG 内容写进 .jpg 名字里，触发格式校验
        self._make("a.jpg", size=(40, 20))
        self._make("b.jpg", size=(60, 30))
        recorder = Waifu2xProcessRecorder(wrong_format=(0,), wrong_size=(1,))
        with self._fallback_patch(ok=True, writer=self._write_dest) as fallback:
            result, recorder, _ = self._run(recorder=recorder)
        self.assertEqual(recorder.count, 1)
        self.assertEqual(fallback.call_count, 2)
        self.assertEqual(result, (2, 0, 0, 0))
        self.assertEqual(self._size("a.jpg"), ((80, 40), "jpeg"))
        self.assertEqual(self._size("b.jpg"), ((120, 60), "jpeg"))

    def test_unreadable_output_falls_back_per_image(self):
        self._make("a.png", size=(40, 20))
        recorder = Waifu2xProcessRecorder(corrupt=(0,))
        with self._fallback_patch(ok=True, writer=self._write_dest) as fallback:
            result, recorder, _ = self._run(recorder=recorder)
        self.assertEqual(recorder.count, 1)
        self.assertEqual(fallback.call_count, 1)
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(self._size("a.png")[0], (80, 40))

    def test_alpha_loss_falls_back_per_image(self):
        self._make_alpha("alpha.png")
        recorder = Waifu2xProcessRecorder(drop_alpha=(0,))
        with self._fallback_patch(ok=True, writer=self._write_dest) as fallback:
            result, recorder, _ = self._run(recorder=recorder)
        self.assertEqual(recorder.count, 1)
        self.assertEqual(fallback.call_count, 1)                # 目录模式丢了 alpha
        self.assertEqual(result, (1, 0, 0, 0))

    def test_single_failure_does_not_block_other_images(self):
        self._make("a.png", size=(40, 20))
        self._make("b.png", size=(60, 30))
        recorder = Waifu2xProcessRecorder(missing=(1,))
        with self._fallback_patch(ok=False) as fallback:
            result, recorder, _ = self._run(recorder=recorder)
        self.assertEqual(fallback.call_count, 1)
        self.assertEqual(result, (1, 0, 0, 1))                  # 一张成功一张失败
        # 一张是 2 倍放大，另一张原样保留（不依赖具体是哪一张失败）
        scaled, copied = [], []
        for name, original in (("a.png", (40, 20)), ("b.png", (60, 30))):
            size = self._size(name)[0]
            if size == (original[0] * 2, original[1] * 2):
                scaled.append(name)
            elif size == original:
                copied.append(name)
        self.assertEqual((len(scaled), len(copied)), (1, 1))
        for name in ("a.png", "b.png"):
            self.assertTrue((self.output_dir / name).is_file())

    def test_fallback_passes_cancel_check_and_scaled_params(self):
        self._make("a.png")

        def cancel_check():
            return False

        recorder = Waifu2xProcessRecorder(missing=(0,))
        with self._fallback_patch(ok=True, writer=self._write_dest) as fallback:
            self._run(recorder=recorder, cancel_check=cancel_check, scale=4, noise=1)
        kwargs = fallback.call_args.kwargs
        self.assertIs(kwargs["cancel_check"], cancel_check)       # 回退也带取消检查
        self.assertEqual(kwargs["scale"], 4)
        self.assertEqual(kwargs["noise"], 1)
        self.assertEqual(kwargs["gpu"], "auto")



class BatchCancelTest(BatchPipelineTestBase):
    """取消：终止当前批处理进程、不回退、不复制原图、不启动后续批次。"""

    def _run_cancelled(self, recorder, **kwargs):
        """在「进程开始运行后」触发取消（确定性触发，不依赖 sleeping 竞态）。"""
        holder = {}

        def trigger():
            holder["flag"] = True

        recorder.fake_kwargs["hang"] = True
        recorder.fake_kwargs["on_communicate"] = trigger
        with self.assertRaises(InterruptedError) as ctx:
            self._run(recorder=recorder,
                      cancel_check=lambda: holder.get("flag", False),
                      **kwargs)
        return ctx.exception

    def test_cancel_terminates_current_batch_process(self):
        self._make("a.png")
        self._make("b.png")
        recorder = Waifu2xProcessRecorder()
        self._run_cancelled(recorder)
        self.assertEqual(recorder.count, 1)                    # 只启动了当前这一批
        process = recorder.processes[0]
        self.assertEqual(process.calls["terminate"], 1)         # 先正常终止
        self.assertEqual(process.calls["kill"], 0)              # 无需强杀
        self.assertFalse(process.alive)                         # 没有残留子进程

    def test_cancel_does_not_start_cpu_fallback_or_copy_original(self):
        self._make("a.png")
        recorder = Waifu2xProcessRecorder()
        with mock.patch("upscaler.waifu2x.upscale_image") as fallback:
            self._run_cancelled(recorder)
        self.assertEqual(recorder.count, 1)                     # 无 GPU→CPU 重试
        self.assertEqual(fallback.call_count, 0)                # 不做单图失败回退
        self.assertFalse((self.output_dir / "a.png").exists())  # 不复制原图
        self.assertEqual([p.name for p in self.root.iterdir()
                          if p.name.startswith("batch")], [])   # 批次目录已清理
        self.assertTrue(self.root.exists())                     # 工作目录保留

    def test_cancel_stops_later_batches(self):
        """取消发生在第一批：后续批次不得启动。"""
        self._make("a.png")
        self._make("b.jpg")
        recorder = Waifu2xProcessRecorder()
        self._run_cancelled(recorder)
        self.assertEqual(recorder.count, 1)
        self.assertEqual(list(self.output_dir.rglob("*.jpg")), [])
        self.assertEqual(list(self.output_dir.rglob("*.png")), [])

    def test_kill_is_used_when_terminate_is_ignored(self):
        self._make("a.png")
        # terminate 之后进程仍不退出 → 必须升级为 kill
        recorder = Waifu2xProcessRecorder(ignore_terminate=1)
        self._run_cancelled(recorder)
        process = recorder.processes[0]
        self.assertEqual(process.calls["terminate"], 1)
        self.assertEqual(process.calls["kill"], 1)
        self.assertFalse(process.alive)

    def test_flag_flipped_while_process_runs_is_honoured(self):
        """模拟 PipelineWorker.cancel()：其它线程在进程运行期间翻转标志。"""
        self._make("a.png")
        flag = {"cancelled": False}
        recorder = Waifu2xProcessRecorder(hang=True)
        timer = threading.Timer(0.05, lambda: flag.update(cancelled=True))
        timer.start()
        start = time.time()
        try:
            with self.assertRaises(InterruptedError):
                self._run(recorder=recorder,
                          cancel_check=lambda: flag["cancelled"])
        finally:
            timer.cancel()
        elapsed = time.time() - start
        self.assertEqual(recorder.count, 1)
        self.assertFalse(recorder.processes[0].alive)
        self.assertLess(elapsed, 5 * _CANCEL_POLL_INTERVAL)


class ProgressAndStatsTest(BatchPipelineTestBase):
    """进度回调与四元组统计。"""

    def test_progress_reports_each_image_exactly_once(self):
        self._make("a.png")
        self._make("b.png")
        self._make("pic.bmp", size=(60, 30))
        self._make_svg("img.svg")
        self._make_animated_gif("anim.gif")
        self._make_broken("broken.jpg")
        result, _, progress = self._run()
        self.assertEqual(result, (3, 0, 2, 1))
        total = 6
        self.assertEqual(progress[0], (0, total))
        self.assertEqual(progress[-1], (total, total))
        self.assertEqual([done for done, _ in progress],
                         list(range(total + 1)))                 # 不重复、不跳号
        self.assertEqual({t for _, t in progress}, {total})

    def test_stats_tuple_order_and_legacy_positional_call(self):
        self._make("a.png", size=(5000, 3000))                  # 已达目标 → 跳过
        self._make("b.png", size=(40, 20))                      # 正常超分
        self._make_svg("img.svg")                               # 原样复制
        self._make_broken("broken.jpg")                         # 失败（原样保留）
        recorder = Waifu2xProcessRecorder()
        with fake_exe(), fake_processes(recorder):
            # 位置参数：upscale_folder(input_dir, output_dir, scale, noise)
            result = upscale_folder(self.input_dir, self.output_dir, 2, 3,
                                    None, True, None, (2160, 3840))
        self.assertEqual(result, (1, 1, 1, 1))
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 4)
        self.assertEqual(recorder.count, 1)                     # 只有 b.png 进批次

    def test_batch_images_and_copies_are_not_double_counted(self):
        for index in range(4):
            self._make("page_%d.png" % index)
        self._make_svg("img.svg")
        result, recorder, progress = self._run()
        self.assertEqual(recorder.count, 1)
        self.assertEqual(result, (4, 0, 1, 0))
        self.assertEqual(sum(result), 5)
        self.assertEqual(progress[-1], (5, 5))

