"""waifu2x 批处理规划器（纯函数，无副作用）。

把「一个目录里的待处理图片」规划成可以安全交给 waifu2x 的批次：本模块只做
分类与分组，产出的全是纯数据，供后续真正的批处理执行器使用。它
  - 不启动任何进程（不 import subprocess）；
  - 不创建/不删除临时文件；
  - 不修改原图（只读图片头部拿宽高）；
  - 不读也不修改配置；
  - 不依赖 GUI / Qt。
因此可以独立测试，也不受当前工作目录影响。

规划口径（格式判定集中在 epub/formats.py，与 upscaler/waifu2x.py 保持一致）：
  - JPG/JPEG/PNG/WebP：waifu2x 原生格式，可直接处理（action=direct）；
  - 静态 GIF / BMP：标记为「先转 PNG 再处理」（action=convert，本阶段只做标记，
    不真正转换文件）；
  - SVG、多帧 GIF、已达目标分辨率、损坏/无法读取尺寸、不支持的格式：
    原样复制（action=copy），绝不进入 waifu2x 批次。

同批条件：处理方式、所需倍率、noise、waifu2x 输入格式、waifu2x 输出格式、
GPU 配置完全一致（再加「最终输出格式」以区分同样转 PNG 但最终要转回 BMP 与
静态 GIF 的图片 —— 多一条限制只会让批次更细，不会把不同参数的图片混进同一批）。

临时文件名：由「EPUB 内部相对路径的 sha256 摘要 + 路径 slug」生成，不使用原始
basename 作为唯一标识，嵌套目录下的同名图片不会互相覆盖，并且可以用
temp_to_rel_path() 把临时产物还原回原始相对路径。
"""

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from PIL import Image

from epub.formats import (
    CONVERT_FOR_UPSCALE, PASSTHROUGH_EXTS, WAIFU2X_EXTS, is_animated_gif, iter_images,
)
from upscaler.resolution import needed_scale

# 处理方式
ACTION_DIRECT = "direct"      # 直接交给 waifu2x
ACTION_CONVERT = "convert"    # 先转成 PNG 再交给 waifu2x
ACTION_COPY = "copy"          # 原样复制，绝不进入 waifu2x 批次

# 会进入 waifu2x 批次的处理方式
WAIFU2X_ACTIONS = (ACTION_DIRECT, ACTION_CONVERT)

# 原样复制的原因（仅 action=copy 的图片有值）
REASON_SVG = "svg"
REASON_ANIMATED_GIF = "animated_gif"
REASON_ALREADY_AT_TARGET = "already_at_target"
REASON_UNREADABLE = "unreadable"
REASON_UNSUPPORTED = "unsupported_format"

# 转换路径的中间格式：BMP / 静态 GIF 先转 PNG 交给 waifu2x，waifu2x 输出 PNG
CONVERT_FORMAT = "png"

# 临时文件名的前缀 / 摘要长度 / 可读 slug 长度（避免路径过长触发 Windows 上限）
_TEMP_PREFIX = "w2x"
_DIGEST_LEN = 16
_SLUG_MAX = 48
@dataclass(frozen=True)
class BatchKey:
    """批次键：只有键完全相同的图片才允许放进同一批。"""

    action: str            # 处理方式：direct / convert
    scale: int             # 所需倍率（1 不会出现在批次里，倍率 1 的图片是原样复制）
    noise: int             # waifu2x -n 参数
    input_format: str      # waifu2x 输入格式（direct 为原格式，convert 为 png）
    output_format: str     # waifu2x 输出格式（direct 为原格式，convert 为 png）
    target_format: str     # 最终产物格式（= 原扩展名；区分转回 BMP 还是 GIF）
    gpu: str | int         # GPU 配置："auto" / -1（CPU）/ 非负整数 GPU 编号

    def sort_key(self):
        """稳定排序用的字符串元组（GPU 可能是 "auto"/int，统一转字符串）。"""
        return (self.action, self.scale, self.noise, self.input_format,
                self.output_format, self.target_format, str(self.gpu))

    def label(self):
        """批次标签（只含文件名安全字符），便于日志与将来的临时目录命名。"""
        gpu = "auto" if self.gpu == "auto" else "g%d" % self.gpu
        return "%s_s%d_n%d_%s2%s_%s_%s" % (
            self.action, self.scale, self.noise, self.input_format,
            self.output_format, self.target_format, gpu,
        )


