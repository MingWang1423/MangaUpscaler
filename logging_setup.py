"""统一日志配置：用户可写目录、大小轮转、安全降级。

日志不依赖当前工作目录：开发环境放项目根目录 logs/，打包环境放系统用户数据
目录。日志只记录流程与错误，绝不记录图片内容或其它敏感数据。
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "MangaUpscaler"
LOG_FILENAME = "manga_upscaler.log"
MAX_BYTES = 1_000_000      # 单文件 1MB 即轮转
BACKUP_COUNT = 3           # 保留 3 份历史

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def _log_dir() -> Path:
    if getattr(sys, "frozen", False):
        try:
            from PySide6.QtCore import QStandardPaths
            base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
            if base:
                return Path(base) / "logs"
        except Exception:
            pass
        return Path(sys.executable).resolve().parent / "logs"
    return Path(__file__).resolve().parent / "logs"


def get_log_path() -> Path:
    return _log_dir() / LOG_FILENAME


def setup_logging(log_path=None) -> logging.Logger:
    """配置应用日志；返回 "MangaUpscaler" logger。

    幂等：重复调用会先安全移除并关闭旧 handler，不会叠加多个。文件日志创建失败时
    降级：stderr 可用用 StreamHandler(sys.stderr)，否则用 NullHandler，绝不崩溃。
    """
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()   # 关闭底层文件，避免资源泄漏/占用
        except Exception:
            pass

    path = Path(log_path) if log_path else get_log_path()
    formatter = logging.Formatter(_FORMAT)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except Exception as exc:
        _install_fallback_handler(logger, formatter, path, exc)
    return logger


def _install_fallback_handler(logger, formatter, path, exc) -> None:
    """文件日志创建失败时的安全降级，只保证「记录日志本身」不会二次崩溃。"""
    stream = sys.stderr
    if stream is not None:
        try:
            handler = logging.StreamHandler(stream)
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.warning("日志目录不可写，已降级输出到 stderr: %s (%s)", path, exc)
            return
        except Exception:
            pass
    # stderr 不可用（如 PyInstaller console=False 下为 None）或创建失败：静默降级
    logger.addHandler(logging.NullHandler())


def get_logger(name: str = "") -> logging.Logger:
    """返回子 logger；name 会出现在日志的模块字段里（如 epub.builder）。"""
    return logging.getLogger(f"{APP_NAME}.{name}" if name else APP_NAME)
