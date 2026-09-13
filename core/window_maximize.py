"""桌面窗口模式：自动最大化 uxplay 视频窗口（宿主 WM）。

UxPlay 的 X 窗口尺寸由协商到的 AirPlay 流分辨率决定；竖屏 Photos 视频
常为 ~450×800，在 Deck 横屏上会变成「小窗 + 大片黑边」。本模块在**宿主**
桌面会话上用 wmctrl / xdotool（可选 xprop）找到镜像窗口并最大化——不改用
``-fs``，尽量保持可还原的最大化窗口。

游戏模式仍由 ``uxplay_args`` 强制 ``-fs``，不走本模块。

若主机没有 wmctrl/xdotool：打中文警告；调用方可选择最后手段给桌面
window 模式追加 ``-fs``（见 ``tools_available`` / launcher 注释）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

LogCb = Callable[[str, str], None]

# 窗口标题 / WM_CLASS 关键词（大小写不敏感子串匹配）
_MATCH_NEEDLES = (
    "uxplay",
    "airplay",
    "gstreamer",
    "ximagesink",
    "xvimagesink",
)

# 启动后重试：约 12 次、间隔 ~1.2s → ~14s
DEFAULT_ATTEMPTS = 12
DEFAULT_INTERVAL_SEC = 1.2


def tools_available() -> bool:
    """宿主是否有可用的最大化工具（wmctrl 或 xdotool）。"""
    return bool(shutil.which("wmctrl") or shutil.which("xdotool"))


def should_auto_maximize(is_gamemode: bool, display_mode: Optional[str]) -> bool:
    """桌面 + window/auto（未走 -fs 的路径）才自动最大化。"""
    if is_gamemode:
        return False
    mode = (display_mode or "window").strip().lower()
    return mode in ("window", "auto")


def _host_env(display: Optional[str] = None) -> dict:
    env = os.environ.copy()
    disp = (display or env.get("DISPLAY") or ":0").strip() or ":0"
    env["DISPLAY"] = disp
    # 宿主 WM 操作不应被 Steam LD_PRELOAD 干扰
    env.pop("LD_PRELOAD", None)
    env.pop("LD_LIBRARY_PATH", None)
    # 保留宿主 XAUTHORITY（桌面会话通常已正确）
    return env


def _run(
    argv: Sequence[str],
    display: Optional[str] = None,
    timeout: float = 5.0,
) -> Tuple[int, str]:
    try:
        r = subprocess.run(
            list(argv),
            env=_host_env(display),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (r.stdout or "") + (r.stderr or "")
        return int(r.returncode), out
    except FileNotFoundError:
        return -1, ""
    except Exception as e:
        return 1, str(e)


def _norm_hex_id(token: str) -> Optional[str]:
    t = (token or "").strip()
    if not t:
        return None
    if t.startswith("0x") or t.startswith("0X"):
        try:
            return hex(int(t, 16))
        except ValueError:
            return None
    if t.isdigit():
        try:
            return hex(int(t))
        except ValueError:
            return None
    return None


def _line_matches(text: str, extra_needles: Iterable[str] = ()) -> bool:
    low = (text or "").lower()
    # 排除本 App 主窗口（标题含 AirPlay Deck），避免误最大化 GUI
    if "airplay deck" in low or "airplay-deck" in low or "airplaydeck" in low:
        return False
    for n in _MATCH_NEEDLES:
        if n in low:
            return True
    for n in extra_needles:
        n = (n or "").strip().lower()
        if not n or n in ("airplay", "airplay deck"):
            continue
        if n in low:
            return True
    return False


def _ids_from_wmctrl(display: Optional[str], extra: Iterable[str]) -> List[str]:
    if not shutil.which("wmctrl"):
        return []
    rc, out = _run(["wmctrl", "-lx"], display=display)
    if rc != 0 or not out:
        return []
    found: List[str] = []
    for line in out.splitlines():
        # 0x03c00007  0  uxplay.UxPlay  hostname  Title here
        parts = line.split(None, 4)
        if len(parts) < 3:
            continue
        blob = " ".join(parts[2:])  # class + host + title
        if _line_matches(blob, extra):
            wid = _norm_hex_id(parts[0])
            if wid and wid not in found:
                found.append(wid)
    return found


def _ids_from_xdotool(display: Optional[str], extra: Iterable[str]) -> List[str]:
    if not shutil.which("xdotool"):
        return []
    found: List[str] = []
    searches: List[List[str]] = [
        ["xdotool", "search", "--name", "uxplay"],
        ["xdotool", "search", "--name", "UxPlay"],
        # 不搜裸 "AirPlay"：会误中本 App「AirPlay Deck」窗口
        ["xdotool", "search", "--classname", "uxplay"],
        ["xdotool", "search", "--classname", "ximagesink"],
        ["xdotool", "search", "--classname", "xvimagesink"],
        ["xdotool", "search", "--class", "uxplay"],
        ["xdotool", "search", "--class", "GStreamer"],
    ]
    for needle in extra:
        n = (needle or "").strip()
        if n:
            searches.append(["xdotool", "search", "--name", n])
    for argv in searches:
        rc, out = _run(argv, display=display)
        if rc != 0 or not out.strip():
            continue
        for tok in out.split():
            wid = _norm_hex_id(tok) or (hex(int(tok)) if tok.isdigit() else None)
            if wid and wid not in found:
                # 再核对一下窗口名，减少误伤
                rc2, name = _run(
                    ["xdotool", "getwindowname", str(int(wid, 16))],
                    display=display,
                )
                rc3, cls = _run(
                    ["xdotool", "getwindowclassname", str(int(wid, 16))],
                    display=display,
                )
                blob = f"{name} {cls}"
                if _line_matches(blob, extra) or _line_matches(argv[-1], extra):
                    found.append(wid)
    return found


def _ids_from_xprop_wmctrl(display: Optional[str], extra: Iterable[str]) -> List[str]:
    """用 wmctrl 列出窗口，再用 xprop 读 WM_CLASS / WM_NAME 细筛。"""
    if not (shutil.which("wmctrl") and shutil.which("xprop")):
        return []
    rc, out = _run(["wmctrl", "-l"], display=display)
    if rc != 0 or not out:
        return []
    found: List[str] = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if not parts:
            continue
        wid = _norm_hex_id(parts[0])
        if not wid:
            continue
        title = parts[3] if len(parts) > 3 else ""
        rc2, xp = _run(["xprop", "-id", wid, "WM_CLASS", "WM_NAME"], display=display)
        blob = f"{title} {xp}"
        if _line_matches(blob, extra):
            if wid not in found:
                found.append(wid)
    return found


def find_mirror_window_ids(
    display: Optional[str] = None,
    extra_needles: Optional[Sequence[str]] = None,
) -> List[str]:
    """按优先级收集候选窗口 id（hex 字符串）。"""
    extra = list(extra_needles or ())
    ordered: List[str] = []
    for getter in (_ids_from_wmctrl, _ids_from_xdotool, _ids_from_xprop_wmctrl):
        try:
            for wid in getter(display, extra):
                if wid not in ordered:
                    ordered.append(wid)
        except Exception:
            continue
    return ordered


def _maximize_wmctrl(wid: str, display: Optional[str]) -> bool:
    if not shutil.which("wmctrl"):
        return False
    # 先激活再最大化，部分 WM 对未映射窗口忽略 hint
    _run(["wmctrl", "-i", "-a", wid], display=display)
    rc, _ = _run(
        ["wmctrl", "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz"],
        display=display,
    )
    return rc == 0


def _display_geometry(display: Optional[str]) -> Optional[Tuple[int, int]]:
    if shutil.which("xdotool"):
        rc, out = _run(["xdotool", "getdisplaygeometry"], display=display)
        if rc == 0:
            m = re.match(r"\s*(\d+)\s+(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
    if shutil.which("xdpyinfo"):
        rc, out = _run(["xdpyinfo"], display=display, timeout=8.0)
        if rc == 0:
            m = re.search(r"dimensions:\s*(\d+)x(\d+)", out)
            if m:
                return int(m.group(1)), int(m.group(2))
    return None


def _maximize_xdotool(wid: str, display: Optional[str]) -> bool:
    if not shutil.which("xdotool"):
        return False
    dec = str(int(wid, 16))
    _run(["xdotool", "windowactivate", "--sync", dec], display=display)
    # windowstate 在较新 xdotool 上可用
    rc, _ = _run(
        ["xdotool", "windowstate", "--add", "MAXIMIZED_VERT", dec],
        display=display,
    )
    rc2, _ = _run(
        ["xdotool", "windowstate", "--add", "MAXIMIZED_HORZ", dec],
        display=display,
    )
    if rc == 0 or rc2 == 0:
        return True
    geo = _display_geometry(display)
    if not geo:
        return False
    w, h = geo
    _run(["xdotool", "windowmove", dec, "0", "0"], display=display)
    rc3, _ = _run(["xdotool", "windowsize", dec, str(w), str(h)], display=display)
    return rc3 == 0


def maximize_window(wid: str, display: Optional[str] = None) -> bool:
    """对单个窗口尝试最大化（wmctrl 优先，其次 xdotool）。"""
    if _maximize_wmctrl(wid, display):
        return True
    if _maximize_xdotool(wid, display):
        return True
    return False


def try_maximize_once(
    display: Optional[str] = None,
    extra_needles: Optional[Sequence[str]] = None,
    log: Optional[LogCb] = None,
) -> bool:
    """找窗口并最大化；成功返回 True。"""
    log = log or (lambda *a, **k: None)
    ids = find_mirror_window_ids(display=display, extra_needles=extra_needles)
    if not ids:
        return False
    ok = False
    for wid in ids:
        if maximize_window(wid, display=display):
            ok = True
            log("info", f"已最大化投屏窗口（id={wid}，DISPLAY={display or os.environ.get('DISPLAY', ':0')}）")
            break
    if not ok:
        log("warn", f"找到投屏窗口但最大化失败（候选={','.join(ids)}）")
    return ok


class WindowMaximizer:
    """后台重试：uxplay 启动后窗口可能尚未映射，需轮询若干次。"""

    def __init__(
        self,
        display: Optional[str] = None,
        extra_needles: Optional[Sequence[str]] = None,
        attempts: int = DEFAULT_ATTEMPTS,
        interval: float = DEFAULT_INTERVAL_SEC,
        log: Optional[LogCb] = None,
    ):
        self._display = display
        self._extra = list(extra_needles or ())
        self._attempts = max(1, int(attempts))
        self._interval = max(0.3, float(interval))
        self._log = log or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._succeeded = False
        self._lock = threading.Lock()
        self._connected_retried = False

    @property
    def succeeded(self) -> bool:
        with self._lock:
            return self._succeeded

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if not tools_available():
            self._log(
                "warn",
                "未找到 wmctrl/xdotool，无法自动最大化投屏窗口；"
                "请安装其一，或在设置里改用「全屏」显示模式",
            )
            return
        self._log(
            "info",
            "桌面窗口模式：将自动最大化投屏窗口"
            f"（DISPLAY={self._display or os.environ.get('DISPLAY', ':0')}，"
            f"最多 {self._attempts} 次）",
        )
        self._thread = threading.Thread(
            target=self._loop, name="airplay-window-maximize", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t and t.is_alive():
            t.join(timeout=2.0)

    def on_connected(self) -> None:
        """CONNECTED 时再试一次（首次可能窗口尚未创建）。"""
        with self._lock:
            if self._succeeded or self._connected_retried:
                return
            self._connected_retried = True
        if not tools_available():
            return
        if self._stop.is_set():
            return

        def _once() -> None:
            time.sleep(0.8)  # 给 GStreamer 窗口一点映射时间
            if self._stop.is_set():
                return
            if try_maximize_once(self._display, self._extra, self._log):
                with self._lock:
                    self._succeeded = True
            else:
                self._log("warn", "CONNECTED 后仍未能最大化投屏窗口（可手动最大化或改用全屏）")

        threading.Thread(target=_once, name="airplay-window-maximize-connected", daemon=True).start()

    def _loop(self) -> None:
        for i in range(self._attempts):
            if self._stop.is_set():
                return
            with self._lock:
                if self._succeeded:
                    return
            if try_maximize_once(self._display, self._extra, self._log):
                with self._lock:
                    self._succeeded = True
                return
            # 前几次安静重试，最后一次再打失败日志
            if i == self._attempts - 1:
                self._log(
                    "warn",
                    "多次尝试后仍未找到/未能最大化投屏窗口；"
                    "竖屏流可能仍以小窗显示，可手动最大化或改用全屏模式",
                )
            if self._stop.wait(self._interval):
                return
