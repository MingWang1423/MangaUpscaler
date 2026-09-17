# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（--onedir + --windowed）。

datas 把整个 tools/waifu2x-ncnn-vulkan 目录打入 _internal/tools/...，
保证 waifu2x.exe 与 models-* 的相对结构不变，运行时可正确调用模型。
"""

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('tools/waifu2x-ncnn-vulkan', 'tools/waifu2x-ncnn-vulkan')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

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
