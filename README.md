# MangaUpscaler

一个基于 [waifu2x-ncnn-vulkan](https://github.com/nihui/waifu2x-ncnn-vulkan) 的 EPUB 漫画批量放大工具。

选择一本漫画 EPUB，程序会自动提取图片、用 AI 放大、重新打包成新的 EPUB，全程一键完成。

An EPUB manga upscaler powered by waifu2x-ncnn-vulkan.

## ✨ 功能特性

- **一键处理**：选择 EPUB 后自动完成「提取 → 放大 → 打包 → 清理」全流程
- **GPU 加速**：基于 Vulkan，支持 NVIDIA / AMD / Intel 显卡，N 卡自动识别
- **随时取消**：处理过程中可随时终止，临时文件保留以便重试
- **参数可调**：支持 2x / 4x 放大，降噪等级 -1 到 3
- **输出路径可自定义**：支持手动输入或浏览选择输出目录
- **配置持久化**：设置自动保存，下次启动仍生效
- **绿色免安装**：打包为独立 exe，无需 Python 环境

## 📥 下载与使用

1. 前往 [Releases](https://github.com/MingWang1423/MangaUpscaler/releases/latest) 页面
2. 下载最新的 `MangaUpscaler_vX.X.X_win64.rar`
3. 解压到任意目录（不要放在中文路径下）
4. 双击 `MangaUpscaler.exe` 运行
5. 点击「选择 EPUB」，选一本漫画即可自动处理
6. 处理完成后，去输出目录（默认在 `文档/MangaUpscaler/output`）找到 `xxx_upscaled.epub`

### 系统要求

- Windows 10 / 11 64 位
- 显卡支持 Vulkan API（2014 年后的显卡基本都支持）
- 若目标电脑缺少 VC++ 运行库，需安装 [VC++ 2015-2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe)

## 🛠️ 开发环境搭建

如果你想从源码运行或参与开发：

```bash
# 1. 克隆仓库
git clone https://github.com/MingWang1423/MangaUpscaler.git
cd MangaUpscaler

# 2. 创建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. 安装依赖
pip install -r requirements.txt

# 4. 下载 waifu2x-ncnn-vulkan（大文件未纳入仓库）
#    访问 https://github.com/nihui/waifu2x-ncnn-vulkan/releases
#    下载 Windows 版压缩包，解压到 tools/waifu2x-ncnn-vulkan/
#    确保该目录下包含 waifu2x-ncnn-vulkan.exe 和 models-cunet 等模型文件夹

# 5. 运行
python main.py
```
