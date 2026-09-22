"""test_process_runner.py — waifu2x 子进程执行器（subprocess.Popen）行为测试。

全部使用假的 Popen 替身，不真正启动 waifu2x-ncnn-vulkan.exe，覆盖：
  - 正常退出时记录 returncode / stdout / stderr；
  - 非零 returncode 不抛异常，交给上层判定失败；
  - GPU 失败最多回退 CPU(-1) 一次，绝不无限重试；
  - 运行期间周期性检查 cancel_check，取消时先 terminate()、超时再 kill()，
    只终止当前 Popen 对象，并抛 InterruptedError。
"""

import subprocess
import unittest
from unittest import mock

from errors import PipelineError
from upscaler.waifu2x import _run_waifu2x_process, upscale_image


class FakeProcess:
    """最小 Popen 替身：可配置自然退出时机，以及忽略 terminate/kill 的次数。"""

    def __init__(self, returncode=0, stdout="", stderr="", pid=4321,
                 running_polls=0, ignore_terminate=0, ignore_kill=0):
        self.returncode = None
        self.pid = pid
        self.calls = {"communicate": 0, "terminate": 0, "kill": 0,
                      "wait": 0, "poll": 0}
        self.timeouts = []          # 每次 communicate() 收到的 timeout
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = returncode
        self._running_polls = running_polls  # 还需空转多少轮才自然退出
        self._ignore_terminate = ignore_terminate
        self._ignore_kill = ignore_kill
        self._running = True

    # --- Popen 接口 ------------------------------------------------------
    def communicate(self, timeout=None):
        self.calls["communicate"] += 1
        self.timeouts.append(timeout)
        if self._running:
            if self._running_polls > 0:
                self._running_polls -= 1
                raise subprocess.TimeoutExpired("waifu2x", timeout)
            self._finish(self._exit_code)
        return self._stdout, self._stderr

    def poll(self):
        self.calls["poll"] += 1
        return self.returncode if not self._running else None

    def wait(self, timeout=None):
        self.calls["wait"] += 1
        if self._running:
            raise subprocess.TimeoutExpired("waifu2x", timeout)
        return self.returncode

    def terminate(self):
        self.calls["terminate"] += 1
        if self._ignore_terminate > 0:
            self._ignore_terminate -= 1
            return
        self._finish(-15)

    def kill(self):
        self.calls["kill"] += 1
        if self._ignore_kill > 0:
            self._ignore_kill -= 1
            return
        self._finish(-9)

    def _finish(self, returncode):
        self._running = False
        self.returncode = returncode


class RunWaifu2xProcessTest(unittest.TestCase):
    """_run_waifu2x_process：Popen 执行 + returncode/stdout/stderr 记录。"""

    CMD = ["waifu2x-ncnn-vulkan.exe", "-i", "in.png", "-o", "out.png"]

    def test_success_records_returncode_stdout_stderr(self):
        proc = FakeProcess(returncode=0, stdout="done\n", stderr="1.00%\n")
        with mock.patch("upscaler.waifu2x.subprocess.Popen",
                        return_value=proc) as popen:
            result = _run_waifu2x_process(self.CMD)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "done\n")
        self.assertEqual(result.stderr, "1.00%\n")
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args[0][0], self.CMD)
        self.assertEqual(proc.calls["terminate"], 0)
        self.assertEqual(proc.calls["kill"], 0)

    def test_output_pipes_and_text_mode(self):
        proc = FakeProcess(returncode=0)
        with mock.patch("upscaler.waifu2x.subprocess.Popen",
                        return_value=proc) as popen:
            _run_waifu2x_process(self.CMD)
        kwargs = popen.call_args[1]
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        self.assertTrue(kwargs["text"])

    def test_nonzero_returncode_returned_not_raised(self):
        proc = FakeProcess(returncode=3, stderr="boom\n")
        with mock.patch("upscaler.waifu2x.subprocess.Popen", return_value=proc):
            result = _run_waifu2x_process(self.CMD)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, "boom\n")
        self.assertEqual(proc.calls["terminate"], 0)

    def test_without_cancel_check_blocks_instead_of_polling(self):
        proc = FakeProcess(returncode=0)
        with mock.patch("upscaler.waifu2x.subprocess.Popen", return_value=proc):
            _run_waifu2x_process(self.CMD)
        self.assertEqual(proc.timeouts, [None])           # 不轮询
        self.assertEqual(proc.calls["communicate"], 1)

    def test_checks_cancel_periodically_while_running(self):
        proc = FakeProcess(returncode=0, running_polls=2)
        checks = []

        def cancel_check():
            checks.append(True)
            return False                                  # 一直不取消

        with mock.patch("upscaler.waifu2x.subprocess.Popen", return_value=proc):
            result = _run_waifu2x_process(self.CMD, cancel_check=cancel_check)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(checks), 2)                  # 每轮超时后检查一次
        self.assertEqual(proc.calls["terminate"], 0)


