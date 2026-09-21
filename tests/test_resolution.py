"""test_resolution.py — 目标分辨率、方向自适应与智能倍率的纯函数测试。

覆盖：竖版漫画页、横版双页、正方形、已超过 4K、2× 足够、必须 4×、
原画质（由 upscale_folder 的 target=None 覆盖）、边界值恰好等于目标。
"""

import unittest

from upscaler.resolution import (
    needed_scale, orientation_of, resize_size, target_bounds,
)

# (short_limit, long_limit)
FOUR_K = (2160, 3840)
TWO_K = (1600, 2560)


class OrientationTest(unittest.TestCase):
    def test_portrait(self):
        self.assertEqual(orientation_of(1000, 2000), "portrait")

    def test_landscape(self):
        self.assertEqual(orientation_of(2000, 1000), "landscape")

    def test_square_is_portrait(self):
        self.assertEqual(orientation_of(1000, 1000), "portrait")


class TargetBoundsTest(unittest.TestCase):
    def test_portrait_bounds(self):
        self.assertEqual(target_bounds(1000, 2000, *FOUR_K), (2160, 3840))

    def test_landscape_bounds(self):
        self.assertEqual(target_bounds(2000, 1000, *FOUR_K), (3840, 2160))

    def test_square_bounds(self):
        self.assertEqual(target_bounds(1000, 1000, *FOUR_K), (2160, 3840))


class NeededScaleTest(unittest.TestCase):
    def test_already_over_target_skips(self):
        # 已超过 4K 的横版双页
        self.assertEqual(needed_scale(5000, 3000, 4, *FOUR_K), 1)

    def test_boundary_exactly_target_skips(self):
        # 竖版恰好 2160×3840
        self.assertEqual(needed_scale(2160, 3840, 4, *FOUR_K), 1)
        # 横版恰好 3840×2160
        self.assertEqual(needed_scale(3840, 2160, 4, *FOUR_K), 1)

    def test_two_x_sufficient(self):
        # 竖版 2000×3000：2× → 4000×6000，已覆盖 2160×3840
        self.assertEqual(needed_scale(2000, 3000, 4, *FOUR_K), 2)

    def test_must_four_x(self):
        # 竖版 800×1200：2× → 1600×2400 不足，4× → 3200×4800 足够
        self.assertEqual(needed_scale(800, 1200, 4, *FOUR_K), 4)

    def test_requested_two_caps(self):
        # 用户只允许 2×，即使 4× 才够，也只返回 2×
        self.assertEqual(needed_scale(800, 1200, 2, *FOUR_K), 2)

    def test_square_under_target(self):
        # 正方形 1000×1000 → 目标 2160×2160，2× 不足，4× 够
        self.assertEqual(needed_scale(1000, 1000, 4, *FOUR_K), 4)


class ResizeSizeTest(unittest.TestCase):
    def test_portrait_shrink(self):
        # 竖版 5000×8000 → 2160×3456
        self.assertEqual(resize_size(5000, 8000, *FOUR_K), (2160, 3456))

    def test_landscape_double_page(self):
        # 横版双页 5000×3000 → 3600×2160（高触顶，宽不再被压到 2160）
        self.assertEqual(resize_size(5000, 3000, *FOUR_K), (3600, 2160))

    def test_landscape_not_limited_to_2160_width(self):
        w, h = resize_size(5000, 3000, *FOUR_K)
        self.assertEqual(w, 3600)
        self.assertGreater(w, 2160)

    def test_landscape_2k(self):
        self.assertEqual(resize_size(5000, 3000, *TWO_K), (2560, 1536))

    def test_under_target_not_upscaled(self):
        # 小于目标：只缩小不放大
        self.assertEqual(resize_size(1000, 800, *FOUR_K), (1000, 800))

    def test_boundary_exact(self):
        self.assertEqual(resize_size(2160, 3840, *FOUR_K), (2160, 3840))
        self.assertEqual(resize_size(3840, 2160, *FOUR_K), (3840, 2160))


if __name__ == "__main__":
    unittest.main()
