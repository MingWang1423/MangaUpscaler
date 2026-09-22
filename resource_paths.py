"""统一的资源路径解析（兼容源码运行与 PyInstaller 打包运行）。

打包后（PyInstaller）：
  - onedir（本项目 MangaUpscaler.spec 用的就是 --onedir）：sys.frozen 为 True，
    sys._MEIPASS 指向 dist/MangaUpscaler/_internal，datas 里的资源都在那里；
  - onefile：sys._MEIPASS 指向运行时解压出来的临时目录；
  - 万一拿不到 _MEIPASS，退化为「exe 所在目录」。
源码运行时：以本文件所在目录（项目根）为资源根目录。

因此本模块**不依赖当前工作目录**，也不假设调用方的 __file__ 层级，
GUI、spec 与诊断代码可以共用同一份路径规则。
"""

import sys
from pathlib import Path

# 资源目录名与图标相对路径（spec 与 GUI 共用同一份常量，避免两边写歪）
RESOURCES_DIR = "resources"
APP_ICON_NAME = "app.ico"
APP_ICON_RELATIVE_PATH = f"{RESOURCES_DIR}/{APP_ICON_NAME}"


def resource_root() -> Path:
    """返回资源根目录：打包后是解压目录，源码运行时是项目根目录。"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(*parts) -> Path:
    """按「资源根目录 + 相对路径片段」拼出绝对路径；不检查文件是否存在。

    片段里的 "/" 与 "\\" 都当作层级分隔符，所以 Windows 上直接传
    "resources/app.ico" 也能正确拼接。文件缺失时只返回不存在的路径，
    由调用方决定如何降级，绝不抛异常。
    """
    path = resource_root()
    for part in parts:
        for piece in str(part).replace("\\", "/").split("/"):
            if piece and piece != ".":
                path = path / piece
    return path


def app_icon_path() -> Path:
    """返回应用图标 resources/app.ico 的绝对路径。"""
    return resource_path(APP_ICON_RELATIVE_PATH)
