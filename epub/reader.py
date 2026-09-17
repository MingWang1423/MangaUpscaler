import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DEFAULT_OUTPUT_DIR = "temp/extracted"

_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


def normalize_arcname(arcname):
    """把 ZIP 条目名归一化成 POSIX 风格（ZIP 规范用 '/'，'\' 视为畸形容错）。"""
    return arcname.replace("\\", "/")


def is_image_arcname(arcname):
    """按扩展名判断 ZIP 条目名是不是图片。"""
    return PurePosixPath(normalize_arcname(arcname)).suffix.lower() in IMAGE_EXTS


def arcname_to_parts(arcname):
    """ZIP 条目名 -> 安全的相对路径片段列表；非法或危险条目返回 None。

    'OEBPS/Images/cover.jpg' -> ('OEBPS', 'Images', 'cover.jpg')
    拒绝绝对路径、'..'、盘符，避免写到工作目录之外（zip-slip 防护）。
    """
    if not arcname:
        return None
    normalized = normalize_arcname(arcname)
    if normalized.startswith("/"):
        return None
    parts = [p for p in PurePosixPath(normalized).parts
             if p not in ("", ".", "/")]
    if not parts:
        return None
    for part in parts:
        if part == ".." or _DRIVE_RE.match(part):
            return None
    return parts


def build_local_names(arcnames):
    """arcname -> 本地相对路径（PurePosixPath），提取端与打包端共用的唯一映射规则。

    默认原样保留 EPUB 内部的相对目录结构（1:1 映射，跨目录同名图片各归各位）。
    只有在大小写不敏感的文件系统上会互相覆盖时（如 Cover.jpg 与 cover.jpg
    在 NTFS 上是同一个文件），才给后来的那个加确定性后缀（Cover__2.jpg），
    保证映射是单射。纯函数：同一份 arcnames（同一顺序）必得同一份映射，
    所以不需要额外的映射记录文件。
    """
    mapping = {}
    used = {}   # 已占用的本地路径（casefold 后，按大小写不敏感文件系统模拟）-> arcname
    for arcname in arcnames:
        parts = arcname_to_parts(arcname)
        if parts is None:
            continue
        candidate = PurePosixPath(*parts)
        key = candidate.as_posix().casefold()
        if key in used and used[key] != arcname:
            parent = PurePosixPath(*parts[:-1])
            index = 2
            while True:
                renamed = parent / f"{candidate.stem}__{index}{candidate.suffix}"
                key = renamed.as_posix().casefold()
                if key not in used:
                    break
                index += 1
            candidate = renamed
        used[key] = arcname
        mapping[arcname] = candidate
    return mapping


def inspect_epub(file_path):
    """打开 EPUB，打印内部目录结构并统计图片数量（不真正提取）。"""
    path = Path(file_path)

    with zipfile.ZipFile(path, "r") as zf:
        entries = zf.namelist()
        images = [name for name in entries if is_image_arcname(name)]

    directories = {}
    basenames = {}
    for name in images:
        rel = PurePosixPath(normalize_arcname(name))
        directories[str(rel.parent)] = directories.get(str(rel.parent), 0) + 1
        basenames[rel.name] = basenames.get(rel.name, 0) + 1
    duplicated = {name: count for name, count in basenames.items() if count > 1}

    print(f"EPUB 文件: {path.name}")
    print(f"条目总数: {len(entries)}，图片数量: {len(images)}")
    print(f"图片分布: {len(directories)} 个目录")
    for directory, count in sorted(directories.items()):
        print(f"  {directory}: {count} 张")
    if duplicated:
        print(f"跨目录同名图片: {len(duplicated)} 组（按相对路径分别存放，不会串页）")
        for name, count in sorted(duplicated.items()):
            print(f"  {name} x{count}")
    print("目录结构:")
    for name in entries:
        print(f"  {name}")

    return {
        "file": path.name,
        "total_entries": len(entries),
        "image_count": len(images),
        "images": images,
        "directories": directories,
        "duplicated_basenames": duplicated,
    }


def extract_images(file_path, output_dir=DEFAULT_OUTPUT_DIR, clean=True):
    """提取 EPUB 内所有图片到 output_dir，完整保留 EPUB 内部的相对目录结构。

    例如 EPUB 内的 OEBPS/Images/cover.jpg 会提取到
    <output_dir>/OEBPS/Images/cover.jpg，因此不同章节目录下的同名图片
    （ch1/page01.jpg 与 ch2/page01.jpg）不会互相覆盖。
    clean=True 时先清空 output_dir（它是可随时重建的工作目录），避免上一次
    提取的残留文件混进后续放大与打包流程。
    返回提取出的图片完整路径列表。
    """
    path = Path(file_path)
    output_path = Path(output_dir)
    if clean and output_path.exists():
        shutil.rmtree(output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(path, "r") as zf:
        image_names = [name for name in zf.namelist() if is_image_arcname(name)]
        # 与 build_epub 共用同一份映射规则，保证两端路径完全一致
        local_names = build_local_names(image_names)

        extracted = []
        skipped = 0
        for name in image_names:
            rel = local_names.get(name)
            if rel is None:
                print(f"跳过不安全条目: {name}")
                skipped += 1
                continue
            target = output_path.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))
            extracted.append(str(target))
            print(f"已提取: {name} -> {target}")

    print(f"提取完成: {len(extracted)} 张图片，跳过 {skipped} 个不安全条目")
    return extracted

