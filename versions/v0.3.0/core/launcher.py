"""UxPlay 进程管理：启动 / 停止 / 崩溃重启 / 日志与状态回调。

设计要点（纯 Python，不依赖 PyQt）：
  * 后台线程里跑 uxplay，主线程跑 GUI；通过回调把日志和状态推给界面。
  * uxplay 客户端断开后默认保持运行继续广播，所以只在它「真正崩溃」时才重启；
    正常被停止（SIGTERM）时退出循环。
  * 退出码 0 但几乎立刻退出（<10s）通常是 mDNS 没就绪，重启 avahi 后重试；
    连续 8 次仍失败就停手并报错，避免刷屏。
  * 监听 SIGTERM/SIGINT：Steam「退出游戏」会发 SIGTERM 给 AppImage 进程，
    这里要能干净地杀掉 uxplay 子进程再退出。
"""

import os
import signal
import shlex
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import avahi, session, uxplay_args

StatusCb = Callable[[str], None]          # "idle" | "waiting" | "connected" | "error"
LogCb = Callable[[str, str], None]        # (level, message)

_LEVELS = ("info", "warn", "error", "cmd", "exit", "uxplay")


class Launcher:
    def __init__(
        self,
        settings: Dict[str, Any],
        log_cb: Optional[LogCb] = None,
        status_cb: Optional[StatusCb] = None,
    ):
        self.settings = settings
        self._log = log_cb or (lambda *a, **k: None)
        self._status = status_cb or (lambda *a, **k: None)
        self.proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._install_signal_handlers()

    # ---- 信号处理：让 Steam「退出游戏」能干净关掉 uxplay -------------------- #
    def _install_signal_handlers(self) -> None:
        try:
            signal.signal(signal.SIGTERM, lambda *a: self.stop())
            signal.signal(signal.SIGINT, lambda *a: self.stop())
        except Exception:
            pass

    # ---- 对外接口 ---------------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                pgid = os.getpgid(self.proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 内部 -------------------------------------------------------------- #
    def _uxplay_bin(self) -> str:
        p = (self.settings.get("uxplay_path") or "").strip()
        return p or "uxplay"

    def _kill_existing_uxplay(self) -> None:
        """启动 uxplay 之前，先尽力杀掉残留的 uxplay 进程（宿主机 + 容器内），
        避免端口/mDNS NameConflict 冲突。这是「频繁断线」的根因之一。"""
        # 宿主机
        try:
            subprocess.run(["pkill", "-x", "uxplay"], capture_output=True, timeout=5.0)
        except Exception:
            pass
        # distrobox 容器内
        if self.settings.get("use_distrobox"):
            container = (self.settings.get("distrobox_container") or "uxplay-env").strip()
            try:
                subprocess.run(
                    ["distrobox", "enter", container, "--", "pkill", "-x", "uxplay"],
                    capture_output=True, timeout=10.0,
                )
            except Exception:
                pass
        time.sleep(0.5)  # 给 mDNS 注册 / 端口释放一点时间

    def _run(self) -> None:
        # 1) avahi 就绪门闸
        self._log("info", "检查 avahi-daemon (mDNS) 就绪…")
        if avahi.ensure_avahi(20.0):
            self._log("info", "avahi 就绪，设备应出现在「隔空播放 / 屏幕镜像」列表")
        else:
            self._log("warn", "avahi 未就绪，iPhone/Mac 可能暂时搜不到本机；仍继续尝试")

        # 2) 组装参数
        is_gm = session.is_gamemode()
        try:
            args = uxplay_args.build_args(self.settings, is_gm)
        except Exception as e:
            self._log("error", f"参数组装失败: {e}")
            self._status("error")
            return

        binpath = self._uxplay_bin()
        if self.settings.get("use_distrobox"):
            container = (self.settings.get("distrobox_container") or "uxplay-env").strip()
            method = (self.settings.get("distrobox_method") or "enter").strip()
            if method == "podman":
                # 直接 podman start + exec：rootless 容器无需任何授权，
                # 可绕开游戏模式下 distrobox enter 弹不出 polkit 对话框导致 uxplay 起不来的问题。
                try:
                    subprocess.run(["podman", "start", container],
                                   capture_output=True, timeout=30.0)
                except Exception:
                    pass  # 已在运行会报错，忽略
                cmd = [
                    "podman", "exec",
                    "-e", f"DISPLAY={os.environ.get('DISPLAY', ':0')}",
                    "-e", f"XAUTHORITY={os.environ.get('XAUTHORITY', '')}",
                    container, binpath, *args,
                ]
                self._log("info", f"使用 podman 直接执行（容器={container}，免授权）")
            else:
                cmd = ["distrobox", "enter", container, "--", binpath, *args]
                self._log("info", f"使用 distrobox enter 运行 uxplay（容器={container}）")
        else:
            cmd = [binpath, *args]
        self._log("info", "运行模式: " + ("游戏模式(全屏)" if is_gm else "桌面模式(窗口)"))
        self._log("cmd", " ".join(shlex.quote(c) for c in cmd))

        # 3) 环境
        env = os.environ.copy()
        env["GST_GL_XINITTHREADS"] = "1"
        env["GST_DEBUG_NO_COLOR"] = "1"
        env.pop("LD_PRELOAD", None)
        env.pop("LD_LIBRARY_PATH", None)
        # 加大 UDP 接收缓冲，缓解网络抖动丢包（需 root，失败忽略）
        try:
            subprocess.run(
                ["sysctl", "-w", "net.core.rmem_max=33554432"],
                capture_output=True, timeout=5.0,
            )
        except Exception:
            pass

        # 3.5) 启动前清理可能残留的 uxplay 进程（避免端口/NameConflict 冲突）。
        # 这也是「频繁断线 + 反复弹密码」的根因：旧 uxplay 占着端口，新 uxplay
        # 一启动就因 kDNSServiceErr_NameConflict 立即退出，触发重试循环。
        self._kill_existing_uxplay()
        self._ghost_hint_shown = False

        # 4) 主循环：仅在 uxplay 真正崩溃时重启
        self._status("waiting")
        early_fails = 0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.proc = subprocess.Popen(
                    cmd, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=True, text=True, bufsize=1,
                )
            except FileNotFoundError:
                if self.settings.get("use_distrobox"):
                    self._log("error",
                              "找不到 distrobox 可执行文件。请先安装 distrobox，"
                              "或关闭「通过 distrobox 运行」改用本机 uxplay。")
                else:
                    self._log("error",
                              f"找不到 uxplay 可执行文件: {binpath}。"
                              f"请先安装（SteamOS: yay -S uxplay；Debian/Ubuntu: sudo apt install uxplay）")
                self._status("error")
                return

            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            ec = self.proc.wait()
            self.proc = None
            run = time.time() - t0
            self._log("exit", f"uxplay 退出码={ec} 存活={run:.0f}s")

            if self._stop.is_set():
                break

            # 几乎立刻退出：通常是端口/NameConflict 或 mDNS 未就绪。
            # 重要：此处不再 restart avahi（之前会反复触发 polkit 弹密码框），
            # avahi 已在步骤 1 检查过一次，只 sleep 等残留 mDNS 注册过期。
            if ec == 0 and run < 10:
                early_fails += 1
                if early_fails >= 8:
                    self._log("error", "uxplay 连续启动即退出，已停止重试；"
                              "请检查端口冲突或 Decky 插件的 uxplay 是否仍在跑。")
                    self._status("error")
                    return
                hint = ""
                if not self._ghost_hint_shown:
                    hint = ("（若日志含 'kDNSServiceErr_NameConflict'，"
                            "说明端口/mDNS 名被旧 uxplay 占用——本程序已自动清理残留；"
                            "如反复出现，请确认 Decky 插件的 uxplay 未在跑。）")
                    self._ghost_hint_shown = True
                self._log("warn", f"uxplay 几乎立刻退出，重试（第 {early_fails} 次）{hint}")
                time.sleep(1.0)
                continue

            # 其他异常退出：1 秒后重启
            early_fails = 0
            self._log("warn", "uxplay 异常退出，1 秒后重启…")
            time.sleep(1.0)

        self._status("idle")

    def _pump(self, proc: subprocess.Popen) -> None:
        # uxplay 各版本措辞不一致，这里用较宽的关键字集合判断「设备已连上 / 已断开」，
        # 避免出现「明明在投屏，状态还停在等待设备连接」。
        CONNECTED_HINTS = (
            "client connected", "connection open", "connection from",
            "accepted connection", "connection established",
            "raop_rtp starting", "streaming",
        )
        DISCONNECT_HINTS = (
            "connection closed", "client disconnected", "closed connection",
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                low = line.lower()
                if any(h in low for h in DISCONNECT_HINTS):
                    self._status("waiting")
                elif any(h in low for h in CONNECTED_HINTS):
                    self._status("connected")
                self._log("uxplay", line)
        except Exception:
            pass
