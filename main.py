"""程序入口：读取配置、启动主窗口。

配置的读写统一放在这里（load_config / save_config），再通过依赖注入交给
MainWindow 使用：main.py 作为脚本运行时模块名是 __main__，gui 层无法
`from main import ...`，注入回调可以彻底避开循环导入与模块名的坑。
"""

import json
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow

APP_NAME = "MangaUpscaler"
CONFIG_FILENAME = "config.json"
DEFAULT_CONFIG = {"scale": 2, "noise": 3}


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


def load_config(path=None) -> dict:
    """读取 config.json；文件缺失、损坏或字段非法时静默回退默认值，绝不抛异常。

    output_dir 字段永远返回有效字符串：文件里没有、为空串、或类型不对时，
    一律回退到 get_default_output_dir()。旧版配置（只有 scale/noise、缺
    output_dir）会在读取时自动补全并写回，防止后续 os.path.join 拿到空串。
    """
    config_path = Path(path) if path else get_config_path()
    config = dict(DEFAULT_CONFIG)
    config["output_dir"] = get_default_output_dir()
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # 文件不存在 / 无权限 / JSON 语法错误：一律按默认值继续运行
        return config

    if isinstance(data, dict):
        # 只认这一对已知键，且必须是 int（bool 是 int 的子类，显式排除）
        for key in DEFAULT_CONFIG:
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                config[key] = value

        # output_dir：有效非空字符串才采用，否则回退默认；字段缺失视为旧配置迁移
        out_dir = data.get("output_dir")
        if isinstance(out_dir, str) and out_dir.strip():
            config["output_dir"] = out_dir
        elif "output_dir" not in data:
            save_config(config, config_path)
    return config


def save_config(config, path=None) -> None:
    """把配置写入 config.json；写失败只提示，不影响程序继续运行。"""
    config_path = Path(path) if path else get_config_path()
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # indent=4 便于用户手动编辑；ensure_ascii=False 保留可读性
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=4)
            fh.write("\n")
    except OSError as exc:
        print(f"警告: 无法写入配置文件 {config_path}: {exc}")


def ensure_config(path=None) -> dict:
    """读取配置；若 config.json 不存在则用默认值生成，保证文件一定存在。

    供 main() 与测试脚本共用，避免把「首次生成」的逻辑复制多份。
    """
    config_path = Path(path) if path else get_config_path()
    config = load_config(config_path)
    if not config_path.exists():
        # 首次启动：自动生成默认配置文件
        save_config(config, config_path)
        print(f"已创建默认配置文件: {config_path}")
    return config


def main() -> None:
    app = QApplication(sys.argv)
    # AppConfigLocation 依赖 applicationName，必须在取配置路径之前设置
    app.setApplicationName(APP_NAME)

    config = ensure_config()
    window = MainWindow(config, save_config_callback=save_config,
                        default_output_dir=get_default_output_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

