# MangaUpscaler 项目规划

## 项目目标
一个漫画 EPUB 放大工具：读取 EPUB，提取漫画图片，用 waifu2x 放大，再重新打包成 EPUB。

## 技术栈
- Python 3.11+
- GUI：PySide6
- 放大：waifu2x-ncnn-vulkan.exe（subprocess 调用）
- 打包：后续用 PyInstaller

## 第一阶段：只建 5 个文件
MangaUpscaler/
├── main.py
├── requirements.txt
├── gui/
│   └── main_window.py
├── epub/
│   └── reader.py
└── upscaler/
    └── waifu2x.py

## 开发路线
第1步：5个文件骨架
第2步：做出 GUI（PySide6 窗口）
第3步：能够选择 EPUB
第4步：提取漫画图片
第5步：接入 waifu2x
第6步：重新生成 EPUB
第7步：加入进度条 / GPU 检测
第8步：PyInstaller 打包

## 当前阶段
- v1.0.0 已发布
- v1.1.0 开发中（画质档位功能）

## v1.1.0 改动清单
- 新增 Pillow 依赖
- 新增画质档位配置（original / 4k / 2k）
- 新增 upscaler/compressor.py 压缩模块
- 新增智能跳过超分（skip_if_larger_than）
- 新增 5 阶段流水线（提取 → 放大 → 压缩 → 打包 → 清理）

## 约束
- 不要创建 _internal、temp、output、logs、waifu2x.exe 等文件
- 不要一次性生成所有文件
- 除非导入报错，否则不要创建 __init__.py
- 每个阶段都要保证程序可运行
- 新增文件前先说明原因