class CancelProcessTest(unittest.TestCase):
    """取消语义：只终止当前进程，先 terminate()，超时再 kill()，抛 InterruptedError。"""

    CMD = ["waifu2x-ncnn-vulkan.exe", "-i", "in.png", "-o", "out.png"]

    def test_cancel_terminates_only_current_process(self):
        running = FakeProcess(running_polls=1000, stdout="part", stderr="part")
        later = FakeProcess()
        with mock.patch("upscaler.waifu2x.subprocess.Popen",
                        side_effect=[running, later]) as popen:
            with self.assertRaises(InterruptedError):
                _run_waifu2x_process(self.CMD, cancel_check=lambda: True)
        self.assertEqual(popen.call_count, 1)             # 只启动了当前这一个进程
        self.assertEqual(running.calls["terminate"], 1)   # 先正常终止
        self.assertEqual(running.calls["kill"], 0)        # 已退出，无需强杀
        self.assertEqual(running.returncode, -15)
        self.assertGreaterEqual(running.calls["communicate"], 2)  # 残留输出读干净
        self.assertEqual(later.calls["terminate"], 0)     # 另一个进程不受影响
        self.assertEqual(later.calls["communicate"], 0)

    def test_cancel_escalates_to_kill_when_terminate_ignored(self):
        proc = FakeProcess(running_polls=1000, ignore_terminate=1)
        with mock.patch("upscaler.waifu2x.subprocess.Popen", return_value=proc):
            with self.assertRaises(InterruptedError):
                _run_waifu2x_process(self.CMD, cancel_check=lambda: True)
        self.assertEqual(proc.calls["terminate"], 1)
        self.assertEqual(proc.calls["kill"], 1)           # 超时后强制终止
        self.assertEqual(proc.returncode, -9)

    def test_not_cancelled_runs_to_completion(self):
        proc = FakeProcess(returncode=0, running_polls=1)
        with mock.patch("upscaler.waifu2x.subprocess.Popen", return_value=proc):
            result = _run_waifu2x_process(self.CMD, cancel_check=lambda: False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(proc.calls["terminate"], 0)
        self.assertEqual(proc.calls["kill"], 0)

    def test_unexpected_error_still_reaps_process(self):
        proc = FakeProcess(running_polls=1000)

        def cancel_check():
            raise RuntimeError("cancel_check 自身出错")

        with mock.patch("upscaler.waifu2x.subprocess.Popen", return_value=proc):
            with self.assertRaises(RuntimeError):
                _run_waifu2x_process(self.CMD, cancel_check=cancel_check)
        self.assertEqual(proc.calls["terminate"], 1)      # 不留孤儿进程


class UpscaleImageProcessTest(unittest.TestCase):
    """upscale_image 走 Popen 后的行为：成功、失败、GPU 回退一次、取消。"""

    def _run(self, processes, gpu="auto", cancel_check=None):
        with mock.patch("upscaler.waifu2x.WAIFU2X_EXE") as exe, \
             mock.patch("upscaler.waifu2x.subprocess.Popen",
                        side_effect=processes) as popen:
            exe.exists.return_value = True
            ok = upscale_image("in.png", "out.png", scale=2, noise=3, gpu=gpu,
                               cancel_check=cancel_check)
            return ok, popen

    def test_success_returns_true_with_single_attempt(self):
        ok, popen = self._run([FakeProcess(returncode=0)])
        self.assertTrue(ok)
        self.assertEqual(popen.call_count, 1)

    def test_nonzero_returncode_on_cpu_returns_false(self):
        ok, popen = self._run([FakeProcess(returncode=1, stderr="error\n")], gpu=-1)
        self.assertFalse(ok)
        self.assertEqual(popen.call_count, 1)             # 用户指定 CPU，不再回退

    def test_gpu_failure_falls_back_to_cpu_once(self):
        ok, popen = self._run([FakeProcess(returncode=1, stderr="gpu fail\n"),
                               FakeProcess(returncode=0)])
        self.assertTrue(ok)
        self.assertEqual(popen.call_count, 2)
        first = popen.call_args_list[0][0][0]
        second = popen.call_args_list[1][0][0]
        self.assertNotIn("-g", first)                     # auto：不传 -g
        self.assertEqual(second[second.index("-g") + 1], "-1")

    def test_manual_gpu_failure_falls_back_to_cpu_once(self):
        ok, popen = self._run([FakeProcess(returncode=1),
                               FakeProcess(returncode=0)], gpu=1)
        self.assertTrue(ok)
        self.assertEqual(popen.call_count, 2)
        first = popen.call_args_list[0][0][0]
        second = popen.call_args_list[1][0][0]
        self.assertEqual(first[first.index("-g") + 1], "1")
        self.assertEqual(second[second.index("-g") + 1], "-1")

    def test_gpu_and_cpu_failure_does_not_retry_forever(self):
        ok, popen = self._run([FakeProcess(returncode=1, stderr="boom\n"),
                               FakeProcess(returncode=1, stderr="boom again\n")])
        self.assertFalse(ok)
        self.assertEqual(popen.call_count, 2)             # 最多两次，绝不无限重试

    def test_vulkan_failure_raises_pipeline_error(self):
        proc = FakeProcess(returncode=1, stderr="vkCreateInstance failed\n")
        with self.assertRaises(PipelineError) as ctx:
            self._run([proc], gpu=-1)
        self.assertEqual(ctx.exception.kind, "no_vulkan")

    def test_missing_exe_raises_pipeline_error(self):
        with mock.patch("upscaler.waifu2x.WAIFU2X_EXE") as exe:
            exe.exists.return_value = False
            with self.assertRaises(PipelineError) as ctx:
                upscale_image("in.png", "out.png")
        self.assertEqual(ctx.exception.kind, "missing_waifu2x")

    def test_cancel_terminates_and_skips_cpu_fallback(self):
        proc = FakeProcess(running_polls=1000)
        with mock.patch("upscaler.waifu2x.WAIFU2X_EXE") as exe, \
             mock.patch("upscaler.waifu2x.subprocess.Popen",
                        return_value=proc) as popen:
            exe.exists.return_value = True
            with self.assertRaises(InterruptedError):
                upscale_image("in.png", "out.png", gpu="auto",
                              cancel_check=lambda: True)
        self.assertEqual(popen.call_count, 1)             # 取消后不再回退 CPU
        self.assertEqual(proc.calls["terminate"], 1)
        self.assertEqual(proc.calls["kill"], 0)

    def test_positional_call_signature_still_works(self):
        """老调用方式（位置参数、不带 cancel_check）必须保持兼容。"""
        with mock.patch("upscaler.waifu2x.WAIFU2X_EXE") as exe, \
             mock.patch("upscaler.waifu2x.subprocess.Popen",
                        return_value=FakeProcess(returncode=0)) as popen:
            exe.exists.return_value = True
            self.assertTrue(upscale_image("in.png", "out.png", 4, 1, "auto"))
        cmd = popen.call_args[0][0]
        self.assertIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "4")
        self.assertEqual(cmd[cmd.index("-n") + 1], "1")


if __name__ == "__main__":
    unittest.main()

