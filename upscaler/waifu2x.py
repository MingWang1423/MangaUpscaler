import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from epub.formats import (
    CONVERT_FOR_UPSCALE, PASSTHROUGH_EXTS, WAIFU2X_EXTS,
    is_animated_gif, iter_images,
)
from errors import PipelineError
from logging_setup import get_logger
from upscaler.resolution import needed_scale

logger = get_logger("upscaler.waifu2x")

# Windows 下隐藏 waifu2x 子进程的控制台黑窗；非 Windows 平台该值无用（不传该参数）
_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)


def _resource_root() -> Path:
    """返回打包资源根目录。

    PyInstaller 打包后（--onedir）：sys.frozen 为 True，sys._MEIPASS 指向
    dist/MangaUpscaler/_internal（解压资源目录）；
    开发环境（python 直接跑）：用项目根目录（本文件的上上级）。
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


# exe 路径与必需模型（模块级常量，兼容开发环境与 PyInstaller 打包）
WAIFU2X_EXE = (
    _resource_root() / "tools" / "waifu2x-ncnn-vulkan" / "waifu2x-ncnn-vulkan.exe"
)
WAIFU2X_MODEL_DIR = _resource_root() / "tools" / "waifu2x-ncnn-vulkan" / "models-cunet"
# GUI 只提供 2×/4×（4× 由 2.0x 模型重复套用），因此只需 2.0x 系列模型
REQUIRED_MODELS = ["scale2.0x_model.bin", "scale2.0x_model.param"] + [
    f"noise{n}_scale2.0x_model.{ext}" for n in range(4) for ext in ("bin", "param")
]


def gpu_args(gpu):
    """生成 waifu2x 的 GPU 命令行参数片段。

    "auto" 表示自动选择 GPU（不传 -g）；-1 表示 CPU（-g -1）；非负整数表示
    手动指定 GPU 编号（-g <编号>）。所有 waifu2x 调用都必须复用本函数，
    禁止在不同位置分别拼接 GPU 参数。
    """
    if gpu == "auto":
        return []
    return ["-g", str(gpu)]


def upscale_image(input_path, output_path, scale=2, noise=3, gpu="auto"):
    """用 waifu2x-ncnn-vulkan 放大单张图片，成功返回 True，失败返回 False。

    找不到可执行文件 / 模型，或 Vulkan 不可用时会抛 PipelineError 分类异常，
    由上层统一转成友好提示。
    """
    exe = WAIFU2X_EXE
    if not exe.exists():
        raise PipelineError(
            "missing_waifu2x", f"找不到 waifu2x-ncnn-vulkan：{exe}"
        )

    def _run(gpu_value):
        cmd = ([str(exe), "-i", str(input_path), "-o", str(output_path),
                "-n", str(noise), "-s", str(scale)] + gpu_args(gpu_value))
        kwargs = dict(capture_output=True, text=True, errors="replace")
        if sys.platform == "win32":
            # 隐藏子进程（waifu2x-ncnn-vulkan.exe）的控制台黑窗
            kwargs["creationflags"] = _SUBPROCESS_FLAGS
        return subprocess.run(cmd, **kwargs)

    result = _run(gpu)
    if result.returncode == 0:
        return True

    # GPU（自动或手动编号）处理失败，回退 CPU
    if gpu != -1:
        logger.warning("GPU(%s) 处理失败，回退 CPU(-1)...", gpu)
        result = _run(-1)
        if result.returncode == 0:
            return True

    stderr = (result.stderr or "").lower()
    if "vulkan" in stderr or "vkcreateinstance" in stderr or "vkinstance" in stderr:
        raise PipelineError("no_vulkan", "waifu2x 无法初始化 Vulkan 设备")

    logger.error("放大失败: %s", input_path)
    if result.stderr:
        logger.error("%s", result.stderr.strip())
    return False


def _convert_for_waifu2x(source: Path, tmp_dir: Path):
    """把 waifu2x 不支持的格式（BMP/静态 GIF）转成临时 PNG；失败返回 None。"""
    tmp = tmp_dir / f"{source.stem}_in.png"
    try:
        with Image.open(source) as im:
            # BMP 无透明通道用 RGB；静态 GIF 可能有透明，用 RGBA 保住
            mode = "RGB" if source.suffix.lower() == ".bmp" else "RGBA"
            im.convert(mode).save(tmp, "PNG")
        return tmp
    except Exception as exc:
        logger.error("无法转换为临时 PNG: %s: %s", source, exc)
        return None


def _convert_back(png_path: Path, target: Path) -> bool:
    """把 waifu2x 输出的 PNG 转回原扩展名格式，保证「扩展名 == 真实格式」。"""
    ext = target.suffix.lower()
    try:
        with Image.open(png_path) as im:
            if ext == ".bmp":
                im.convert("RGB").save(target, "BMP")
            elif ext == ".gif":
                # 静态 GIF：转回单帧调色板 GIF，绝不生成多帧
                im.convert("P", palette=Image.ADAPTIVE).save(target, "GIF")
            else:
                return False
        return True
    except Exception as exc:
        logger.error("转回 %s 失败: %s: %s", ext, target, exc)
        return False


def _convert_and_upscale(source: Path, target: Path, scale: int, noise: int, gpu) -> bool:
    """BMP / 静态 GIF：转 PNG -> waifu2x -> 转回原格式。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="waifu2x_convert_"))
    try:
        png_in = _convert_for_waifu2x(source, tmp_dir)
        if png_in is None:
            return False
        png_out = tmp_dir / "out.png"
        if not upscale_image(str(png_in), str(png_out), scale=scale, noise=noise, gpu=gpu):
            return False
        return _convert_back(png_out, target)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _upscale_one(source: Path, target: Path, scale: int, noise: int, gpu) -> bool:
    """单张超分：waifu2x 原生格式直接超分，其余栅格格式转换后超分。"""
    ext = source.suffix.lower()
    if ext in WAIFU2X_EXTS:
        return upscale_image(str(source), str(target), scale=scale, noise=noise, gpu=gpu)
    if ext in CONVERT_FOR_UPSCALE:
        return _convert_and_upscale(source, target, scale, noise, gpu)
    logger.error("不支持的栅格格式: %s", source)
    return False