@dataclass(frozen=True)
class ImagePlan:
    """一张图片的规划结果（纯数据，不持有打开的文件/进程/临时目录）。"""

    rel_path: str                  # EPUB 内部相对路径（POSIX 风格）
    abs_path: str                  # 原始绝对路径
    format: str                    # 图片格式（小写扩展名，不含点）
    action: str                    # 处理方式：direct / convert / copy
    scale: int                     # 所需倍率（copy 恒为 1，表示不超分）
    noise: int                     # noise 参数
    gpu: str | int                 # GPU 配置
    width: int | None              # 宽；读不到尺寸时为 None
    height: int | None             # 高；读不到尺寸时为 None
    input_format: str | None       # 交给 waifu2x 的输入格式；copy 为 None
    output_format: str | None      # waifu2x 输出格式；copy 为 None
    target_format: str | None      # 最终产物格式；copy 为 None
    temp_in_name: str | None       # 转换前的临时 PNG 名；无需转换时为 None
    temp_out_name: str | None      # waifu2x 输出的临时文件名；copy 为 None
    reason: str | None             # 原样复制的原因；非 copy 为 None

    @property
    def can_waifu2x(self):
        """是否可以进入 waifu2x 批次。"""
        return self.action in WAIFU2X_ACTIONS

    @property
    def direct(self):
        """是否可以直接交给 waifu2x（不需要格式转换）。"""
        return self.action == ACTION_DIRECT

    @property
    def needs_conversion(self):
        """是否需要先转换成 PNG 再交给 waifu2x。"""
        return self.action == ACTION_CONVERT

    @property
    def copy_as_is(self):
        """是否应当原样复制、不做任何像素处理。"""
        return self.action == ACTION_COPY

    @property
    def size(self):
        """(宽, 高)；读不到尺寸时返回 None。"""
        if self.width is None or self.height is None:
            return None
        return (self.width, self.height)

    def batch_key(self):
        """本图片所属批次的键（copy 图片也会算出键，但不会被分组）。"""
        return BatchKey(action=self.action, scale=self.scale, noise=self.noise,
                        input_format=self.input_format or "",
                        output_format=self.output_format or "",
                        target_format=self.target_format or "", gpu=self.gpu)

    def as_dict(self):
        """转成普通 dict（便于日志与断言）。"""
        return asdict(self)


@dataclass(frozen=True)
class Batch:
    """一个批次：键相同的图片集合。"""

    key: BatchKey
    plans: tuple

    def __len__(self):
        return len(self.plans)

    @property
    def rel_paths(self):
        return tuple(plan.rel_path for plan in self.plans)

    @property
    def temp_out_names(self):
        return tuple(plan.temp_out_name for plan in self.plans)


@dataclass(frozen=True)
class BatchPlan:
    """整体规划结果：全部图片 + 可交给 waifu2x 的批次 + 原样复制的图片。"""

    plans: tuple        # 全部图片（含原样复制）
    batches: tuple      # 只含可交给 waifu2x 的批次
    copies: tuple       # 原样复制，绝不进批处理

    @property
    def batch_count(self):
        return len(self.batches)

    @property
    def waifu2x_count(self):
        return sum(len(batch) for batch in self.batches)

    @property
    def copy_count(self):
        return len(self.copies)



def _normalize_rel(rel_path):
    """把相对路径规范成 POSIX 风格（'a\\b.png' -> 'a/b.png'），去掉开头 './' 与 '/'。"""
    rel = str(rel_path).replace("\\", "/").lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _validate_gpu(gpu):
    """校验 GPU 配置；非法值抛 ValueError（纯校验：不读配置、不改入参）。

    合法值与 config_schema 一致："auto"、-1（CPU）、任意非负整数。
    """
    if gpu == "auto":
        return
    if isinstance(gpu, bool) or not isinstance(gpu, int) or gpu < -1:
        raise ValueError(
            "非法 GPU 配置: %r（只能是 \"auto\"、-1 或非负整数）" % (gpu,)
        )


