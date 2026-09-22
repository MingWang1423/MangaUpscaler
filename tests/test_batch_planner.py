"""test_batch_planner.py — waifu2x 批处理规划器（纯函数）测试。

全部在临时目录里创建真实图片，但规划器本身不跑 waifu2x、不建临时文件、不改原图。
覆盖：字段描述、同参同批、不同倍率/格式/GPU/noise 分不同批、嵌套目录与同名图片的
临时文件名映射、SVG/多帧 GIF/损坏图片只做原样复制、静态 GIF/BMP 只标记转换、
输入顺序无关，以及纯函数性质（无 subprocess / 无临时文件 / 不改原图）。
"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from PIL import Image

from upscaler import batch_planner
from upscaler.batch_planner import (
    ACTION_CONVERT, ACTION_COPY, ACTION_DIRECT, REASON_ALREADY_AT_TARGET,
    REASON_ANIMATED_GIF, REASON_SVG, REASON_UNREADABLE, group_batches, plan_batches,
    plan_image, plan_images, temp_stem, temp_to_rel_path,
)


class PlannerTestBase(unittest.TestCase):
    """提供 input 临时目录与造图辅助（造的是真实文件，便于验证「不改原图」）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.input_dir = self.root / "input"
        self.input_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _make(self, rel, size=(120, 180)):
        """在 input_dir 下按扩展名生成一张真实图片，返回其绝对路径。"""
        path = self.input_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (10, 20, 30)).save(path)
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
            b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>')
        return path

    def _make_broken(self, rel="broken.jpg"):
        path = self.input_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a real jpeg")
        return path

    def _plan(self, **kwargs):
        return plan_batches(self.input_dir, **kwargs)

    def _snapshot(self):
        """input_dir 下「相对路径 -> 字节」的快照，用于验证规划器没改任何原图。"""
        return {p.relative_to(self.input_dir).as_posix(): p.read_bytes()
                for p in self.input_dir.rglob("*") if p.is_file()}


class PlanDescriptionTest(PlannerTestBase):
    """每张图片的描述信息是否完整、准确。"""

    def test_png_plan_fields(self):
        path = self._make("page.png", size=(1200, 1800))
        plan = plan_image("page.png", self.input_dir, scale=2, noise=3, gpu="auto")
        self.assertEqual(plan.rel_path, "page.png")                 # EPUB 内部相对路径
        self.assertEqual(plan.abs_path, str(path.resolve()))        # 原始绝对路径
        self.assertEqual(plan.format, "png")
        self.assertEqual(plan.action, ACTION_DIRECT)                # 处理方式
        self.assertEqual(plan.scale, 2)                             # 所需倍率
        self.assertEqual(plan.noise, 3)
        self.assertEqual(plan.size, (1200, 1800))
        self.assertTrue(plan.direct)                          # 是否可直接交给 waifu2x
        self.assertTrue(plan.can_waifu2x)
        self.assertFalse(plan.needs_conversion)               # 是否需要格式转换
        self.assertFalse(plan.copy_as_is)                     # 是否应原样复制
        self.assertEqual((plan.input_format, plan.output_format, plan.target_format),
                         ("png", "png", "png"))
        self.assertIsNone(plan.temp_in_name)
        self.assertTrue(plan.temp_out_name.endswith("_out.png"))
        self.assertIsNone(plan.reason)
        self.assertEqual(plan.gpu, "auto")

    def test_bmp_is_marked_for_conversion_without_converting(self):
        self._make("pic.bmp", size=(60, 30))
        before = self._snapshot()
        plan = plan_image("pic.bmp", self.input_dir)
        self.assertEqual(plan.format, "bmp")
        self.assertEqual(plan.action, ACTION_CONVERT)
        self.assertTrue(plan.needs_conversion)
        self.assertTrue(plan.can_waifu2x)
        self.assertFalse(plan.copy_as_is)
        self.assertEqual((plan.input_format, plan.output_format, plan.target_format),
                         ("png", "png", "bmp"))
        self.assertTrue(plan.temp_in_name.endswith("_in.png"))
        self.assertTrue(plan.temp_out_name.endswith("_out.png"))
        self.assertEqual(self._snapshot(), before)        # 本阶段不真正转换文件
        self.assertFalse((self.input_dir / plan.temp_in_name).exists())

    def test_static_gif_is_marked_for_conversion(self):
        self._make("static.gif", size=(60, 40))
        plan = plan_image("static.gif", self.input_dir)
        self.assertEqual(plan.action, ACTION_CONVERT)
        self.assertEqual((plan.input_format, plan.output_format, plan.target_format),
                         ("png", "png", "gif"))

    def test_nested_rel_path_is_posix_and_absolute(self):
        path = self._make("OEBPS/Images/Chapter 1/page.png")
        plan = plan_image("OEBPS/Images/Chapter 1/page.png", self.input_dir)
        self.assertEqual(plan.rel_path, "OEBPS/Images/Chapter 1/page.png")
        self.assertEqual(plan.abs_path, str(path.resolve()))
        self.assertTrue(Path(plan.abs_path).is_absolute())

    def test_backslash_rel_path_is_normalized(self):
        self._make("sub/page.png")
        plan = plan_image("sub\\page.png", self.input_dir)
        self.assertEqual(plan.rel_path, "sub/page.png")
        self.assertEqual(plan.format, "png")

    def test_noise_and_scale_are_recorded(self):
        self._make("page.png")
        plan = plan_image("page.png", self.input_dir, scale=4, noise=0)
        self.assertEqual((plan.scale, plan.noise), (4, 0))

    def test_as_dict_contains_all_descriptions(self):
        self._make("page.png")
        data = plan_image("page.png", self.input_dir).as_dict()
        for key in ("abs_path", "rel_path", "format", "action", "scale", "noise",
                    "input_format", "output_format", "target_format", "reason"):
            self.assertIn(key, data)
        self.assertEqual(data["action"], ACTION_DIRECT)



