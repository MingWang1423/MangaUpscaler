"""每次任务独立的临时工作目录。

流水线不再固定使用 temp/extracted、temp/upscaled、temp/compressed 这类
相对当前工作目录的路径，而是为每次任务在系统临时目录（或指定的 base_dir）下
创建一个唯一目录，其下保留 extracted/upscaled/compressed 三个子目录。
成功时由调用方调用 cleanup() 清理；失败或取消时不调用 cleanup()，目录会保留
下来，路径可展示给用户用于排错。多任务/多进程并发时互不覆盖。
"""

import shutil
import tempfile
from pathlib import Path

EXTRACTED = "extracted"
UPSCALED = "upscaled"
COMPRESSED = "compressed"


class TaskWorkspace:
    """一次任务的工作目录，含 extracted / upscaled / compressed 三个子目录。"""

    def __init__(self, base_dir=None):
        # mkdtemp 在系统临时目录（或 base_dir）下创建唯一目录，不依赖 CWD
        self._root = Path(tempfile.mkdtemp(prefix="manga_upscaler_", dir=base_dir))
        self._cleaned = False
        for name in (EXTRACTED, UPSCALED, COMPRESSED):
            (self._root / name).mkdir()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def extracted(self) -> Path:
        return self._root / EXTRACTED

    @property
    def upscaled(self) -> Path:
        return self._root / UPSCALED

    @property
    def compressed(self) -> Path:
        return self._root / COMPRESSED

    def cleanup(self) -> None:
        """只清理本任务创建的工作目录，绝不影响任何其他文件。"""
        if not self._cleaned:
            shutil.rmtree(self._root, ignore_errors=True)
            self._cleaned = True
