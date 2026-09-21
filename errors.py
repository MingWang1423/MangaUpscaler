"""自定义异常与错误分类（供 GUI 显示友好信息）。"""

import errno
import traceback


class PipelineError(Exception):
    """带分类的流水线错误；kind 用于映射到友好提示。"""

    def __init__(self, kind, message, suggestion=""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.suggestion = suggestion


_PRESETS = {
    "missing_waifu2x": {
        "title": "缺少 waifu2x",
        "suggestion": "请将 waifu2x-ncnn-vulkan 完整放到 tools/ 目录，或重新下载完整包。",
    },
    "no_vulkan": {
        "title": "未检测到可用的 Vulkan 设备",
        "suggestion": "请更新显卡驱动；或在设置里把 GPU 选为 CPU 处理。",
    },
    "disk_full": {
        "title": "磁盘空间不足",
        "suggestion": "请清理磁盘空间后重试。",
    },
    "not_writable": {
        "title": "输出目录不可写",
        "suggestion": "请更换一个有写入权限的输出目录。",
    },
    "generic": {
        "title": "处理失败",
        "suggestion": "",
    },
}


def classify_error(exc):
    """把异常分类成 (kind, title, message, suggestion)。"""
    if isinstance(exc, PipelineError):
        preset = _PRESETS.get(exc.kind, _PRESETS["generic"])
        suggestion = exc.suggestion or preset["suggestion"]
        return (exc.kind, preset["title"], exc.message, suggestion)
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            p = _PRESETS["disk_full"]
            return ("disk_full", p["title"], str(exc), p["suggestion"])
        if exc.errno in (errno.EACCES, errno.EPERM):
            p = _PRESETS["not_writable"]
            return ("not_writable", p["title"], str(exc), p["suggestion"])
    p = _PRESETS["generic"]
    return ("generic", p["title"], str(exc), p["suggestion"])


def format_details(exc):
    """返回完整异常堆栈字符串（供「复制详细信息」）。"""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