class BatchGroupingTest(PlannerTestBase):
    """分批规则：同参同批、异参异批，copy 图片绝不进批。"""

    def test_same_parameters_share_one_batch(self):
        for name in ("a.png", "b.png", "c.png"):
            self._make(name)
        result = self._plan()
        self.assertEqual(result.batch_count, 1)
        batch = result.batches[0]
        self.assertEqual(len(batch), 3)
        self.assertEqual(batch.rel_paths, ("a.png", "b.png", "c.png"))
        self.assertEqual(result.waifu2x_count, 3)
        self.assertEqual(result.copy_count, 0)
        self.assertTrue(all(plan.batch_key() == batch.key for plan in batch.plans))

    def test_jpg_png_webp_grouped_by_format(self):
        for name in ("a.jpg", "b.jpg", "c.png", "d.png", "e.webp", "f.webp"):
            self._make(name)
        result = self._plan()
        self.assertEqual(result.batch_count, 3)
        self.assertEqual(sorted(len(batch) for batch in result.batches), [2, 2, 2])
        for batch in result.batches:
            formats = {plan.format for plan in batch.plans}
            self.assertEqual(len(formats), 1)          # 同批内 waifu2x 格式一致
            self.assertIn(batch.key.input_format, ("jpg", "png", "webp"))

    def test_different_scale_goes_to_different_batches(self):
        self._make("medium.png", size=(1200, 1800))    # 目标下 2× 已够
        self._make("small.png", size=(800, 1200))      # 目标下需要 4×
        result = self._plan(scale=4, target=(2160, 3840))
        self.assertEqual(result.batch_count, 2)
        by_scale = {batch.key.scale: batch.rel_paths for batch in result.batches}
        self.assertEqual(sorted(by_scale), [2, 4])
        self.assertEqual(by_scale[2], ("medium.png",))
        self.assertEqual(by_scale[4], ("small.png",))

    def test_different_format_goes_to_different_batches(self):
        for name in ("a.png", "b.jpg", "c.webp", "d.bmp", "e.gif"):
            self._make(name)
        result = self._plan()
        self.assertEqual(result.batch_count, 5)
        combos = {(b.key.input_format, b.key.output_format, b.key.target_format)
                  for b in result.batches}
        self.assertEqual(combos, {
            ("png", "png", "png"), ("jpg", "jpg", "jpg"), ("webp", "webp", "webp"),
            ("png", "png", "bmp"), ("png", "png", "gif"),
        })

    def test_different_gpu_goes_to_different_batches(self):
        self._make("page.png")
        plans = (plan_image("page.png", self.input_dir, gpu="auto"),
                 plan_image("page.png", self.input_dir, gpu=0),
                 plan_image("page.png", self.input_dir, gpu=-1))
        batches = group_batches(plans)
        self.assertEqual(len(batches), 3)
        self.assertEqual({batch.key.gpu for batch in batches}, {"auto", 0, -1})
        self.assertEqual(len({batch.key.label() for batch in batches}), 3)

    def test_different_noise_goes_to_different_batches(self):
        self._make("page.png")
        plans = (plan_image("page.png", self.input_dir, noise=3),
                 plan_image("page.png", self.input_dir, noise=0))
        batches = group_batches(plans)
        self.assertEqual(len(batches), 2)
        self.assertEqual({batch.key.noise for batch in batches}, {0, 3})

    def test_already_at_target_is_copy_not_batch(self):
        self._make("big.png", size=(5000, 3000))
        result = self._plan(target=(2160, 3840))
        self.assertEqual(result.batch_count, 0)
        self.assertEqual(result.copy_count, 1)
        copy = result.copies[0]
        self.assertEqual(copy.reason, REASON_ALREADY_AT_TARGET)
        self.assertEqual(copy.scale, 1)                # 不超分
        self.assertFalse(copy.can_waifu2x)
        self.assertIsNone(copy.temp_out_name)

    def test_batch_plan_counts(self):
        self._make("a.png")
        self._make("b.bmp", size=(60, 30))
        self._make_svg("c.svg")
        result = self._plan()
        self.assertEqual((result.batch_count, result.waifu2x_count,
                          result.copy_count, len(result.plans)), (2, 2, 1, 3))



