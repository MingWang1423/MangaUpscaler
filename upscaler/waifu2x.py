import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from epub.formats import CONVERT_FOR_UPSCALE, WAIFU2X_EXTS
from errors import PipelineError
from logging_setup import get_logger
from upscaler.batch_planner import (
    REASON_ALREADY_AT_TARGET, REASON_UNREADABLE, plan_batches, temp_stem,
    temp_to_rel_path,
)

logger = get_logger("upscaler.waifu2x")

# Windows 下隐藏 waifu2x 子进程的控制台黑窗；非 Windows 平台该值无用（不传该参数）
_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)

# 取消检查的轮询间隔（秒）：子进程运行期间每轮最多阻塞这么久，然后检查一次
# cancel_check；cancel_check 为 None 时不做轮询，直接阻塞等待进程结束。
_CANCEL_POLL_INTERVAL = 0.2
# 取消时先 terminate() 等子进程自行退出，超过该秒数还没退出就 kill() 强制终止
_TERMINATE_TIMEOUT = 5.0

# Pillow 格式名 -> 扩展名：校验批处理产物「扩展名 == 真实格式」时用
_FORMAT_TO_EXT = {
    "JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "BMP": "bmp",
}
# waifu2x 目录模式支持的输出格式（-f 只接受这三种）
_WAIFU2X_OUTPUT_FORMATS = ("jpg", "png", "webp")
# 批次临时目录前缀：<任务工作目录>/batch_<序号>_<批次标签>/input|output
_BATCH_DIR_PREFIX = "batch"


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


def _check_cancel(cancel_check):
    """取消检查点：cancel_check() 返回 True 时抛 InterruptedError。

    整条超分链路（upscale_folder -> _upscale_one -> _convert_and_upscale ->
    upscale_image -> _run_waifu2x_process）都复用本函数，保证取消语义一致：
    取消只会抛 InterruptedError，绝不会被「失败回退」逻辑当成处理失败吞掉。
    cancel_check 为 None（旧式调用）时完全不检查，行为与不加该参数时一致。
    """
    if cancel_check and cancel_check():
        raise InterruptedError("用户已取消")


def _popen_kwargs():
    """waifu2x 子进程的通用 Popen 参数：捕获输出、Windows 下隐藏控制台黑窗。"""
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "errors": "replace",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _SUBPROCESS_FLAGS
    return kwargs


def _log_process_result(result):
    """记录子进程的 returncode、stdout 与 stderr（失败原因由调用方另行报错）。"""
    logger.debug("waifu2x 子进程结束: returncode=%s", result.returncode)
    if result.stdout:
        logger.debug("waifu2x stdout: %s", result.stdout.strip())
    if result.stderr:
        logger.debug("waifu2x stderr: %s", result.stderr.strip())


def _terminate_process(proc, timeout=_TERMINATE_TIMEOUT):
    """终止取消时的 waifu2x 子进程：先 terminate()，超时未退出再 kill()。

    只作用于传入的 Popen 对象，绝不去终止其它进程（例如其它并发任务）。
    """
    if proc.poll() is not None:
        return
    logger.debug("正在终止 waifu2x 子进程 (pid=%s)", proc.pid)
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        logger.warning("waifu2x 子进程未在 %.1f 秒内退出，强制终止 (pid=%s)",
                       timeout, proc.pid)
    proc.kill()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error("waifu2x 子进程仍未退出 (pid=%s)", proc.pid)


def _drain_process(proc):
    """终止后把残留输出读干净并关闭管道；读取失败不影响取消语义。

    返回 (stdout, stderr)，读取不到时返回空字符串。
    """
    try:
        stdout, stderr = proc.communicate()
    except Exception as exc:  # 已被强制终止的进程可能读不到输出
        logger.debug("读取已终止 waifu2x 子进程输出失败: %s", exc)
        return "", ""
    return stdout or "", stderr or ""


