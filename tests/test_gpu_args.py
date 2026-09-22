"""test_gpu_args.py — waifu2x GPU 参数统一生成函数与命令行构造。"""

import unittest
from unittest import mock

from upscaler.waifu2x import gpu_args, upscale_image


class GpuArgsTest(unittest.TestCase):
    def test_auto_has_no_g_flag(self):
        self.assertEqual(gpu_args("auto"), [])

    def test_cpu_is_neg_one(self):
        self.assertEqual(gpu_args(-1), ["-g", "-1"])

    def test_manual_gpu_id(self):
        self.assertEqual(gpu_args(0), ["-g", "0"])
        self.assertEqual(gpu_args(1), ["-g", "1"])
        self.assertEqual(gpu_args(2), ["-g", "2"])

    def test_gpu_id_above_two(self):
        self.assertEqual(gpu_args(5), ["-g", "5"])
        self.assertEqual(gpu_args(42), ["-g", "42"])


def _fake_popen(returncode=0, stdout="", stderr=""):
    """最小 Popen 替身：communicate() 直接返回结果，不真正启动子进程。"""
    proc = mock.Mock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


class UpscaleImageCommandTest(unittest.TestCase):
    """upscale_image 的命令行构造（subprocess.Popen 全部 mock，不真正执行）。"""

    def _run_command(self, gpu):
        with mock.patch("upscaler.waifu2x.WAIFU2X_EXE") as exe, \
             mock.patch("upscaler.waifu2x.subprocess.Popen",
                        return_value=_fake_popen()) as popen:
            exe.exists.return_value = True
            upscale_image("in.png", "out.png", scale=2, noise=3, gpu=gpu)
            return popen.call_args[0][0]

    def test_auto_omits_g_flag(self):
        cmd = self._run_command("auto")
        self.assertNotIn("-g", cmd)
        self.assertNotIn("auto", cmd)

    def test_cpu_includes_neg_one(self):
        cmd = self._run_command(-1)
        idx = cmd.index("-g")
        self.assertEqual(cmd[idx + 1], "-1")

    def test_manual_gpu_includes_id(self):
        for gpu_id in (0, 1, 2, 5):
            cmd = self._run_command(gpu_id)
            idx = cmd.index("-g")
            self.assertEqual(cmd[idx + 1], str(gpu_id))


if __name__ == "__main__":
    unittest.main()
