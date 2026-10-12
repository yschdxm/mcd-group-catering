"""兼容层:让 scripts/ 下的薄封装可以直接 import mcdteam,无需手动设置 PYTHONPATH。

Agent 与人类使用者都不应该为了跑一条命令去配置环境变量,
所以每个 scripts/ 入口在导入第三方模块前先调用 :func:`bootstrap`。

同时把标准输出切到 UTF-8:Windows 控制台默认代码页不是 UTF-8,
中文与 emoji 会乱码或直接抛 UnicodeEncodeError,这在 Agent 读取输出时是致命的。
"""

from __future__ import annotations

import io
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")


def bootstrap() -> None:
    """把仓库的 src/ 注入 sys.path,并把标准输出设为 UTF-8。"""
    if _SRC not in sys.path:
        sys.path.insert(0, _SRC)
    _force_utf8()


def _force_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
                continue
            except (ValueError, OSError):  # pragma: no cover - 少数环境不支持
                pass
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:  # pragma: no cover - 兜底路径
            try:
                setattr(sys, stream_name,
                        io.TextIOWrapper(buffer, encoding="utf-8", errors="replace"))
            except (ValueError, OSError):
                pass


def repo_root() -> str:
    return _REPO_ROOT
