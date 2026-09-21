"""配置字段定义、候选值与严格校验（纯函数，可独立测试）。"""

SCALE_OPTIONS = (2, 4)
NOISE_OPTIONS = (-1, 0, 1, 2, 3)
QUALITY_OPTIONS = ("original", "4k", "2k")
# GUI 常用 GPU 选项；配置层额外允许任意非负整数（为后续 GPU 自动检测预留空间）
GPU_GUI_OPTIONS = ("auto", -1, 0, 1, 2)

LEGACY_QUALITY_MAP = {"balanced": "4k", "small": "2k"}

DEFAULT_CONFIG = {
    "scale": 2,
    "noise": 3,
    "quality": "4k",
    "gpu": "auto",
}


def _is_int(value):
    """True 仅当 value 是真 int（bool 是 int 的子类，显式排除）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_gpu(value):
    """GPU 配置合法："auto"、-1（CPU）、或任意非负整数。

    明确拒绝 bool、除 -1 外的负数、浮点数、空字符串和其它非法类型。
    """
    if isinstance(value, str) and value == "auto":
        return True
    if isinstance(value, bool):
        return False
    return isinstance(value, int) and value >= -1


def sanitize_config(data, default_output_dir):
    """把任意输入规范化成合法配置；返回 (config, changed)。

    changed 为 True 表示存在缺失/非法字段（发生过迁移或修复），调用方据此决定
    是否原子写回。配置文件缺失、JSON 损坏或字段非法时，都回退到默认值。
    """
    config = dict(DEFAULT_CONFIG)
    config["output_dir"] = default_output_dir
    if not isinstance(data, dict):
        return config, True

    changed = False

    value = data.get("scale")
    if _is_int(value) and value in SCALE_OPTIONS:
        config["scale"] = value
    else:
        changed = True

    value = data.get("noise")
    if _is_int(value) and value in NOISE_OPTIONS:
        config["noise"] = value
    else:
        changed = True

    value = data.get("quality")
    if isinstance(value, str) and value.strip():
        value = LEGACY_QUALITY_MAP.get(value, value)
        if value in QUALITY_OPTIONS:
            config["quality"] = value
        else:
            changed = True
    else:
        changed = True

    value = data.get("gpu")
    if _valid_gpu(value):
        config["gpu"] = value
    else:
        changed = True

    value = data.get("output_dir")
    if isinstance(value, str) and value.strip():
        config["output_dir"] = value
    else:
        changed = True

    return config, changed
