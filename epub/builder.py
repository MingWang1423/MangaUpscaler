"""把放大后的图片按原有目录结构替换回 EPUB。

与 epub/reader.py 共用同一套 "arcname -> 本地相对路径" 映射规则
（build_local_names），因此提取、放大、打包三个环节的目录结构必然一致，
跨目录同名图片也不会串页。
"""

import zipfile
from pathlib import Path

from epub.reader import build_local_names, is_image_arcname

DEFAULT_OUTPUT_DIR = "output"
MIMETYPE = "mimetype"
EPUB_MIMETYPE = b"application/epub+zip"


def default_output_path(original_epub_path):
    """按 output/<原文件名去扩展名>_upscaled.epub 给出输出路径（只算路径，不建文件）。"""
    stem = Path(original_epub_path).stem
    return str(Path(DEFAULT_OUTPUT_DIR) / f"{stem}_upscaled.epub")


def build_epub(original_epub_path, upscaled_images_dir, output_epub_path=None,
               progress_callback=None, cancel_check=None):
    """用放大后的图片替换原 EPUB 内的图片，其余内容原样复制，重新打包成新 EPUB。

    图片按 EPUB 内部的相对路径在 upscaled_images_dir 里查找（如
    OEBPS/Images/cover.jpg -> <upscaled_images_dir>/OEBPS/Images/cover.jpg），
    找到就替换，找不到就保留原图，目录结构与条目顺序完全保持原样。
    output_epub_path 省略时按 default_output_path() 生成。
    progress_callback(done, total) 会在开始打包前以 (0, 图片总数) 调用一次（方便
    界面先设好进度条上限），之后每处理完一张图片条目再调用一次；分母是图片条目
    总数，替换成功、保留原图、跳过的条目都计入，所以进度必然走到 total。
    cancel_check 是可选的无参可调用对象，返回 True 表示调用方要求取消；每个
    文件条目写入前检查一次，被取消时抛 InterruptedError（此时新输出文件尚未
    写完，调用方应负责清理该残缺产物）。默认 None 时完全不检查，行为与不加
    该参数时一致。
    返回新 EPUB 的完整路径。
    """
    src = Path(original_epub_path)
    upscaled_path = Path(upscaled_images_dir)
    if not src.is_file():
        raise FileNotFoundError(f"找不到原始 EPUB: {src}")
    if not upscaled_path.is_dir():
        raise FileNotFoundError(f"找不到放大结果目录: {upscaled_path}")

    dst = Path(output_epub_path) if output_epub_path else Path(default_output_path(src))
    dst.parent.mkdir(parents=True, exist_ok=True)

    replaced, kept, skipped = 0, 0, 0
    missing = []

    with zipfile.ZipFile(src, "r") as zin:
        entries = zin.infolist()
        names = [entry.filename for entry in entries]
        # 与 extract_images 完全相同的映射规则：arcname -> 本地相对路径
        image_names = [n for n in names if is_image_arcname(n)]
        local_names = build_local_names(image_names)
        image_total = len(image_names)   # 进度分母 = 图片条目总数（与提取端同一判定）
        images_done = 0

        if progress_callback:
            progress_callback(0, image_total)

        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            # EPUB 规范：mimetype 必须是第一个条目，且不压缩
            mimetype = zipfile.ZipInfo(MIMETYPE)
            mimetype.compress_type = zipfile.ZIP_STORED
            zout.writestr(
                mimetype,
                zin.read(MIMETYPE) if MIMETYPE in names else EPUB_MIMETYPE,
            )
            if MIMETYPE not in names:
                print(f"警告: 原 EPUB 缺少 {MIMETYPE} 条目，已补写标准内容")

            for entry in entries:
                if cancel_check and cancel_check():
                    raise InterruptedError("用户已取消")
                name = entry.filename
                if name == MIMETYPE:
                    continue

                is_image = is_image_arcname(name)
                data = zin.read(name)
                if is_image:
                    rel = local_names.get(name)
                    if rel is None:
                        skipped += 1      # 危险条目：保持原样，但同样计入进度
                    else:
                        candidate = upscaled_path.joinpath(*rel.parts)
                        if candidate.is_file():
                            data = candidate.read_bytes()
                            replaced += 1
                        else:
                            kept += 1
                            missing.append(name)

                # 复用原 ZipInfo（保留目录标记、权限、时间戳与文件名编码标志位）；
                # EPUB 只允许 store/deflate，其余压缩方法纠正成 deflate
                if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    entry.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(entry, data)

                # 数据写盘之后再报进度，语义与 upscale_folder 一致
                if is_image:
                    images_done += 1
                    if progress_callback:
                        progress_callback(images_done, image_total)

    print(f"打包完成: 替换图片 {replaced} 张，保留原图 {kept} 张，"
          f"跳过不安全条目 {skipped} 张 -> {dst}")
    if missing:
        print("以下图片在放大结果目录中没有对应文件，已保留原图:")
        for name in missing[:10]:
            print(f"  {name}")
        if len(missing) > 10:
            print(f"  ... 共 {len(missing)} 张")

    return str(dst.resolve())
