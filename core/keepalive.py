"""投屏期间的显示保活 + 退出时恢复显示策略。

优先级：**不要破坏 SteamOS / gamescope**。
以前用 xset -dpms / s off 强行关电源管理，电源键熄屏或强杀 App 后
compositor 常卡在黑屏，只能重启 Steam。现改为：

* 投屏中：只做轻量 s reset + dpms force on（不永久关 DPMS）
* 退出/停止：restore_display_defaults 重新打开 DPMS/屏保并强制亮屏
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Callable, Iterable, List, Optional, Sequence

LogCb = Callable[[str, str], None]

DEFAULT_INTERVAL_SEC = 25.0
GAMEMODE_INTERVAL_SEC = 10.0


def xset_available() -> bool:
    return shutil.which("xset") is not None


def resolve_displays(
    preferred: Optional[str] = None,
    extra: Optional[Sequence[str]] = None,
) -> List[str]:
    out: List[str] = []

    def _add(d: Optional[str]) -> None:
        d = (d or "").strip()
        if d and d not in out:
            out.append(d)

    _add(preferred)
    _add(os.environ.get("DISPLAY"))
    for d in extra or ():
        _add(d)
    for d in (":1", ":0"):
        _add(d)
    return out


def _xset_env(display: str) -> dict:
    env = os.environ.copy()
    env["DISPLAY"] = display
    env.pop("LD_PRELOAD", None)
    env.pop("LD_LIBRARY_PATH", None)
    return env


def _run_xset(display: str, args: Sequence[str], timeout: float = 5.0) -> int:
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


def poke_displays(displays: Iterable[str], log: Optional[LogCb] = None) -> bool:
    """周期性轻量保活：只 reset / force on，绝不 -dpms / s off。"""
    if not xset_available():
        return False
    ok_any = False
    for disp in displays:
        for args in (("s", "reset"), ("dpms", "force", "on")):
            if _run_xset(disp, args) == 0:
                ok_any = True
    return ok_any


def restore_display_defaults(
    displays: Optional[Sequence[str]] = None,
    log: Optional[LogCb] = None,
) -> bool:
    """退出投屏后恢复宿主显示策略，减轻 gamescope 黑死。"""
    log = log or (lambda *a, **k: None)
    targets = list(displays) if displays else resolve_displays()
    if not xset_available():
        log("warn", "未找到 xset，无法恢复显示策略（若已黑屏请重启 Steam）")
        return False
    ok_any = False
    for disp in targets:
        for args in (
            ("+dpms",),
            ("s", "on"),
            ("s", "blank"),
            ("s", "expose"),
            ("dpms", "0", "0", "0"),
            ("s", "reset"),
            ("dpms", "force", "on"),
        ):
            if _run_xset(disp, args) == 0:
                ok_any = True
    if ok_any:
        log("info", "已恢复显示策略（+dpms / s on）并强制亮屏，降低 Steam 黑屏残留风险")
    else:
        log("warn", "恢复显示策略未成功；若 Steam 背景仍黑，请完全退出并重启 Steam")
    return ok_any


def one_shot_wake(displays: Optional[Sequence[str]] = None,
                  log: Optional[LogCb] = None) -> None:
    """停止投屏后的恢复入口：优先 restore，而不是继续 -dpms。"""
    restore_display_defaults(displays, log)


def apply_blank_disabled(displays: Iterable[str], log: Optional[LogCb] = None) -> bool:
    """兼容旧调用名：现仅做一次轻量亮屏，不再关闭 DPMS。"""
    log = log or (lambda *a, **k: None)
    ok = poke_displays(displays, log=None)
    if ok:
        log("info", "已做轻量显示保活初始 poke（s reset / dpms force on；不再关闭 DPMS）")
    return ok


class DisplayKeepalive:
    """后台线程：投屏期间周期性轻量 poke；停止时恢复显示策略。"""

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
        mode = "游戏模式" if self._gamemode else "桌面"
        self._log(
            "info",
            f"{mode}显示保活已启用：轻量 xset poke"
            f"（DISPLAY={','.join(self._displays)}，间隔 {int(self._interval)}s；"
            "不关闭 DPMS，避免电源键后 Steam 黑死）",
        )
        if not xset_available():
            self._log("warn", "未找到 xset，显示保活线程将空转")
            self._xset_missing_logged = True
        else:
            apply_blank_disabled(self._displays, self._log)
        self._thread = threading.Thread(
            target=self._loop, name="airplay-display-keepalive", daemon=True,
        )
        self._thread.start()

    def stop(self, wake: bool = True) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t and t.is_alive():
            t.join(timeout=3.0)
        if wake:
            restore_display_defaults(self._displays, self._log)
        self._log("info", "显示保活已停止")

    def _loop(self) -> None:
        while not self._stop.is_set():
            if xset_available():
                poke_displays(self._displays)
            elif not self._xset_missing_logged:
                self._log("warn", "未找到 xset，跳过本轮显示保活 poke")
                self._xset_missing_logged = True
            if self._stop.wait(self._interval):
                break