def _run_waifu2x_process(cmd, cancel_check=None, poll_interval=_CANCEL_POLL_INTERVAL):
    """用 subprocess.Popen 执行一条 waifu2x 命令，返回 subprocess.CompletedProcess。

    子进程运行期间每 poll_interval 秒检查一次 cancel_check()：用户取消时先
    terminate()、超时后 kill()，把残留输出读干净后抛 InterruptedError。
    只会终止本函数自己启动的那个进程对象。
    返回值与 subprocess.run(capture_output=True, text=True) 一致，returncode /
    stdout / stderr 全部记入日志；非零 returncode 由调用方判定如何处理。
    """
    proc = subprocess.Popen(cmd, **_popen_kwargs())
    try:
        # 不需要取消时直接阻塞等待，避免无谓轮询
        timeout = poll_interval if cancel_check else None
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                break
            except subprocess.TimeoutExpired:
                if cancel_check and cancel_check():
                    logger.warning("用户取消，正在终止 waifu2x 子进程 (pid=%s)", proc.pid)
                    _terminate_process(proc)
                    stdout, stderr = _drain_process(proc)
                    _log_process_result(subprocess.CompletedProcess(
                        cmd, proc.returncode, stdout, stderr))
                    raise InterruptedError("用户已取消") from None
    except BaseException:
        # 任何异常（含取消、Ctrl+C、cancel_check 自身出错）都不能留下孤儿进程
        if proc.poll() is None:
            _terminate_process(proc)
        raise

    result = subprocess.CompletedProcess(cmd, proc.returncode,
                                         stdout or "", stderr or "")
    _log_process_result(result)
    return result


def upscale_image(input_path, output_path, scale=2, noise=3, gpu="auto",
                  cancel_check=None):
    """用 waifu2x-ncnn-vulkan 放大单张图片，成功返回 True，失败返回 False。

    找不到可执行文件 / 模型，或 Vulkan 不可用时会抛 PipelineError 分类异常，
    由上层统一转成友好提示。
    子进程由 _run_waifu2x_process 用 subprocess.Popen 执行；GPU（自动或手动
    编号）失败时最多回退 CPU(-1) 一次，绝不无限重试。
    cancel_check 是可选的无参可调用对象，返回 True 表示用户要求取消：正在运行
    的子进程会被终止，并抛出 InterruptedError（不会触发 CPU 回退）。
    """
    exe = WAIFU2X_EXE
    if not exe.exists():
        raise PipelineError(
            "missing_waifu2x", f"找不到 waifu2x-ncnn-vulkan：{exe}"
        )

    def _run(gpu_value):
        cmd = ([str(exe), "-i", str(input_path), "-o", str(output_path),
                "-n", str(noise), "-s", str(scale)] + gpu_args(gpu_value))
        return _run_waifu2x_process(cmd, cancel_check=cancel_check)

    result = _run(gpu)
    if result.returncode == 0:
        return True

    # GPU（自动或手动编号）处理失败，回退 CPU；回退只有这一次。
    # 已取消时绝不回退，直接抛 InterruptedError。
    if gpu != -1:
        _check_cancel(cancel_check)
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


def _convert_for_waifu2x(source: Path, tmp_dir: Path, cancel_check=None,
                         temp_name=None):
    """把 waifu2x 不支持的格式（BMP/静态 GIF）转成临时 PNG；失败返回 None。

    temp_name 可指定产物文件名：批次模式下用规划器生成的唯一临时名，避免不同
    目录下的同名图片互相覆盖；默认沿用「<文件名>_in.png」。
    cancel_check 已取消时抛 InterruptedError（检查点在 try 之外，且显式
    重新抛出，绝不被下面的兜底当成「转换失败」返回 None）。
    """
    _check_cancel(cancel_check)
    tmp = tmp_dir / (temp_name or f"{source.stem}_in.png")
    try:
        with Image.open(source) as im:
            # BMP 无透明通道用 RGB；静态 GIF 可能有透明，用 RGBA 保住
            mode = "RGB" if source.suffix.lower() == ".bmp" else "RGBA"
            im.convert(mode).save(tmp, "PNG")
        return tmp
    except InterruptedError:
        raise
    except Exception as exc:
        logger.error("无法转换为临时 PNG: %s: %s", source, exc)
        return None


def _convert_back(png_path: Path, target: Path, cancel_check=None) -> bool:
    """把 waifu2x 输出的 PNG 转回原扩展名格式，保证「扩展名 == 真实格式」。

    cancel_check 已取消时抛 InterruptedError，不会写出目标文件。
    """
    _check_cancel(cancel_check)
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
    except InterruptedError:
        raise
    except Exception as exc:
        logger.error("转回 %s 失败: %s: %s", ext, target, exc)
        return False


