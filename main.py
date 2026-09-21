"""程序入口：读取配置、配置日志、做启动检查、启动主窗口。

配置的读写统一放在这里（load_config / save_config），再通过依赖注入交给
MainWindow 使用：main.py 作为脚本运行时模块名是 __main__，gui 层无法
`from main import ...`，注入回调可以彻底避开循环导入与模块名的坑。
"""

import json
import os
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QMessageBox

from config_schema import DEFAULT_CONFIG, sanitize_config
from diagnostics import startup_checks
from gui.main_window import MainWindow
from logging_setup import get_logger, setup_logging

APP_NAME = "MangaUpscaler"
CONFIG_FILENAME = "config.json"

logger = get_logger("main")


def get_config_path() -> Path:
    """返回 config.json 的读写路径，兼顾开发环境与 PyInstaller 打包后的可写性。

    开发环境（python main.py）：放在项目根目录，方便直接查看和手动编辑。
    打包环境（PyInstaller exe）：放在用户配置目录（Windows 为
    %LOCALAPPDATA%/MangaUpscaler/config.json），保证 exe 即使装在
    Program Files 这类只读目录里也能写配置；万一取不到用户目录，
    再兜底到 exe 同级目录。
    """
    if getattr(sys, "frozen", False):
        base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
        if not base:
            base = str(Path(sys.executable).resolve().parent)
        return Path(base) / CONFIG_FILENAME
    return Path(__file__).resolve().parent / CONFIG_FILENAME


def get_default_output_dir() -> str:
    """返回默认输出目录。

    开发环境（python main.py）：项目根目录下的 output 文件夹（相对路径）。
    打包环境（PyInstaller exe）：系统文档目录下的 MangaUpscaler/output，
    避免 exe 装在只读目录（如 Program Files）时无法写输出。
    """
    if getattr(sys, "frozen", False):
        base = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        if not base:
            base = str(Path(sys.executable).resolve().parent)
        return str(Path(base) / "MangaUpscaler" / "output")
    return "output"


def _write_config_atomic(config, config_path) -> None:
    """临时文件 + os.replace 原子写入，避免突然断电产生半个 JSON。"""
    config_path = Path(config_path)
    tmp = None
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_name(f"{config_path.name}.tmp-{uuid.uuid4().hex}")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=4)
            fh.write("\n")
        os.replace(tmp, config_path)
    except OSError as exc:
        logger.warning("无法写入配置文件 %s: %s", config_path, exc)
        if tmp is not None:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass


def save_config(config, path=None) -> None:
    """把配置原子写入 config.json；写失败只记录日志，不影响程序继续运行。"""
    _write_config_atomic(config, Path(path) if path else get_config_path())


def load_config(path=None) -> dict:
    """读取并严格校验 config.json；缺失/损坏/非法字段一律回退默认值，绝不抛异常。

    字段非法或缺失（迁移/修复）时会原子写回修复后的配置；文件不存在时只返回
    默认值（由 ensure_config 负责生成文件）。
    """
    config_path = Path(path) if path else get_config_path()
    default_output = get_default_output_dir()
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {**DEFAULT_CONFIG, "output_dir": default_output}
    except (OSError, ValueError) as exc:
        # JSON 损坏 / 无权限：回退默认并尝试写回修复
        logger.warning("配置文件不可用，回退默认值: %s (%s)", config_path, exc)
        config = {**DEFAULT_CONFIG, "output_dir": default_output}
        _write_config_atomic(config, config_path)
        return config

    config, changed = sanitize_config(data, default_output)
    if changed:
        logger.info("配置文件已迁移/修复: %s", config_path)
        _write_config_atomic(config, config_path)
    return config


def ensure_config(path=None) -> dict:
    """读取配置；若 config.json 不存在则用默认值生成，保证文件一定存在。"""
    config_path = Path(path) if path else get_config_path()
    config = load_config(config_path)
    if not config_path.exists():
        save_config(config, config_path)
        logger.info("已创建默认配置文件: %s", config_path)
    return config


def _show_startup_warnings(config) -> None:
    """把启动检查发现的问题汇总成一个可读对话框，给出修复建议，不崩溃。"""
    issues = startup_checks(config)
    if not issues:
        return
    lines = []
    for issue in issues:
        lines.append(f"• {issue['title']}")
        for detail in issue["details"]:
            lines.append(f"    {detail}")
        if issue["suggestion"]:
            lines.append(f"    建议：{issue['suggestion']}")
    QMessageBox.warning(None, "启动检查", "\n".join(lines))


def main() -> None:
    app = QApplication(sys.argv)
    # AppConfigLocation / AppDataLocation 依赖 applicationName，必须先设置
    app.setApplicationName(APP_NAME)

    setup_logging()
    config = ensure_config()
    _show_startup_warnings(config)

    window = MainWindow(config, save_config_callback=save_config,
                        default_output_dir=get_default_output_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