def read_image_size(path):
    """读取图片 (宽, 高)；损坏或不支持的图片返回 None。

    只解析图片头部（Pillow 懒加载），不修改原图、不落任何文件。
    """
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def temp_stem(rel_path):
    """由 EPUB 内部相对路径生成安全的临时文件主名。

    - 主名里带完整相对路径的 sha256 摘要，不使用原始 basename 作为唯一标识；
    - 不同目录下的同名图片摘要不同，绝不互相覆盖；
    - 尾部 slug 保留可读的路径轮廓，方便人工排错；
    - 只含 [a-z0-9-]，可直接当 Windows / 类 Unix 文件名。
    """
    rel = _normalize_rel(rel_path)
    digest = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    slug = re.sub(r"[^0-9a-zA-Z]+", "-", rel).strip("-").lower()[:_SLUG_MAX]
    if slug:
        return "%s_%s_%s" % (_TEMP_PREFIX, digest, slug)
    return "%s_%s" % (_TEMP_PREFIX, digest)


def temp_file_names(rel_path, output_format, needs_conversion):
    """返回 (temp_in_name, temp_out_name)。

    temp_in_name 是「先转 PNG」用的临时输入名（无需转换时为 None）；
    temp_out_name 是 waifu2x 输出的临时文件名。两者都由 temp_stem 派生，
    因此同一张图片反复规划结果一致，而不同图片（含同名不同目录）不冲突。
    """
    stem = temp_stem(rel_path)
    temp_in = "%s_in.%s" % (stem, CONVERT_FORMAT) if needs_conversion else None
    temp_out = "%s_out.%s" % (stem, output_format) if output_format else None
    return temp_in, temp_out


def plan_image(rel_path, input_dir, scale=2, noise=3, gpu="auto", target=None,
               size_reader=None):
    """规划单张图片，返回只读的 ImagePlan（纯函数：不写盘、不起进程、不读配置）。

    rel_path 是 EPUB 内部相对路径（POSIX 风格），input_dir 是解压后的图片根目录，
    abs_path 由两者拼出的原始绝对路径。target=(短边, 长边) 与 upscale_folder 口径
    一致：原图已达目标分辨率时该图片按「原样复制」处理（所需倍率记为 1）。
    size_reader 可注入自定义的宽高读取函数（默认 read_image_size），便于测试；
    即使 target 为 None 也会读一次尺寸，以便识别损坏/无法读取的图片。
    """
    _validate_gpu(gpu)
    reader = size_reader or read_image_size
    rel = _normalize_rel(rel_path)
    ext = PurePosixPath(rel).suffix.lower()
    fmt = ext.lstrip(".")
    source = Path(input_dir) / rel
    abs_path = str(source.resolve())

    def _copy(reason, width, height):
        return ImagePlan(rel_path=rel, abs_path=abs_path, format=fmt,
                         action=ACTION_COPY, scale=1, noise=noise, gpu=gpu,
                         width=width, height=height, input_format=None,
                         output_format=None, target_format=None,
                         temp_in_name=None, temp_out_name=None, reason=reason)

    # SVG：原样保留，不做像素超分，也不必读尺寸
    if ext in PASSTHROUGH_EXTS:
        return _copy(REASON_SVG, None, None)

    # 多帧 GIF：字节级原样保留，绝不重编码成单帧
    if ext == ".gif" and is_animated_gif(source):
        return _copy(REASON_ANIMATED_GIF, None, None)

    size = reader(source)
    if size is None:
        # 损坏 / 无法读取尺寸：不进批处理，交由上层原样复制
        return _copy(REASON_UNREADABLE, None, None)
    width, height = size

    if ext in WAIFU2X_EXTS:
        action, in_format, out_format = ACTION_DIRECT, fmt, fmt
    elif ext in CONVERT_FOR_UPSCALE:
        # 静态 GIF / BMP：标记为「先转 PNG 再处理」；本阶段只标记，不真正转换
        action, in_format, out_format = ACTION_CONVERT, CONVERT_FORMAT, CONVERT_FORMAT
    else:
        return _copy(REASON_UNSUPPORTED, width, height)

    use_scale = scale
    if target is not None:
        short_limit, long_limit = target
        use_scale = needed_scale(width, height, scale, short_limit, long_limit)
        if use_scale == 1:
            # 原图已达目标分辨率：原样复制（与 upscale_folder 的跳过口径一致）
            return _copy(REASON_ALREADY_AT_TARGET, width, height)

    temp_in, temp_out = temp_file_names(rel, out_format, action == ACTION_CONVERT)
    return ImagePlan(rel_path=rel, abs_path=abs_path, format=fmt, action=action,
                     scale=use_scale, noise=noise, gpu=gpu, width=width,
                     height=height, input_format=in_format, output_format=out_format,
                     target_format=fmt, temp_in_name=temp_in,
                     temp_out_name=temp_out, reason=None)