class CopyExclusionTest(PlannerTestBase):
    """SVG、多帧 GIF、损坏图片、不支持的格式必须排除在批次之外。"""

    def test_svg_animated_gif_and_broken_never_enter_batches(self):
        self._make_svg("img.svg")
        self._make_animated_gif("anim.gif")
        self._make_broken("broken.jpg")
        self._make("ok.png")
        result = self._plan()
        self.assertEqual(result.batch_count, 1)
        self.assertEqual(result.batches[0].rel_paths, ("ok.png",))
        reasons = {plan.rel_path: plan.reason for plan in result.copies}
        self.assertEqual(reasons, {"img.svg": REASON_SVG,
                                   "anim.gif": REASON_ANIMATED_GIF,
                                   "broken.jpg": REASON_UNREADABLE})
        self.assertEqual(result.copy_count, 3)
        batched = {rel for batch in result.batches for rel in batch.rel_paths}
        self.assertFalse(batched & set(reasons))

    def test_copy_plans_have_no_temp_names_and_scale_one(self):
        self._make_svg()
        self._make_broken()
        self._make_animated_gif()
        for plan in self._plan().copies:
            self.assertTrue(plan.copy_as_is)
            self.assertFalse(plan.can_waifu2x)
            self.assertIsNone(plan.temp_in_name)
            self.assertIsNone(plan.temp_out_name)
            self.assertEqual(plan.scale, 1)
            self.assertIsNone(plan.input_format)

    def test_static_gif_and_bmp_stay_in_batches(self):
        self._make("a.gif", size=(60, 40))
        self._make("b.bmp", size=(60, 40))
        self._make_animated_gif("anim.gif")
        result = self._plan()
        batched = {rel for batch in result.batches for rel in batch.rel_paths}
        self.assertEqual(batched, {"a.gif", "b.bmp"})
        self.assertEqual([plan.rel_path for plan in result.copies], ["anim.gif"])

    def test_unsupported_format_is_copy(self):
        plan = plan_image("weird.tiff", self.input_dir,
                          size_reader=lambda path: (10, 10))
        self.assertEqual(plan.action, ACTION_COPY)
        self.assertFalse(plan.can_waifu2x)
        self.assertEqual(plan.size, (10, 10))



