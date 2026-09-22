# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（--onedir + --windowed）。

体积裁剪（三层，全部在打包期生效，重打包不会回退）：
  1) datas 白名单：tools/waifu2x-ncnn-vulkan 只保留运行必需的模型。
     界面放大倍数只有 2/4（gui/main_window.py 的 SCALE_OPTIONS），且 4x 由
     exe 内部重复套用 2.0x 模型实现（exe 里不存在 scale4.0x 模板），因此
     1x 模型 noise{N}_model.*（只在 scale=1 时加载）与从未被 -m 引用的
     models-upconv_7_* 两个模型目录都不需要打包。
  2) a.datas 过滤：去掉 PySide6/translations/*.qm（程序未安装 QTranslator，
     Qt 不会加载它们，删除是行为等价的）。
  3) a.binaries 过滤：去掉用不到的 Qt 插件与 Pillow 的 AVIF 扩展。
     - platforms 只留 qwindows.dll（qdirect2d/qoffscreen/qminimal 用不到）
     - imageformats 只留 qico.dll（应用不做 Qt 图片解码，图片全走 Pillow；
       PNG/BMP 由 QtGui 内置支持）
     - tls / generic / networkinformation / platforminputcontexts 整个删掉
     - styles（控件外观）与 iconengines 保留
     - PIL/_avif.*.pyd：AvifImagePlugin 与 Image.init() 都有 try/except
       ImportError 保护，且 IMAGE_EXTS 不含 .avif

调试用：设 MANGA_UPSCALER_FULL_PACKAGE=1 可跳过全部裁剪，打出裁剪前的完整包，
便于 A/B 对比与排查。
"""

import os
from pathlib import Path

FULL_PACKAGE = os.environ.get("MANGA_UPSCALER_FULL_PACKAGE") == "1"

# ---------------------------------------------------------------- tools 白名单
_WAIFU2X_SRC = Path("tools/waifu2x-ncnn-vulkan")
_WAIFU2X_DEST = "tools/waifu2x-ncnn-vulkan"
# models-cunet 里真正会被加载的模型（waifu2x exe 内的模板只有这三种：
#   %s/scale2.0x_model.*         -> noise=-1
#   %s/noise%d_model.*           -> scale=1（界面不提供，删）
#   %s/noise%d_scale2.0x_model.* -> noise=0..3）
_CUNET_KEEP = {
    "scale2.0x_model.bin", "scale2.0x_model.param",
    *(f"noise{n}_scale2.0x_model.{ext}" for n in range(4) for ext in ("bin", "param")),
}


def _keep_tool_file(rel_posix):
    """tools/ 下的文件是否打包（按相对路径判断，避免同名模型误判）。"""
    parts = rel_posix.split("/")
    if not parts[0].startswith("models-"):
        return True                       # exe / vcomp140.dll / README / LICENSE 全留
    if parts[0] != "models-cunet":
        return False                      # 两个 upconv 模型目录：从未被 -m 引用
    return parts[-1] in _CUNET_KEEP       # 只留 2.0x 模型


def _waifu2x_datas():
    """生成 waifu2x 的 datas；缺必需文件时直接报错，避免打出静默缺模型的包。"""
    if not _WAIFU2X_SRC.is_dir():
        raise SystemExit(f"[spec] 找不到 {_WAIFU2X_SRC}，请先下载 waifu2x-ncnn-vulkan 到该目录")
    entries, pruned = [], 0
    for path in sorted(_WAIFU2X_SRC.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(_WAIFU2X_SRC)
        rel_posix = rel.as_posix()
        if not FULL_PACKAGE and not _keep_tool_file(rel_posix):
            pruned += 1
            continue
        # datas 的第二个元素是「目标目录」，不是目标文件名：PyInstaller 对单文件
        # 条目只做 join(dest_dir, basename(src))（building/utils.py:517-522），
        # 所以必须把源文件的父目录拼进 dest，否则 models-cunet/ 会被压平到
        # tools/waifu2x-ncnn-vulkan/ 根下，exe 就找不到 -m 默认的模型目录了。
        parent = rel.parent.as_posix()
        dest_dir = _WAIFU2X_DEST if parent == "." else f"{_WAIFU2X_DEST}/{parent}"
        entries.append((str(path).replace("\\", "/"), dest_dir))

    required = [_WAIFU2X_SRC / "waifu2x-ncnn-vulkan.exe"]
    required += [_WAIFU2X_SRC / "models-cunet" / name for name in sorted(_CUNET_KEEP)]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise SystemExit(f"[spec] 缺少必需文件: {missing}")
    print(f"[spec] tools: 打包 {len(entries)} 个文件，裁剪 {pruned} 个")
    return entries


# ---------------------------------------------------------------- TOC 过滤
_KEEP_PLUGINS = {"platforms": {"qwindows.dll"}, "imageformats": {"qico.dll"}}
_DROP_PLUGIN_DIRS = {"tls", "generic", "networkinformation", "platforminputcontexts"}


def _drop_binary(dest):
    """返回 True 表示该二进制不打包（Qt 插件 / Pillow AVIF 扩展）。"""
    d = dest.replace("\\", "/")
    if d.startswith("PySide6/plugins/"):
        category, _, name = d[len("PySide6/plugins/"):].partition("/")
        if category in _DROP_PLUGIN_DIRS:
            return True
        keep = _KEEP_PLUGINS.get(category)
        return keep is not None and name not in keep
    return d.startswith("PIL/_avif.")      # Pillow AVIF 扩展

# ---------------------------------------------------------------- 应用资源
# 窗口图标：既要作为 exe 图标（EXE(icon=...)），也要打进包内供运行时读取
# （PyInstaller datas 的第二项是目标目录，因此最终落在 resources/app.ico）。
_APP_RESOURCES = ("resources/app.ico",)


def _app_datas():
    """应用自带资源的 datas；缺文件直接报错，避免打出没有图标的包。"""
    entries = []
    for rel in _APP_RESOURCES:
        src = Path(rel)
        if not src.is_file():
            raise SystemExit(f"[spec] 缺少资源文件: {rel}（窗口图标需要它）")
        entries.append((rel, str(src.parent).replace("\\", "/")))
    print(f"[spec] 应用资源: 打包 {len(entries)} 个文件")
    return entries


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=_waifu2x_datas() + _app_datas(),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# ---- 裁剪：必须在 Analysis 之后、COLLECT 之前 ----
if FULL_PACKAGE:
    print("[spec] FULL_PACKAGE=1：跳过 TOC 裁剪")
else:
    _nb, _nd = len(a.binaries), len(a.datas)
    a.binaries = [b for b in a.binaries if not _drop_binary(b[0])]
    a.datas = [d for d in a.datas
               if not d[0].replace("\\", "/").startswith("PySide6/translations/")]
    print(f"[spec] 裁剪 binaries {_nb} -> {len(a.binaries)}，"
          f"datas {_nd} -> {len(a.datas)}")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir：二进制分离到 _internal
    name='MangaUpscaler',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                  # --windowed：无控制台黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="resources/app.ico",       # exe 图标（窗口图标另由 resource_paths 在运行时读取）
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MangaUpscaler',
)
