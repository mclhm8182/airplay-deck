"""图形会话探测：判断当前是不是 SteamOS 游戏模式（gamescope）。

独立 App 作为非 Steam 游戏启动时运行在 gamescope 子会话里，这里用来决定
uxplay 要不要 -fs 全屏。两种判据：环境变量 + /proc 扫 gamescope 进程。
"""

import glob
import os


def is_gamemode() -> bool:
    desk = (
        os.environ.get("XDG_CURRENT_DESKTOP", "")
        + " "
        + os.environ.get("XDG_SESSION_DESKTOP", "")
    ).lower()
    if "gamescope" in desk:
        return True

    # 兜底：扫 /proc 的 cmdline 命中 gamescope（comm 只有 15 字符，cmdline 更可靠）
    for proc in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            data = open(proc, "rb").read().replace(b"\x00", b" ")
        except Exception:
            continue
        if b"gamescope" in data:
            return True
    return False
