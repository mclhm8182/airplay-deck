"""投屏期间的显示保活：对抗 gamescope / Game Mode 下与 logind 无关的熄屏。

SteamOS 游戏模式里，仅靠 ``systemd-inhibit --what=idle:sleep`` 往往不够：
gamescope 可能独立熄屏/黑屏，表现为「画面黑了、声音还在」。本模块在**宿主**
上对当前 DISPLAY 周期性调用 ``xset``（``s reset`` / ``dpms force on``），
并在启动时尽量关掉屏保与 DPMS。``xset`` 缺失时静默降级，不影响投屏主流程。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import Callable, Iterable, List, Optional, Sequence

LogCb = Callable[[str, str], None]

# 20–30s 区间取中；过密浪费、过疏可能赶不上熄屏策略。
DEFAULT_INTERVAL_SEC = 25.0
GAMEMODE_INTERVAL_SEC = 10.0


def xset_available() -> bool:
    return shutil.which("xset") is not None


def resolve_displays(
    preferred: Optional[str] = None,
    extra: Optional[Sequence[str]] = None,
) -> List[str]:
    """选出宿主侧要 poke 的 DISPLAY 列表（去重，保留顺序）。

    游戏模式常见 ``:1``，桌面常见 ``:0``；优先用环境 / 探针得到的值。
    """
    out: List[str] = []

    def _add(d: Optional[str]) -> None:
        d = (d or "").strip()
        if d and d not in out:
            out.append(d)

    _add(preferred)
    _add(os.environ.get("DISPLAY"))
    for d in extra or ():
        _add(d)
    # 兜底常见显示号，避免 env 异常时完全不 poke
    for d in (":1", ":0"):
        _add(d)
    return out


def _xset_env(display: str) -> dict:
    env = os.environ.copy()
    env["DISPLAY"] = display
    # 宿主 poke 不应被 Steam LD_PRELOAD 干扰
    env.pop("LD_PRELOAD", None)
    env.pop("LD_LIBRARY_PATH", None)
    return env


def _run_xset(display: str, args: Sequence[str], timeout: float = 5.0) -> int:
    """在指定 DISPLAY 上跑 xset；返回 exit code，找不到命令返回 -1。"""
    if not xset_available():
        return -1
    try:
        r = subprocess.run(
            ["xset", *args],
            env=_xset_env(display),
            capture_output=True,
            timeout=timeout,
        )
        return int(r.returncode)
    except FileNotFoundError:
        return -1
    except Exception:
        return 1


def apply_blank_disabled(displays: Iterable[str], log: Optional[LogCb] = None) -> bool:
    """启动时尽量关掉屏保 / DPMS / blank（每个显示号一次）。

    失败软处理：某个显示号或某条命令失败不影响其余。
    """
    log = log or (lambda *a, **k: None)
    if not xset_available():
        log("warn", "未找到 xset，跳过显示保活初始设置（屏保/DPMS）")
        return False
    ok_any = False
    for disp in displays:
        # 顺序：关屏保 → 关 DPMS → 禁止 blank → 立刻亮屏
        for args in (
            ("s", "off"),
            ("-dpms",),
            ("s", "noblank"),
            ("s", "reset"),
            ("dpms", "force", "on"),
        ):
            rc = _run_xset(disp, args)
            if rc == 0:
                ok_any = True
    if ok_any:
        log("info", "已对显示应用 xset 防熄屏初始设置（s off / -dpms / noblank）")
    else:
        log("warn", "xset 防熄屏初始设置未成功（显示号可能不可达，将继续周期性尝试）")
    return ok_any


def poke_displays(displays: Iterable[str], log: Optional[LogCb] = None) -> bool:
    """周期性轻量唤醒：``xset s reset`` + ``xset dpms force on``。"""
    log = log or (lambda *a, **k: None)
    if not xset_available():
        return False
    ok_any = False
    for disp in displays:
        # 周期性重新关掉屏保/DPMS：SteamOS 电源策略可能中途改回去
        for args in (
            ("s", "off"),
            ("-dpms",),
            ("s", "noblank"),
            ("s", "reset"),
            ("dpms", "force", "on"),
        ):
            if _run_xset(disp, args) == 0:
                ok_any = True
    return ok_any


def one_shot_wake(displays: Optional[Sequence[str]] = None,
                  log: Optional[LogCb] = None) -> None:
    """停止投屏后的一次性亮屏，帮助从黑屏恢复而无需重启 Steam。"""
    log = log or (lambda *a, **k: None)
    targets = list(displays) if displays else resolve_displays()
    if not xset_available():
        return
    if poke_displays(targets, log=None):
        log("info", "已执行一次性显示唤醒（xset dpms force on / s reset）")


class DisplayKeepalive:
    """后台线程：投屏期间周期性 poke 宿主 DISPLAY。"""

    def __init__(
        self,
        displays: Optional[Sequence[str]] = None,
        interval: float = DEFAULT_INTERVAL_SEC,
        log: Optional[LogCb] = None,
        gamemode: bool = False,
    ):
        self._displays = list(displays) if displays else resolve_displays()
        self._interval = max(5.0, float(interval))
        self._log = log or (lambda *a, **k: None)
        self._gamemode = bool(gamemode)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._xset_missing_logged = False

    @property
    def displays(self) -> List[str]:
        return list(self._displays)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if self._gamemode:
            self._log(
                "info",
                "游戏模式显示保活已启用：宿主 xset 周期性防熄屏"
                f"（DISPLAY={','.join(self._displays)}，间隔 {int(self._interval)}s；"
                "仅靠 systemd-inhibit 不足以阻止 gamescope 黑屏）",
            )
        else:
            self._log(
                "info",
                "显示保活已启用：宿主 xset 周期性防熄屏"
                f"（DISPLAY={','.join(self._displays)}，间隔 {int(self._interval)}s）",
            )
        if not xset_available():
            self._log("warn", "未找到 xset，显示保活线程将空转等待（仅依赖 systemd-inhibit）")
            self._xset_missing_logged = True
        else:
            apply_blank_disabled(self._displays, self._log)
        self._thread = threading.Thread(
            target=self._loop, name="airplay-display-keepalive", daemon=True,
        )
        self._thread.start()

    def stop(self, wake: bool = True) -> None:
        """停止线程；默认再做一次亮屏，便于黑屏后恢复。"""
        self._stop.set()
        t = self._thread
        self._thread = None
        if t and t.is_alive():
            t.join(timeout=3.0)
        if wake:
            one_shot_wake(self._displays, self._log)
        self._log("info", "显示保活已停止")

    def _loop(self) -> None:
        # 先立刻 poke 一次，再按间隔循环
        while not self._stop.is_set():
            if xset_available():
                poke_displays(self._displays)
            elif not self._xset_missing_logged:
                self._log("warn", "未找到 xset，跳过本轮显示保活 poke")
                self._xset_missing_logged = True
            # 可中断 sleep
            if self._stop.wait(self._interval):
                break
