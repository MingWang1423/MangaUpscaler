"""启动检查：waifu2x 组件、输出目录可写性。检查失败不崩溃，只返回问题列表。"""

import uuid
from pathlib import Path

from upscaler.waifu2x import REQUIRED_MODELS, WAIFU2X_EXE, WAIFU2X_MODEL_DIR


def check_waifu2x_tool():
    """返回缺失的 waifu2x 组件描述列表（空列表表示就绪）。"""
    problems = []
    if not WAIFU2X_EXE.exists():
        problems.append(f"缺少可执行文件：{WAIFU2X_EXE}")
    for name in REQUIRED_MODELS:
        if not (WAIFU2X_MODEL_DIR / name).exists():
            problems.append(f"缺少模型文件：{name}")
    return problems


def check_output_dir(output_dir):
    """检查输出目录是否可创建并写入；返回 (ok, 错误消息)。

    在目标目录内创建唯一探针文件，无论成功或异常都在 finally 中删除，
    且绝不删除输出目录本身或其中的用户文件。
    """
    path = Path(output_dir)
    probe = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write_probe_{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


def startup_checks(config):
    """返回启动问题列表，每项为 {title, details, suggestion}。"""
    issues = []
    missing = check_waifu2x_tool()
    if missing:
        issues.append({
            "title": "缺少 waifu2x 组件",
            "details": missing,
            "suggestion": "请下载完整包，或将 waifu2x-ncnn-vulkan 放到 tools/waifu2x-ncnn-vulkan/。",
        })
    output_dir = (config or {}).get("output_dir") or "output"
    ok, err = check_output_dir(output_dir)
    if not ok:
        issues.append({
            "title": "输出目录不可用",
            "details": [err],
            "suggestion": "请在设置中更换一个可写的输出目录。",
        })
    return issues
