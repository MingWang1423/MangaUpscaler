"""目标分辨率与方向自适应的纯函数（无副作用，可独立测试）。

边界框不再固定为竖屏：竖版页面用竖版边界（如 4K 的 2160×3840），横版跨页用
横版边界（3840×2160），正方形图片按竖版处理（两种方向下计算结果一致）。
超分前的跳过判断（needed_scale）与超分后的缩放（resize_size）都通过
target_bounds() 得到同一套方向判断，保证口径一致。
"""


def orientation_of(width, height):
    """返回 'landscape'（宽 > 高）或 'portrait'（高 >= 宽，正方形归竖版）。"""
    return "landscape" if width > height else "portrait"


def target_bounds(width, height, short_limit, long_limit):
    """按方向返回该图片的 (max_w, max_h) 边界框。

    short_limit / long_limit 为目标短边 / 长边上限（4K 为 2160/3840，2.5K 为
    1600/2560）。横图宽为长边，竖图（含正方形）高为长边。
    """
    if orientation_of(width, height) == "landscape":
        return (long_limit, short_limit)
    return (short_limit, long_limit)


def _fit_size(width, height, max_w, max_h):
    """等比缩放进 (max_w, max_h)：只缩小不放大，保持宽高比，不裁剪。"""
    ratio = min(1.0, max_w / width, max_h / height)
    return max(1, round(width * ratio)), max(1, round(height * ratio))


def resize_size(width, height, short_limit, long_limit):
    """按方向自适应边界计算最终尺寸（只缩小，保持宽高比，不裁剪）。"""
    max_w, max_h = target_bounds(width, height, short_limit, long_limit)
    return _fit_size(width, height, max_w, max_h)


def needed_scale(width, height, requested_scale, short_limit, long_limit):
    """返回应执行的 waifu2x 倍率（1/2/4）。

    1 表示原图已达目标分辨率，无需超分（交由压缩阶段按需缩小）；否则在用户
    允许的倍率（2×/4×）中取能覆盖目标的最小值，避免 4× 后再大幅缩回；若连
    允许的最大倍率都达不到目标，返回 requested_scale 尽力而为（绝不拉伸变形）。
    """
    max_w, max_h = target_bounds(width, height, short_limit, long_limit)
    # 覆盖目标边界框所需的最小倍率（任一维先触顶即够）
    c = min(max_w / width, max_h / height)
    if c <= 1.0:
        return 1
    for s in (2, 4):
        if s > requested_scale:
            break
        if s >= c:
            return s
    return requested_scale