def _convert_and_upscale(source: Path, target: Path, scale: int, noise: int, gpu,
                         cancel_check=None) -> bool:
    """BMP / 静态 GIF：转 PNG -> waifu2x -> 转回原格式。

    转换前后都检查取消；取消时抛 InterruptedError（不会被当成处理失败），
    本次生成的临时转换文件在 finally 里统一清理，目标文件不会留下半成品。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="waifu2x_convert_"))
    try:
        _check_cancel(cancel_check)
        png_in = _convert_for_waifu2x(source, tmp_dir, cancel_check=cancel_check)
        if png_in is None:
            return False
        _check_cancel(cancel_check)
        png_out = tmp_dir / "out.png"
        if not upscale_image(str(png_in), str(png_out), scale=scale, noise=noise,
                             gpu=gpu, cancel_check=cancel_check):
            return False
        _check_cancel(cancel_check)
        return _convert_back(png_out, target, cancel_check=cancel_check)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _upscale_one(source: Path, target: Path, scale: int, noise: int, gpu,
                 cancel_check=None) -> bool:
    """单张超分：waifu2x 原生格式直接超分，其余栅格格式转换后超分。

    cancel_check 会继续下传给单张超分链路（含 waifu2x 子进程）；取消时抛
    InterruptedError，绝不返回 False（否则上层会把原图当「失败回退」复制）。
    """
    ext = source.suffix.lower()
    if ext in WAIFU2X_EXTS:
        return upscale_image(str(source), str(target), scale=scale, noise=noise,
                             gpu=gpu, cancel_check=cancel_check)
    if ext in CONVERT_FOR_UPSCALE:
        return _convert_and_upscale(source, target, scale, noise, gpu,
                                    cancel_check=cancel_check)
    logger.error("不支持的栅格格式: %s", source)
    return False


def _batch_input_name(image_plan):
    """批次输入目录里的文件名。

    转换路径沿用规划器生成的临时 PNG 名；原生格式在规划器临时主名后加 _in。
    名字里带「EPUB 内部相对路径」的摘要，因此不同目录下的同名图片不会互相覆盖。
    """
    if image_plan.temp_in_name:
        return image_plan.temp_in_name
    return "%s_in.%s" % (temp_stem(image_plan.rel_path), image_plan.format)


def _batch_command(input_dir, output_dir, batch):
    """构造目录模式的 waifu2x 命令：一次进程处理整批。

    - 统一使用该批次的 scale / noise；
    - GPU 参数一律走 gpu_args()（"auto" 不传 -g，-1 传 -g -1，非负整数传编号）；
    - 显式传 -f，保证产物真实格式与预期一致（waifu2x 只支持 jpg/png/webp）。
    """
    cmd = [str(WAIFU2X_EXE), "-i", str(input_dir), "-o", str(output_dir),
           "-n", str(batch.key.noise), "-s", str(batch.key.scale)]
    if batch.key.output_format in _WAIFU2X_OUTPUT_FORMATS:
        cmd += ["-f", batch.key.output_format]
    return cmd + gpu_args(batch.key.gpu)


def _batch_produced_name(image_plan):
    """waifu2x 目录模式会在输出目录里生成的文件名。

    目录模式按「输入文件名 + 输出格式扩展名」命名产物（实测 waifu2x-ncnn-vulkan
    只替换扩展名），因此先按输入名找到产物，再统一改名成规划器的 temp_out_name。
    """
    input_name = _batch_input_name(image_plan)
    return "%s.%s" % (Path(input_name).stem, image_plan.output_format)


def _normalize_batch_outputs(batch, output_dir):
    """把目录模式的产物统一改名成规划器给出的唯一临时名。

    改名后「产物文件」与「EPUB 内部相对路径」的映射只有一个来源（规划器的
    temp_out_name），不同目录下的同名图片也不会串味。
    """
    for image_plan in batch.plans:
        if not image_plan.temp_out_name:
            continue
        produced = output_dir / _batch_produced_name(image_plan)
        wanted = output_dir / image_plan.temp_out_name
        if produced.exists() and produced != wanted:
            produced.replace(wanted)


def _has_alpha(image):
    """图片对象是否带透明通道（RGBA / LA / 带 transparency 的调色板图）。"""
    return (image.mode in ("RGBA", "LA")
            or (image.mode == "P" and "transparency" in image.info))


def _input_has_alpha(path):
    """原图是否带透明通道（只解析头部）；读不到时返回 False，不因它误判失败。"""
    try:
        with Image.open(path) as im:
            return _has_alpha(im)
    except Exception:
        return False


def _verify_batch_output(produced, image_plan, source):
    """校验批处理产物；返回 (ok, 问题描述)。

    检查：文件存在、可读、真实格式与预期一致、尺寸为「原尺寸 × 倍率」，并且
    带透明通道的 PNG 不能「无故」丢掉透明通道（目录模式不可靠时由调用方逐张
    回退到单图处理）。
    """
    if not produced.exists():
        return False, "批处理产物缺失: %s" % produced.name
    try:
        with Image.open(produced) as im:
            fmt = _FORMAT_TO_EXT.get((im.format or "").upper(), "")
            size = im.size
            alpha = _has_alpha(im)
    except Exception as exc:
        return False, "批处理产物无法读取: %s" % exc
    if fmt != image_plan.output_format:
        return False, "批处理产物格式不符: %s != %s" % (fmt, image_plan.output_format)
    expected = (image_plan.width * image_plan.scale,
                image_plan.height * image_plan.scale)
    if size != expected:
        return False, "批处理产物尺寸异常: %s != %s" % (size, expected)
    if fmt == "png" and not alpha and _input_has_alpha(source):
        return False, "批处理产物丢失透明通道"
    return True, None


def _prepare_batch_inputs(batch, input_path, input_dir, cancel_check=None):
    """把一批图片准备到批次输入目录；返回 {rel_path: 不可用原因}。

    - 原生格式（jpg/png/webp）：复制一份进批次目录，绝不改动原始提取目录里的文件；
    - BMP / 静态 GIF：用现有转换函数转成 PNG 再放进批次目录（只写临时目录）；
    - 取消时抛 InterruptedError，不当作普通失败。
    """
    problems = {}
    used = {}
    for image_plan in batch.plans:
        _check_cancel(cancel_check)
        name = _batch_input_name(image_plan)
        owner = used.get(name)
        if owner is not None and owner != image_plan.rel_path:
            # 规划器的临时名带相对路径摘要，正常不会冲突；真冲突时宁可报错也别覆盖
            raise ValueError("批次临时文件名冲突: %s（%s / %s）"
                             % (name, owner, image_plan.rel_path))
        used[name] = image_plan.rel_path
        source = input_path / image_plan.rel_path
        if image_plan.needs_conversion:
            png = _convert_for_waifu2x(source, input_dir, cancel_check=cancel_check,
                                       temp_name=name)
            if png is None:
                problems[image_plan.rel_path] = "无法转换为临时 PNG"
        else:
            shutil.copy2(source, input_dir / name)
    return problems


def _deliver_batch_output(image_plan, output_dir, output_path, input_path,
                          cancel_check=None):
    """把一张批处理产物还原到最终相对路径；返回 (ok, 问题描述)。

    先校验（存在 / 可读 / 格式 / 尺寸 / 透明通道），再按格式处理：
    BMP 与静态 GIF 用现有转换函数转回原格式，其余直接复制到目标相对路径，
    从而保证「最终扩展名 == 真实文件格式」。
    """
    produced = output_dir / image_plan.temp_out_name
    source = input_path / image_plan.rel_path
    ok, problem = _verify_batch_output(produced, image_plan, source)
    if not ok:
        return False, problem
    _check_cancel(cancel_check)
    # 目的地由规划器的「临时文件名 -> 原始相对路径」映射还原，不另起一套命名规则
    rel_path = temp_to_rel_path((image_plan,))[image_plan.temp_out_name]
    dest = output_path / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    if image_plan.needs_conversion:
        if not _convert_back(produced, dest, cancel_check=cancel_check):
            return False, "转回 %s 失败" % image_plan.target_format
        return True, None
    shutil.copy2(produced, dest)
    return True, None


def _run_batch(batch, batch_dir, input_path, output_path, cancel_check=None):
    """执行一个 waifu2x 批次：准备输入 -> 一次目录模式进程 -> 还原产物。

    返回 [(ImagePlan, ok, 问题描述或 None), ...]，顺序与 batch.plans 一致。
    批次层面失败（进程非零 / 产物缺失 / 不可读 / 格式或尺寸不符）时对应图片的
    ok 为 False，由调用方逐张回退到现有单图处理路径，绝不整批直接算成功；
    取消（InterruptedError）不会被这里当成普通失败，直接向上冒泡。
    """
    input_dir = batch_dir / "input"
    output_dir = batch_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("批处理 %s：%d 张，scale=%s noise=%s gpu=%s", batch.key.label(),
                len(batch), batch.key.scale, batch.key.noise, batch.key.gpu)

    outcomes = {}
    problems = _prepare_batch_inputs(batch, input_path, input_dir, cancel_check)
    for rel_path, problem in problems.items():
        logger.error("批处理输入准备失败（%s）：%s", rel_path, problem)
        outcomes[rel_path] = (False, problem)

    prepared = [plan for plan in batch.plans if plan.rel_path not in outcomes]
    if prepared:
        result = _run_waifu2x_process(_batch_command(input_dir, output_dir, batch),
                                      cancel_check=cancel_check)
        if result.returncode != 0:
            reason = "批处理退出码 %s" % result.returncode
            logger.error("批处理失败（%s，%d 张）：%s", batch.key.label(),
                         len(prepared), reason)
            if result.stderr:
                logger.error("%s", result.stderr.strip())
            for plan in prepared:
                outcomes[plan.rel_path] = (False, reason)
        else:
            # 目录模式按输入名产出，先统一改名成规划器的临时名，再逐张校验与还原
            _normalize_batch_outputs(batch, output_dir)
            for plan in prepared:
                ok, problem = _deliver_batch_output(plan, output_dir, output_path,
                                                    input_path, cancel_check)
                if not ok:
                    logger.warning("批处理产物不可用（%s）：%s", plan.rel_path, problem)
                outcomes[plan.rel_path] = (ok, problem)

    return [(plan, outcomes[plan.rel_path][0], outcomes[plan.rel_path][1])
            for plan in batch.plans]


def _fallback_single(image_plan, input_path, output_path, cancel_check=None):
    """把一张图片回退到现有单图处理路径；成功返回 True，失败已原样保留返回 False。

    复用 upscale_image()（其内部 GPU 失败最多回退 CPU 一次），并同样传入
    cancel_check：取消会抛 InterruptedError，绝不当成普通失败、也不复制原图。
    """
    source = input_path / image_plan.rel_path
    dest = output_path / image_plan.rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    _check_cancel(cancel_check)
    if _upscale_one(source, dest, image_plan.scale, image_plan.noise,
                    image_plan.gpu, cancel_check=cancel_check):
        return True
    shutil.copy2(source, dest)
    logger.error("超分失败，已原样保留: %s", image_plan.rel_path)
    return False


def _batch_root_for(input_path, output_path, batch_root):
    """决定批次临时目录的父目录，返回 (根目录, 是否需要本次清理)。

    优先级：
      1. 显式传入的 batch_root（例如当前任务的 TaskWorkspace.root）；
      2. 输入/输出目录的共同父目录 —— GUI 场景就是当前任务的 TaskWorkspace
         （<workspace>/extracted 与 <workspace>/upscaled 的父目录），于是得到
         <workspace>/batch_<id>/input|output；
      3. 两者没有共同父目录时，退化为「本次任务唯一的临时目录」（mkdtemp 生成、
         用完即删）。
    绝不使用项目根目录或固定的全局临时目录。
    """
    if batch_root is not None:
        return Path(batch_root), False
    in_parent = input_path.resolve().parent
    out_parent = output_path.resolve().parent
    if in_parent == out_parent:
        return in_parent, False
    return Path(tempfile.mkdtemp(prefix="waifu2x_batch_")), True


def upscale_folder(input_dir, output_dir, scale=2, noise=3, progress_callback=None,
                   clean=True, cancel_check=None, target=None, gpu="auto",
                   batch_root=None):
    """扫描目录 -> 规划批次 -> 批量放大，在 output_dir 下还原完全相同的目录结构。

    流程（分批逻辑全部来自 upscaler/batch_planner.py 的纯函数规划器）：
      1. plan_batches() 扫描图片并规划：哪些原样复制、哪些可交给 waifu2x，以及
         同一批要求哪些参数完全一致（处理方式 / 倍率 / noise / waifu2x 输入格式 /
         输出格式 / 最终格式 / GPU）；
      2. 原样复制类（SVG、多帧 GIF、已达目标分辨率、损坏或无法读取尺寸、不支持
         的格式）单独处理，绝不进入 waifu2x 批次；
      3. 每个批次在 <batch_root>/batch_<id>/input|output 下准备临时输入，并只启动
         一次 waifu2x 进程（目录输入 / 目录输出，-f 显式指定输出格式）；
      4. 按规划器的临时文件名映射把产物还原回各自的原始相对路径；静态 GIF 与
         BMP 再用现有转换函数转回原格式，保证「扩展名 == 真实文件格式」；
      5. 批次失败（进程非零 / 产物缺失 / 不可读 / 格式或尺寸不符）时逐张回退到
         现有单图处理路径（upscale_image，GPU 失败最多回退 CPU 一次）。
    clean=True 时先清空 output_dir，保证它精确镜像 input_dir。
    target=(short_limit, long_limit) 可选：按方向自适应边界做智能倍率选择，
    原图已达目标分辨率时跳过超分并原样复制；None（默认，原画质档）表示不按
    尺寸跳过，始终按 scale 超分。
    cancel_check 可选：每张图片 / 每个批次开始前检查，并继续下传给正在运行的
    waifu2x 子进程（会被终止）。取消时抛 InterruptedError 向上层传播：当前批次
    未完成的图片不计入成功、不做失败回退复制、不启动后续批次，任务工作目录按
    TaskWorkspace 规则保留。默认 None 时完全不检查。
    batch_root 可选：批次临时目录的父目录；默认用「当前任务的工作目录」（详见
    _batch_root_for）。批次目录用完即删，绝不写入项目根目录或固定全局临时目录。
    progress_callback(done, total) 的 total 是全部计划图片数量，每张图片只回调
    一次，正常结束时恰好推进到 total。
    返回 (超分成功数, 跳过数, 原样复制数, 失败数)。
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    if clean and output_path.exists():
        shutil.rmtree(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    # 扫描 + 规划：分批规则全部来自 batch_planner 的纯函数
    plans = plan_batches(input_path, scale=scale, noise=noise, gpu=gpu, target=target)
    total = len(plans.plans)
    if progress_callback:
        progress_callback(0, total)

    upscaled, skipped, copied, failed = 0, 0, 0, 0
    done = 0

    def _finish(rel_path):
        """每张图片只结算一次：计数后回调一次进度。"""
        nonlocal done
        done += 1
        logger.info("[%d/%d] 完成: %s", done, total, rel_path)
        if progress_callback:
            progress_callback(done, total)

    # 1) 原样复制类：SVG / 多帧 GIF / 已达目标分辨率 / 损坏或无法读取 / 不支持
    for image_plan in plans.copies:
        # 每张图片开始处理前检查取消；InterruptedError 直接冒泡，绝不吞掉
        _check_cancel(cancel_check)
        source = input_path / image_plan.rel_path
        dest = output_path / image_plan.rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        if image_plan.reason == REASON_ALREADY_AT_TARGET:
            skipped += 1
            logger.info("[skip] %s 已达目标分辨率，跳过超分，原样保留",
                        image_plan.rel_path)
        elif image_plan.reason == REASON_UNREADABLE:
            # 与既有语义一致：不可读取的图片计入失败，并原样保留
            failed += 1
            logger.error("图片无法读取，已原样保留: %s", image_plan.rel_path)
        else:
            copied += 1
            logger.info("[copy] %s 原样保留（%s）", image_plan.rel_path,
                        image_plan.reason)
        _finish(image_plan.rel_path)

    # 2) 按批次批量超分：一个批次只启动一次 waifu2x 进程
    batch_root_path, ephemeral = _batch_root_for(input_path, output_path, batch_root)
    try:
        for index, batch in enumerate(plans.batches, start=1):
            # 取消后不启动后续批次（InterruptedError 直接冒泡）
            _check_cancel(cancel_check)
            batch_dir = batch_root_path / ("%s_%02d_%s"
                                           % (_BATCH_DIR_PREFIX, index, batch.key.label()))
            try:
                results = _run_batch(batch, batch_dir, input_path, output_path,
                                     cancel_check)
            finally:
                # 批次结束（含取消）都清理本批次目录；任务工作目录本身不动
                shutil.rmtree(batch_dir, ignore_errors=True)
            for image_plan, ok, problem in results:
                if ok:
                    upscaled += 1
                elif _fallback_single(image_plan, input_path, output_path,
                                      cancel_check):
                    # 批次产物不可用时逐张回退单图处理，仍算超分成功
                    logger.info("[fallback] %s 已回退单图处理（%s）",
                                image_plan.rel_path, problem)
                    upscaled += 1
                else:
                    failed += 1
                _finish(image_plan.rel_path)
    finally:
        if ephemeral:
            shutil.rmtree(batch_root_path, ignore_errors=True)

    logger.info("放大完成: 超分 %d 张，跳过 %d 张，原样复制 %d 张，失败 %d 张",
                upscaled, skipped, copied, failed)
    return upscaled, skipped, copied, failed

