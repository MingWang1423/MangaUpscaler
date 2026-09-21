"""图片格式的统一定义与判定。

reader / waifu2x / compressor 三处共用这里唯一的扩展名集合与格式分类，
避免各自维护不同的扩展名集合导致行为不一致。所有判定都基于名称/文件内容，
不依赖程序当前工作目录。
"""

from pathlib import Path, PurePosixPath

from PIL import Image

# 全部会被当作「图片」处理的扩展名（提取端判定；SVG 也纳入，原样保留不做像素超分）
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
}

# waifu2x-ncnn-vulkan 原生支持的输入/输出格式（见 tools/waifu2x-ncnn-vulkan/README.md）
WAIFU2X_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# 原样保留、不做任何像素处理/重编码的格式（字节级一致）
PASSTHROUGH_EXTS = {".svg"}

# 需要临时转成 PNG 再交给 waifu2x 的格式（超分后转回原格式，保证扩展名 == 真实格式）。
# 注意：.gif 只有「静态 GIF」才走这条转换路径，多帧 GIF 由调用方先行原样保留。
CONVERT_FOR_UPSCALE = {".bmp", ".gif"}


def suffix_of(name):
    """从 arcname / 文件名提取小写扩展名（含点），统一处理 '/' 与 '\\'。"""
    return PurePosixPath(str(name).replace("\\", "/")).suffix.lower()


def is_image_name(name):
    """按扩展名判断是否属于图片（提取端判定）。"""
    return suffix_of(name) in IMAGE_EXTS


def iter_images(root):
    """递归列出 root 下所有图片，返回按 POSIX 相对路径排序的相对路径列表。

    返回相对路径（而不是绝对路径）是为了让调用方能在输出目录里重建同样的
    目录结构，并保证不同平台上的处理顺序一致。
    """
    root_path = Path(root)
    relatives = [p.relative_to(root_path) for p in root_path.rglob("*")
                 if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(relatives, key=lambda rel: rel.as_posix())


def _probe(path):
    """读取图片格式信息；无法识别/损坏的文件返回 None。"""
    try:
        with Image.open(path) as im:
            return {
                "format": (im.format or "").upper(),
                "is_animated": bool(getattr(im, "is_animated", False)),
                "n_frames": int(getattr(im, "n_frames", 1)),
            }
    except Exception:
        return None


def is_animated_gif(path):
    """判断 GIF 是否多帧（>1 帧）；损坏/非 GIF 返回 False。"""
    info = _probe(path)
    return bool(info and info["is_animated"] and info["n_frames"] > 1)