def _read_image_size(path):
    """读取图片宽高 (宽, 高)；Pillow 懒加载，只解析头部不解码整张图。

    损坏或不支持的文件返回 None，调用方据此决定不跳过、交给超分阶段处理。
    """
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def upscale_folder(input_dir, output_dir, scale=2, noise=3, progress_callback=None,
                   clean=True, cancel_check=None, target=None, gpu="auto"):
    """递归批量放大目录内所有图片，在 output_dir 下生成完全相同的目录结构。

    处理策略（格式判定集中在 epub/formats.py）：
      - JPG/JPEG/PNG/WebP：直接交给 waifu2x；
      - SVG：原样保留，不做像素超分；
      - 多帧 GIF：字节级原样保留，不超分、不重编码；
      - 静态 GIF / BMP：临时转 PNG -> waifu2x -> 转回原格式（扩展名与真实格式一致）；
      - 超分失败 / 损坏图片：记录错误并回退为原样复制，不阻断整本 EPUB。
    clean=True 时先清空 output_dir，保证它精确镜像 input_dir。
    target=(short_limit, long_limit) 可选：按方向自适应边界做智能倍率选择，
    原图已达目标分辨率时跳过超分并原样复制；None（默认，原画质档）表示不按
    尺寸跳过，始终按 scale 超分。
    返回 (超分成功数, 跳过数, 原样复制数, 失败数)。
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    if clean and output_path.exists():
        shutil.rmtree(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    images = iter_images(input_path)

    total = len(images)
    if progress_callback:
        progress_callback(0, total)

    upscaled, skipped, copied, failed = 0, 0, 0, 0
    for idx, rel in enumerate(images, start=1):
        if cancel_check and cancel_check():
            raise InterruptedError("用户已取消")
        source = input_path / rel
        dest = output_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        ext = rel.suffix.lower()
        logger.info("[%d/%d] 处理: %s", idx, total, rel.as_posix())

        # SVG：原样保留，不做像素超分
        if ext in PASSTHROUGH_EXTS:
            shutil.copy2(source, dest)
            logger.info("[copy] %s 为 SVG，不做像素超分，原样保留", rel.as_posix())
            copied += 1
            if progress_callback:
                progress_callback(idx, total)
            continue

        # 多帧 GIF：字节级原样保留，绝不重编码成单帧
        if ext == ".gif" and is_animated_gif(source):
            shutil.copy2(source, dest)
            logger.info("[copy] %s 为多帧 GIF，不做超分/重编码，原样保留", rel.as_posix())
            copied += 1
            if progress_callback:
                progress_callback(idx, total)
            continue

        # 智能倍率选择：原图已达目标分辨率则跳过超分（交由压缩阶段按需缩小）
        use_scale = scale
        if target is not None:
            size = _read_image_size(source)
            if size is not None:
                w, h = size
                short_limit, long_limit = target
                use_scale = needed_scale(w, h, scale, short_limit, long_limit)
                if use_scale == 1:
                    shutil.copy2(source, dest)
                    logger.info("[skip] %s (%dx%d) 已达目标分辨率，跳过超分，原样保留",
                                rel.as_posix(), w, h)
                    skipped += 1
                    if progress_callback:
                        progress_callback(idx, total)
                    continue

        # 正常超分 / 转换后超分；失败则回退为原样复制，不阻断整本 EPUB
        if _upscale_one(source, dest, use_scale, noise, gpu):
            upscaled += 1
        else:
            shutil.copy2(source, dest)
            logger.error("超分失败，已原样保留: %s", rel.as_posix())
            failed += 1

        if progress_callback:
            progress_callback(idx, total)

    logger.info("放大完成: 超分 %d 张，跳过 %d 张，原样复制 %d 张，失败 %d 张",
                upscaled, skipped, copied, failed)
    return upscaled, skipped, copied, failed