class TempNameTest(PlannerTestBase):
    """安全临时文件名：不靠 basename、同名不冲突、可还原相对路径。"""

    def test_same_basename_in_different_dirs_does_not_collide(self):
        self._make("dir-a/page.png")
        self._make("dir-b/page.png")
        result = self._plan()
        self.assertEqual(result.batch_count, 1)            # 参数相同，允许同批
        names = result.batches[0].temp_out_names
        self.assertEqual(len(set(names)), 2)               # 临时文件名不冲突
        mapping = temp_to_rel_path(result.plans)
        for plan in result.plans:
            self.assertEqual(mapping[plan.temp_out_name], plan.rel_path)

    def test_stem_depends_on_full_relative_path(self):
        self.assertNotEqual(temp_stem("dir-a/page.png"), temp_stem("dir-b/page.png"))
        self.assertNotEqual(temp_stem("dir-a/page.png"), temp_stem("page.png"))
        self.assertEqual(temp_stem("dir-a/page.png"), temp_stem("dir-a/page.png"))
        self.assertRegex(temp_stem("dir-a/page.png"),
                         r"^w2x_[0-9a-f]{16}_[0-9a-z\-]+$")

    def test_temp_name_is_not_basename_only(self):
        self._make("dir-a/page.png")
        self._make("dir-b/page.png")
        names = {plan.rel_path: plan.temp_out_name
                 for plan in plan_images(self.input_dir)}
        self.assertNotEqual(names["dir-a/page.png"], "page_out.png")
        self.assertIn("dir-a-page", names["dir-a/page.png"])
        self.assertIn("dir-b-page", names["dir-b/page.png"])

    def test_temp_names_are_filename_safe(self):
        self._make("目录 with space/ページ.png")
        plan = self._plan().plans[0]
        self.assertRegex(plan.temp_out_name, r"^[A-Za-z0-9_.\-]+$")

    def test_mapping_recovers_original_relative_paths(self):
        self._make("dir-a/page.png")
        self._make("dir-b/page.png")
        self._make("dir-b/pic.bmp", size=(60, 30))
        result = self._plan()
        mapping = temp_to_rel_path(result.plans)
        for plan in result.plans:
            self.assertEqual(mapping[plan.temp_out_name], plan.rel_path)
            if plan.temp_in_name:
                self.assertEqual(mapping[plan.temp_in_name], plan.rel_path)
        self.assertIn("dir-b/pic.bmp", set(mapping.values()))

    def test_planning_order_does_not_change_mapping_or_batches(self):
        self._make("b.png", size=(100, 100))
        self._make("a/x.png", size=(100, 100))
        self._make("a/y.bmp", size=(50, 50))
        forward = plan_images(self.input_dir)
        reverse = tuple(plan_image(plan.rel_path, self.input_dir)
                        for plan in reversed(forward))
        self.assertEqual(temp_to_rel_path(forward), temp_to_rel_path(reverse))
        self.assertEqual([batch.key for batch in group_batches(forward)],
                         [batch.key for batch in group_batches(reverse)])
        # 批次内容与输入顺序无关（批间按键、批内按相对路径排序）
        self.assertEqual([batch.rel_paths for batch in group_batches(reverse)],
                         [batch.rel_paths for batch in group_batches(forward)])

    def test_conflicting_temp_names_raise(self):
        self._make("a/page.png")
        self._make("b/page.png")
        plans = plan_images(self.input_dir)
        clashing = (plans[0],
                    replace(plans[1], temp_out_name=plans[0].temp_out_name))
        with self.assertRaises(ValueError):
            temp_to_rel_path(clashing)

    def test_nested_directory_mapping_is_correct(self):
        paths = ("OEBPS/Images/Chapter 1/p1.png", "OEBPS/Images/Chapter 2/p1.png",
                 "OEBPS/Text/cover.png")
        for rel in paths:
            self._make(rel)
        result = self._plan()
        by_rel = {plan.rel_path: plan for plan in result.plans}
        for rel in paths:
            plan = by_rel[rel]
            self.assertEqual(plan.abs_path, str((self.input_dir / rel).resolve()))
            self.assertTrue(Path(plan.abs_path).is_file())
        mapping = temp_to_rel_path(result.plans)
        self.assertEqual(set(mapping.values()), set(paths))



class PurityTest(PlannerTestBase):
    """规划器必须无副作用：不起进程、不建临时文件、不改原图、不依赖 GUI/配置。"""

    def test_module_has_no_process_or_tempfile_helpers(self):
        for name in ("subprocess", "tempfile", "shutil", "os", "PySide6"):
            self.assertFalse(hasattr(batch_planner, name), name)

    def test_planning_does_not_modify_input_files(self):
        self._make("a.png")
        self._make("b.bmp", size=(60, 30))
        self._make("c.gif", size=(60, 40))
        self._make_animated_gif("anim.gif")
        self._make_svg("img.svg")
        before = self._snapshot()
        self._plan()
        self.assertEqual(self._snapshot(), before)
        for plan in plan_images(self.input_dir):
            for name in (plan.temp_in_name, plan.temp_out_name):
                if name:
                    self.assertFalse((self.input_dir / name).exists())

    def test_size_reader_is_injectable_and_needs_no_disk(self):
        # 文件并不存在，规划仍然成立：说明它可以被注入、不必真的碰磁盘
        plan = plan_image("ghost.png", self.input_dir, gpu=-1,
                          size_reader=lambda path: (640, 960))
        self.assertEqual(plan.size, (640, 960))
        self.assertEqual(plan.gpu, -1)
        self.assertEqual(plan.action, ACTION_DIRECT)
        self.assertTrue(Path(plan.abs_path).is_absolute())

    def test_missing_file_is_unreadable_copy(self):
        plan = plan_image("gone.png", self.input_dir)   # 默认读取器读不到尺寸
        self.assertEqual(plan.action, ACTION_COPY)
        self.assertEqual(plan.reason, REASON_UNREADABLE)

    def test_invalid_gpu_is_rejected(self):
        for gpu in ("0", -2, 1.5, True, None):
            with self.assertRaises(ValueError):
                plan_image("page.png", self.input_dir, gpu=gpu)

    def test_valid_gpu_values_are_accepted(self):
        for gpu in ("auto", -1, 0, 2, 5):
            plan = plan_image("page.png", self.input_dir, gpu=gpu,
                              size_reader=lambda path: (10, 10))
            self.assertEqual(plan.gpu, gpu)


if __name__ == "__main__":
    unittest.main()