def plan_images(input_dir, scale=2, noise=3, gpu="auto", target=None,
                size_reader=None):
    """扫描 input_dir 下所有图片并逐张规划，返回按相对路径排序的 ImagePlan 元组。

    只读目录与图片头部：不创建/删除任何文件、不启动进程、不改配置、不依赖 GUI。
    输入顺序由 iter_images 保证稳定（按 POSIX 相对路径排序），与当前工作目录无关。
    """
    _validate_gpu(gpu)
    root = Path(input_dir)
    return tuple(
        plan_image(rel.as_posix(), root, scale=scale, noise=noise, gpu=gpu,
                   target=target, size_reader=size_reader)
        for rel in iter_images(root)
    )


def batch_key_of(plan):
    """返回图片所属批次的键（等价于 plan.batch_key()，便于函数式调用）。"""
    return plan.batch_key()


def group_batches(plans):
    """把可交给 waifu2x 的图片按批次键分组，返回按批次键排序的 Batch 元组。

    只有 action 为 direct / convert 且批次键（处理方式 / 倍率 / noise / waifu2x
    输入格式 / waifu2x 输出格式 / 最终输出格式 / GPU 配置）完全相同的图片才会被
    分到同一批；action=copy 的图片（SVG、多帧 GIF、已达目标分辨率、损坏或无法
    读取尺寸、不支持的格式）一律排除，绝不进入批次。
    批次之间按批次键排序、批内按相对路径排序，因此结果与输入顺序无关。
    """
    groups = {}
    for plan in plans:
        if not plan.can_waifu2x:
            continue
        groups.setdefault(plan.batch_key(), []).append(plan)
    return tuple(
        Batch(key=key, plans=tuple(sorted(groups[key], key=lambda plan: plan.rel_path)))
        for key in sorted(groups, key=BatchKey.sort_key)
    )


def copy_plans(plans):
    """挑出应当原样复制的图片（保持输入顺序）。"""
    return tuple(plan for plan in plans if plan.copy_as_is)


def plan_batches(input_dir, scale=2, noise=3, gpu="auto", target=None,
                 size_reader=None):
    """一站式规划：扫描 + 逐张规划 + 分批，返回纯数据的 BatchPlan。

    注意：本函数只做规划，不执行任何超分/转换，也不把结果接到真实处理流程上。
    """
    plans = plan_images(input_dir, scale=scale, noise=noise, gpu=gpu,
                        target=target, size_reader=size_reader)
    return BatchPlan(plans=plans, batches=group_batches(plans),
                     copies=copy_plans(plans))


def temp_to_rel_path(plans):
    """临时文件名 -> EPUB 内部相对路径，用于把临时产物还原回原始相对路径。

    不同目录下的同名图片会得到不同的临时文件名；万一出现重名（两个不同的相对
    路径算出同一个临时名）直接抛 ValueError，绝不让它们互相覆盖。
    """
    mapping = {}
    for plan in plans:
        for name in (plan.temp_in_name, plan.temp_out_name):
            if not name:
                continue
            owner = mapping.get(name)
            if owner is not None and owner != plan.rel_path:
                raise ValueError("临时文件名冲突: %s（%s / %s）"
                                 % (name, owner, plan.rel_path))
            mapping[name] = plan.rel_path
    return mapping

