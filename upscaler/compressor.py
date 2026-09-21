"""按画质档位对目录内图片做缩放 + 同格式重编码。

与 upscaler/waifu2x.py 结构对称：iter_images 列图，_compress_one 处理单张，
compress_folder 批量编排。输出格式由源文件扩展名驱动，保证「扩展名 == 实际
字节」，build_epub 无需任何兜底匹配。
"""

import shutil
from pathlib import Path

from PIL import Image

from epub.formats import PASSTHROUGH_EXTS, is_animated_gif, iter_images
from logging_setup import get_logger
from upscaler.resolution import resize_size

logger = get_logger("upscaler.compressor")

# 扩展名 -> Pillow 格式名（扩展名驱动，保证输出字节与扩展名一致）
_EXT_TO_FORMAT = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".gif": "GIF",
    ".webp": "WEBP",
    ".bmp": "BMP",
}

# 目标分辨率用「短边 / 长边」描述，方向自适应在 resolution.py 统一处理：
# 4K：短边 2160 / 长边 3840；2.5K：短边 1600 / 长边 2560；original 不限制。
QUALITY_PROFILES = {
    "original": {"short": None, "long": None, "jpeg_quality": None},
    "4k": {"short": 2160, "long": 3840, "jpeg_quality": 90},
    "2k": {"short": 1600, "long": 2560, "jpeg_quality": 80},
}


def quality_target(quality):
    """返回 (short_limit, long_limit)；original 档返回 None。"""
    profile = QUALITY_PROFILES.get(quality, QUALITY_PROFILES["4k"])
    if profile["short"] is None:
        return None
    return (profile["short"], profile["long"])


def _compress_one(source, target, short_limit, long_limit, jpeg_quality):
    """按方向自适应边界框等比缩放（只缩小）并同格式重编码。

    输出格式由源文件扩展名决定（绝不改扩展名、绝不转格式）。多帧 GIF 不进入
    本函数（由 compress_folder 原样复制）。尺寸计算复用 resolution.resize_size，
    与超分前的跳过判断共用同一套方向判断。
    """
    fmt = _EXT_TO_FORMAT.get(source.suffix.lower())
    if fmt is None:
        logger.warning("不支持的扩展名: %s", source)
        return False
    try:
        with Image.open(source) as im:
            detected = im.format
            if detected and detected.upper() != fmt:
                logger.warning("扩展名 %s 与内容格式 %s 不符: %s", source.suffix, detected, source)

            # 模式预处理：保证透明通道与缩放正确
            if fmt == "JPEG":
                im = im.convert("RGB")            # JPEG 无透明通道
            elif fmt == "PNG" and im.mode == "P":
                im = im.convert("RGBA")           # 调色板 PNG 先转 RGBA，保住透明
            elif fmt == "GIF":
                im = im.convert("RGBA")           # GIF 透明 + 缩放（保存时自动回量化）
            elif fmt == "BMP":
                im = im.convert("RGB")            # BMP 无透明通道，统一 RGB 保证可写

            # 等比缩放：只缩小不放大，保持宽高比，横竖版共用同一方向判断
            w, h = im.size
            new_w, new_h = resize_size(w, h, short_limit, long_limit)
            if (new_w, new_h) != (w, h):
                im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # 同格式重编码（扩展名不变）
            if fmt == "JPEG":
                im.save(target, format="JPEG", quality=jpeg_quality)
            elif fmt == "WEBP":
                im.save(target, format="WEBP", quality=jpeg_quality)
            else:
                im.save(target, format=fmt)
        return True
    except Exception as exc:
        logger.error("压缩失败: %s: %s", source, exc)
        return False


def compress_folder(input_dir, output_dir, quality="4k",
                    progress_callback=None, cancel_check=None,
                    clean=True):
    """按画质档位批量处理目录内图片，返回 (成功数, 失败数)。

    clean=True 时先清空 output_dir。original 档（short 为 None）、SVG、多帧 GIF
    走纯复制分支（字节级一致）；其余按方向自适应边界缩放 + 同格式重编码。
    单张失败时回退为原样复制，不阻断整本 EPUB。
    progress_callback(done, total) 语义与 upscale_folder 一致。
    """
    profile = QUALITY_PROFILES.get(quality, QUALITY_PROFILES["4k"])
    short_limit = profile["short"]
    long_limit = profile["long"]
    jpeg_quality = profile["jpeg_quality"]

    input_path = Path(input_dir)
    output_path = Path(output_dir)
    if clean and output_path.exists():
        shutil.rmtree(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    images = iter_images(input_path)
    total = len(images)
    if progress_callback:
        progress_callback(0, total)

    success, failed = 0, 0
    for idx, rel in enumerate(images, start=1):
        if cancel_check and cancel_check():
            raise InterruptedError("用户已取消")
        source = input_path / rel
        target = output_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        ext = rel.suffix.lower()
        logger.info("[%d/%d] 处理: %s", idx, total, rel.as_posix())

        if ext in PASSTHROUGH_EXTS:
            # SVG：原样保留，不缩放不重编码
            shutil.copy2(source, target)
            logger.info("[copy] %s 为 SVG，不压缩，原样保留", rel.as_posix())
            success += 1
        elif short_limit is None:
            # original 档：原样复制，不缩放不重编码
            shutil.copy2(source, target)
            success += 1
        elif ext == ".gif" and is_animated_gif(source):
            # 多帧 GIF：保守起见原样复制，避免重编码丢帧
            shutil.copy2(source, target)
            logger.info("[copy] %s 为多帧 GIF，不重编码，原样保留", rel.as_posix())
            success += 1
        elif _compress_one(source, target, short_limit, long_limit, jpeg_quality):
            success += 1
        else:
            # 单张失败：回退为原样复制，不阻断整本 EPUB
            shutil.copy2(source, target)
            logger.error("压缩失败，已原样保留: %s", rel.as_posix())
            failed += 1

        if progress_callback:
            progress_callback(idx, total)

    logger.info("压缩完成: 成功 %d 张，失败 %d 张", success, failed)
    return success, failed
