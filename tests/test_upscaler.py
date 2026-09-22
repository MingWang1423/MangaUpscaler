"""test_upscaler.py — 验证 upscale_folder 的智能倍率选择、跳过与批次编排。

不真正调用 waifu2x-ncnn-vulkan.exe：批次执行器（_run_batch）与单图回退
（upscale_image）都用 mock 替代。重点验证 target（短边/长边）下的智能倍率选择、
已达目标时跳过并字节级复制、原画质模式，以及「一批一次进程」和批次失败时逐张
回退（进程本身的行为见 test_batch_pipeline.py）。
"""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from upscaler.waifu2x import upscale_folder


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_image(dir_path: Path, name: str, width: int, height: int) -> Path:
    path = dir_path / name
    Image.new("RGB", (width, height), (200, 30, 30)).save(path)
    return path


def _fake_run_batch(fail_rels=()):
    """假的批次执行器：只汇报每张图片的结果，不启动进程。"""
    def _run(batch, batch_dir, input_path, output_path, cancel_check=None):
        results = []
        for plan in batch.plans:
            ok = plan.rel_path not in fail_rels
            results.append((plan, ok, None if ok else "假故障"))
        return results
    return _run


class SmartScaleSkipTest(unittest.TestCase):
    """智能倍率选择 + 已达目标跳过时字节级复制 + 批次编排。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "input"
        self.output_dir = self.root / "output"
        self.input_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, target=None, scale=2, fail_rels=(), single_ok=False):
        """返回 (四元组结果, 批次执行器 mock, 单图回退 mock)。"""
        with mock.patch("upscaler.waifu2x._run_batch",
                        side_effect=_fake_run_batch(fail_rels)) as batch, \
                mock.patch("upscaler.waifu2x.upscale_image",
                           return_value=single_ok) as single:
            result = upscale_folder(
                self.input_dir, self.output_dir, scale=scale, target=target,
            )
        return result, batch, single

    def test_original_mode_upscales_all(self):
        _make_image(self.input_dir, "small.png", 100, 100)
        _make_image(self.input_dir, "big.png", 5000, 3000)
        result, batch, single = self._run(target=None, scale=2)
        self.assertEqual(result, (2, 0, 0, 0))
        self.assertEqual(batch.call_count, 1)   # 参数相同：两张同批、一次进程
        self.assertEqual(single.call_count, 0)  # 没有回退
        self.assertEqual(len(batch.call_args.args[0].plans), 2)

    def test_over_target_skips_and_copies_bytes(self):
        p = _make_image(self.input_dir, "big.png", 5000, 3000)  # 已超 4K
        result, batch, _ = self._run(target=(2160, 3840), scale=2)
        self.assertEqual(result, (0, 1, 0, 0))
        self.assertEqual(batch.call_count, 0)   # 不启动任何 waifu2x 进程
        self.assertEqual(_sha256(p), _sha256(self.output_dir / "big.png"))

    def test_under_target_upscales(self):
        _make_image(self.input_dir, "small.png", 100, 100)
        result, batch, _ = self._run(target=(2160, 3840), scale=2)
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(batch.call_count, 1)

    def test_uses_two_x_when_sufficient(self):
        _make_image(self.input_dir, "page.png", 2000, 3000)  # 2× 已够，不选 4×
        result, batch, _ = self._run(target=(2160, 3840), scale=4)
        self.assertEqual(result, (1, 0, 0, 0))
        self.assertEqual(batch.call_args.args[0].key.scale, 2)

    def test_single_failure_preserves_and_continues(self):
        _make_image(self.input_dir, "a.png", 100, 100)
        p_b = _make_image(self.input_dir, "b.png", 200, 200)
        result, batch, single = self._run(fail_rels=("b.png",), single_ok=False)
        self.assertEqual(result, (1, 0, 0, 1))   # 1 成功（批次），1 失败（回退也失败）
        self.assertEqual(batch.call_count, 1)    # 两张同批，只启动一次
        self.assertEqual(single.call_count, 1)   # 只有失败的那张回退
        self.assertIn("b.png", str(single.call_args.args[0]))
        self.assertIsNone(single.call_args.kwargs["cancel_check"])
        self.assertEqual(_sha256(p_b), _sha256(self.output_dir / "b.png"))

    def test_batch_failure_fallback_success_counts_as_success(self):
        _make_image(self.input_dir, "a.png", 100, 100)
        _make_image(self.input_dir, "b.png", 200, 200)
        result, batch, single = self._run(fail_rels=("a.png", "b.png"),
                                          single_ok=True)
        self.assertEqual(result, (2, 0, 0, 0))   # 回退成功仍算超分成功
        self.assertEqual(batch.call_count, 1)
        self.assertEqual(single.call_count, 2)

    def test_fallback_receives_cancel_check(self):
        _make_image(self.input_dir, "a.png", 100, 100)

        def cancel_check():
            return False

        with mock.patch("upscaler.waifu2x._run_batch",
                        side_effect=_fake_run_batch(("a.png",))), \
                mock.patch("upscaler.waifu2x.upscale_image",
                           return_value=True) as single:
            upscale_folder(self.input_dir, self.output_dir,
                           cancel_check=cancel_check)
        self.assertIs(single.call_args.kwargs["cancel_check"], cancel_check)


if __name__ == "__main__":
    unittest.main()
