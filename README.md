# MangaUpscaler

一个基于 [waifu2x-ncnn-vulkan](https://github.com/nihui/waifu2x-ncnn-vulkan) 的 EPUB 漫画批量放大工具。

选择一本漫画 EPUB，程序会自动提取图片、用 AI 放大、重新打包成新的 EPUB，全程一键完成。

An EPUB manga upscaler powered by waifu2x-ncnn-vulkan.

## ✨ 功能特性

- **一键处理**：选择 EPUB 后自动完成「提取 → 放大 → 压缩 → 打包 → 清理」全流程
- **GPU 加速**：基于 Vulkan，支持 NVIDIA / AMD / Intel 显卡，N 卡自动识别
- **随时取消**：处理过程中可随时终止，临时文件保留以便重试
- **参数可调**：支持 2x / 4x 放大，降噪等级 -1 到 3
- **画质档位可调**：支持原画质 / 4K / 2.5K 三档分辨率限制，智能跳过超分
- **输出路径可自定义**：支持手动输入或浏览选择输出目录
- **配置持久化**：设置自动保存，下次启动仍生效
- **绿色免安装**：打包为独立 exe，无需 Python 环境

## ⚙️ 配置说明

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `scale` | 放大倍数（2 / 4） | `2` |
| `noise` | 降噪等级（-1 ~ 3） | `3` |
| `quality` | 画质档位（original / 4k / 2k） | `4k` |
| `output_dir` | 输出目录路径 | `output` |

### 📊 画质档位说明

程序提供三档分辨率限制选项，平衡输出文件的清晰度与体积：

| 档位 | 边界框（宽×高） | 处理方式 | 适用场景 |
|---|---|---|---|
| **原画质（不压缩）** | 不限制 | 原样复制，不缩放不重编码 | 收藏、存档，体积最大 |
| **4K（推荐）** | 2160×3840 | 超出边界框时等比缩放 | 2K/4K 屏阅读，体积约减少 60% |
| **2.5K** | 1600×2560 | 超出边界框时等比缩放 | 1080p/2.5K 屏阅读，体积约减少 80% |

**智能跳过超分**：如果原图尺寸已经超出边界框（如 5000×3000 的图配 4K 边界），程序会跳过 waifu2x 超分，直接进行缩放。这既节省算力，也避免超分再缩小的二次损失。

**格式处理**：压缩阶段保持原格式不变（JPEG 输出 JPEG、PNG 保持 PNG），扩展名与字节始终一致。

## 📥 下载与使用

1. 前往 [Releases](https://github.com/MingWang1423/MangaUpscaler/releases/latest) 页面
2. 下载最新的 `MangaUpscaler_vX.X.X_win64.zip`
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

# 3. 安装运行依赖
pip install -r requirements.txt

# 4. 安装开发依赖（含 ruff）
pip install -r requirements-dev.txt

# 5. 下载 waifu2x-ncnn-vulkan（大文件未纳入仓库）
#    访问 https://github.com/nihui/waifu2x-ncnn-vulkan/releases
#    下载 Windows 版压缩包，解压到 tools/waifu2x-ncnn-vulkan/
#    确保该目录下包含 waifu2x-ncnn-vulkan.exe 和 models-cunet 等模型文件夹

# 6. 运行
python main.py
```

## ✅ 测试与代码检查

```bash
# 代码检查
ruff check .

# 运行全部单元测试
python -m unittest discover -s tests -p "test_*.py" -v
```
