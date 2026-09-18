"""按画质档位对目录内图片做缩放 + 同格式重编码。

与 upscaler/waifu2x.py 结构对称：iter_images 列图，_compress_one 处理单张，
compress_folder 批量编排。输出格式由源文件扩展名驱动，保证「扩展名 == 实际
字节」，build_epub 无需任何兜底匹配。
"""

import shutil
from pathlib import Path

from PIL import Image

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 扩展名 -> Pillow 格式名（扩展名驱动，保证输出字节与扩展名一致）
_EXT_TO_FORMAT = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".gif": "GIF",
    ".webp": "WEBP",
    ".bmp": "BMP",
}

# 边界框采用竖屏比例（宽 < 高），适配漫画的竖版页面
# 4K 屏幕竖屏显示时为 2160×3840，2.5K 屏幕竖屏显示时为 1600×2560
QUALITY_PROFILES = {
    "original": {"box": None, "jpeg_quality": None},
    "4k": {"box": (2160, 3840), "jpeg_quality": 90},
    "2k": {"box": (1600, 2560), "jpeg_quality": 80},
}


def iter_images(root):
    """递归列出 root 下所有图片，返回按 POSIX 相对路径排序的相对路径列表。"""
    root_path = Path(root)
    relatives = [p.relative_to(root_path) for p in root_path.rglob("*")
                 if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(relatives, key=lambda rel: rel.as_posix())


def _is_animated_gif(path):
    """判断 GIF 是否多帧（>1 帧）。多帧动图需要原样复制，否则重编码会丢帧。"""
    try:
        with Image.open(path) as im:
            return bool(getattr(im, "is_animated", False))
    except Exception:
        return False


def _compress_one(source, target, box, jpeg_quality):
    """按边界框等比缩放（只缩小）并同格式重编码；成功返回 True，失败返回 False。

    输出格式由源文件扩展名决定（绝不改扩展名、绝不转格式）。多帧 GIF 不进入
    本函数（由 compress_folder 原样复制）。
    """
    fmt = _EXT_TO_FORMAT.get(source.suffix.lower())
    if fmt is None:
        print(f"[warn] 不支持的扩展名: {source}")
        return False
    try:
        with Image.open(source) as im:
            detected = im.format
            if detected and detected.upper() != fmt:
                print(f"[warn] 扩展名 {source.suffix} 与内容格式 {detected} 不符: {source}")

            # 模式预处理：保证透明通道与缩放正确
            if fmt == "JPEG":
                im = im.convert("RGB")            # JPEG 无透明通道
            elif fmt == "PNG" and im.mode == "P":
                im = im.convert("RGBA")           # 调色板 PNG 先转 RGBA，保住透明
            elif fmt == "GIF":
                im = im.convert("RGBA")           # GIF 透明 + 缩放（保存时自动回量化）

            # 等比缩放：只缩小不放大（ratio 上限为 1）
            w, h = im.size
            max_w, max_h = box
            ratio = min(1.0, max_w / w, max_h / h)
            if ratio < 1:
                new_size = (max(1, round(w * ratio)), max(1, round(h * ratio)))
                im = im.resize(new_size, Image.Resampling.LANCZOS)

            # 同格式重编码（扩展名不变）
            if fmt == "JPEG":
                im.save(target, format="JPEG", quality=jpeg_quality)
            elif fmt == "WEBP":
                im.save(target, format="WEBP", quality=jpeg_quality)
            else:
                im.save(target, format=fmt)
        return True
    except Exception as exc:
        print(f"[error] 压缩失败: {source}: {exc}")
        return False


def compress_folder(input_dir, output_dir, quality="4k",
                    progress_callback=None, cancel_check=None,
                    clean=True):
    """按画质档位批量处理目录内图片，返回 (成功数, 失败数)。

    clean=True 时先清空 output_dir。original 档（box 为 None）与多帧 GIF 走
    纯复制分支（字节级一致）；其余按档位缩放 + 同格式重编码。
    progress_callback(done, total) 语义与 upscale_folder 一致。
    """
    profile = QUALITY_PROFILES.get(quality, QUALITY_PROFILES["4k"])
    box = profile["box"]
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
        print(f"[{idx}/{total}] 处理: {rel.as_posix()}")

        if box is None:
            # original 档：原样复制，不缩放不重编码
            shutil.copy2(source, target)
            success += 1
        elif source.suffix.lower() == ".gif" and _is_animated_gif(source):
            # 多帧 GIF：保守起见原样复制，避免重编码丢帧
            shutil.copy2(source, target)
            print(f"[warn] 多帧 GIF 原样复制（不缩放/重编码）: {rel.as_posix()}")
            success += 1
        elif _compress_one(source, target, box, jpeg_quality):
            success += 1
        else:
            failed += 1

        if progress_callback:
            progress_callback(idx, total)

    print(f"压缩完成: 成功 {success} 张，失败 {failed} 张")
    return success, failed
