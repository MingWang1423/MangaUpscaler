import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

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


# exe 路径（模块级常量，兼容开发环境与 PyInstaller 打包）
WAIFU2X_EXE = (
    _resource_root() / "tools" / "waifu2x-ncnn-vulkan" / "waifu2x-ncnn-vulkan.exe"
)


def upscale_image(input_path, output_path, scale=2, noise=3, gpuid=0):
    """用 waifu2x-ncnn-vulkan 放大单张图片，成功返回 True，失败返回 False。"""
    exe = WAIFU2X_EXE
    if not exe.exists():
        print(f"错误: 找不到 waifu2x-ncnn-vulkan，请先下载到 {exe}")
        return False

    def _run(gpu_id):
        cmd = [str(exe), "-i", str(input_path), "-o", str(output_path),
               "-n", str(noise), "-s", str(scale), "-g", str(gpu_id)]
        kwargs = dict(capture_output=True, text=True, errors="replace")
        if sys.platform == "win32":
            # 隐藏子进程（waifu2x-ncnn-vulkan.exe）的控制台黑窗
            kwargs["creationflags"] = _SUBPROCESS_FLAGS
        return subprocess.run(cmd, **kwargs)

    result = _run(gpuid)
    if result.returncode == 0:
        return True

    # GPU 处理失败，自动回退到 CPU
    if gpuid >= 0:
        print(f"警告: GPU(gpuid={gpuid}) 处理失败，回退 CPU(gpuid=-1)...")
        result = _run(-1)
        if result.returncode == 0:
            return True

    print(f"放大失败: {input_path}")
    if result.stderr:
        print(result.stderr)
    return False


def iter_images(root):
    """递归列出 root 下所有图片，返回按 POSIX 相对路径排序的相对路径列表。

    返回相对路径（而不是绝对路径）是为了让调用方能在输出目录里重建同样的
    目录结构，并保证不同平台上的处理顺序一致。
    """
    root_path = Path(root)
    relatives = [p.relative_to(root_path) for p in root_path.rglob("*")
                 if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(relatives, key=lambda rel: rel.as_posix())


def _read_image_size(path):
    """读取图片宽高 (宽, 高)；Pillow 懒加载，只解析头部不解码整张图。

    损坏或不支持的文件返回 None，调用方据此决定不跳过、交给超分阶段处理。
    """
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def _exceeds_bounds(size, bounds):
    """判断图片尺寸是否放不进边界框：任一边超出即返回 True。"""
    w, h = size
    max_w, max_h = bounds
    return w > max_w or h > max_h


def upscale_folder(input_dir, output_dir, scale=2, noise=3, progress_callback=None,
                   clean=True, cancel_check=None, skip_if_larger_than=None):
    """递归批量放大目录内所有图片，在 output_dir 下生成完全相同的目录结构。

    clean=True 时先清空 output_dir，保证它精确镜像 input_dir（不混入上一次
    的残留文件，避免把别的书的放大结果打包进这本书）。
    progress_callback(done, total) 会在开始处理前以 (0, total) 调用一次（方便
    界面先设好进度条上限），之后每处理完一张图片再调用一次。
    cancel_check 是可选的无参可调用对象，返回 True 表示调用方要求取消；每张
    图片开始处理前检查一次，被取消时抛 InterruptedError（子进程已启动的那张
    会跑完，属于协作式取消的正常边界）。默认 None 时完全不检查，行为与不加
    该参数时一模一样。
    skip_if_larger_than=(max_w, max_h) 可选：原图任一边超出边界框时跳过超分，
    直接复制原图（字节级一致）；None（默认）表示所有图都走超分。
    返回 (超分成功数, 失败数, 跳过数)。
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

    success, failed, skipped = 0, 0, 0
    for idx, rel in enumerate(images, start=1):
        if cancel_check and cancel_check():
            raise InterruptedError("用户已取消")
        source = input_path / rel
        target = output_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"[{idx}/{total}] 处理: {rel.as_posix()}")

        size = None
        should_skip = False
        if skip_if_larger_than is not None:
            size = _read_image_size(source)
            if size is not None and _exceeds_bounds(size, skip_if_larger_than):
                should_skip = True

        if should_skip:
            shutil.copy2(source, target)
            w, h = size
            max_w, max_h = skip_if_larger_than
            print(f"[skip] {rel.as_posix()} ({w}x{h}) 超出边界框 ({max_w}x{max_h})，跳过超分")
            skipped += 1
        elif upscale_image(source, target, scale=scale, noise=noise):
            success += 1
        else:
            failed += 1

        if progress_callback:
            progress_callback(idx, total)

    print(f"放大完成: 超分 {success} 张，跳过 {skipped} 张，失败 {failed} 张")
    return success, failed, skipped

