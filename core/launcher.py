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

from . import audio, avahi, session, uxplay_args, x11

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
        self._lock = threading.Lock()
        self._waiting_timer: Optional[threading.Timer] = None  # 断开→waiting 的防抖定时器
        self._last_data_ts = 0.0   # 最近一次「仍在传输」迹象的时间戳
        self._fatal = False        # 视频渲染器起不来等致命错误
        self._ghost_hint_shown = False
        self._inhibit = None  # systemd-inhibit 子进程；投屏时阻止系统空闲/熄屏
        self._install_signal_handlers()

    # ---- 信号处理：让 Steam「退出游戏」能干净关掉 uxplay -------------------- #
    def _install_signal_handlers(self) -> None:
        try:
            signal.signal(signal.SIGTERM, lambda *a: self.stop())
            signal.signal(signal.SIGINT, lambda *a: self.stop())
        except Exception:
            pass

    # ---- 保活防熄屏：投屏期间阻止系统空闲/熄屏 ---------------------------- #
    def _start_inhibit(self) -> None:
        """连接设备后调用：通过 systemd-inhibit 接管 idle+sleep，保持屏幕常亮。

        仅在 anti_sleep 开启且尚未持锁时生效。SteamOS 是 systemd 系统，
        锁住 logind 的 idle 动作即可阻止 KDE/PowerDevil 熄屏与锁屏。
        若主机没有 systemd-inhibit（如 macOS 调试、非 systemd 环境），仅告警不报错，
        不影响投屏主流程。
        """
        if self._inhibit is not None:
            return
        if not self.settings.get("anti_sleep", True):
            return
        try:
            self._inhibit = subprocess.Popen(
                ["systemd-inhibit", "--what=idle:sleep",
                 "--why=AirPlay 投屏中，保持屏幕常亮", "sleep", "infinity"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._log("info", "已启用保活防熄屏（systemd-inhibit 接管空闲/休眠）")
        except Exception as e:
            self._inhibit = None
            self._log("warn", f"保活防熄屏不可用（systemd-inhibit 缺失或被拒绝）：{e}")

    def _stop_inhibit(self) -> None:
        """释放保活锁。幂等，可重复调用。"""
        if self._inhibit is None:
            return
        p = self._inhibit
        self._inhibit = None
        try:
            p.terminate()
            try:
                p.wait(timeout=3.0)
            except Exception:
                p.kill()
        except Exception:
            pass
        self._log("info", "已解除保活防熄屏限制")

    # ---- 对外接口 ---------------------------------------------------------- #
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # 每次新会话清掉上次残留的 fatal，否则一次渲染失败会永久阻断后续启动。
        self._fatal = False
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
        # 关键：容器内的 uxplay 不会随宿主 podman exec 客户端一起死，
        # 必须单独杀掉，否则「断开接收」后 iOS 依然能搜到并投出声音。
        if self.settings.get("use_distrobox"):
            container = (self.settings.get("distrobox_container") or "uxplay-env").strip()
            self._kill_container_uxplay(container)
            self._log("info", f"已停止接收，并清理容器 {container} 内的 uxplay")
        self._stop_inhibit()  # 退出投屏：解除保活锁

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 内部 -------------------------------------------------------------- #
    def _uxplay_bin(self) -> str:
        p = (self.settings.get("uxplay_path") or "").strip()
        return p or "uxplay"

    def _kill_container_uxplay(self, container: str) -> None:
        """杀掉**容器内**的 uxplay。

        这是「点断开后 iOS 还能搜到、甚至还能投出声音」的根因：
        我们杀的只是 `podman exec` 客户端这个宿主进程，容器里的 uxplay 会变成孤儿
        继续运行、继续注册 mDNS，于是 iPhone 依然能发现并连上它。
        """
        method = (self.settings.get("distrobox_method") or "enter").strip()
        if method == "podman":
            cmds = [
                ["podman", "exec", container, "pkill", "-x", "uxplay"],
                ["podman", "exec", container, "pkill", "-f", "uxplay"],
            ]
        else:
            cmds = [["distrobox", "enter", container, "--", "pkill", "-x", "uxplay"]]
        for c in cmds:
            try:
                subprocess.run(c, capture_output=True, timeout=8.0)
            except Exception:
                pass

    def _kill_existing_uxplay(self) -> None:
        """启动 uxplay 之前，先尽力杀掉残留的 uxplay 进程（宿主机 + 容器内），
        避免端口/mDNS NameConflict 冲突。这是「频繁断线」的根因之一。"""
        # 宿主机
        try:
            subprocess.run(["pkill", "-x", "uxplay"], capture_output=True, timeout=5.0)
        except Exception:
            pass
        # 容器内（按实际使用的方式杀，否则容器里的孤儿进程会继续占着端口）
        if self.settings.get("use_distrobox"):
            container = (self.settings.get("distrobox_container") or "uxplay-env").strip()
            self._kill_container_uxplay(container)
        time.sleep(0.5)  # 给 mDNS 注册 / 端口释放一点时间

    def _run(self) -> None:
        # 每次 _run（含 stop 后再 start）都清 fatal，避免上次会话毒化本次。
        self._fatal = False

        # 1) avahi 就绪检查（纯只读，绝不触发 pkexec/sudo，避免每次启动弹密码框）
        if avahi.ensure_avahi():
            self._log("info", "avahi 已就绪，设备应出现在「隔空播放 / 屏幕镜像」列表")
        else:
            self._log("warn", "未能确认 avahi 在运行（也可能是检测方式在本机不适用）；"
                      "不影响 uxplay 自行注册 mDNS，继续启动")

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
            # 游戏模式 / 无图形授权环境：polkit 对话框弹不出来，distrobox enter 会卡住或失败，
            # 这里无条件降级到 podman 直接执行（rootless 容器不需要任何授权）。
            if is_gm and method != "podman":
                self._log("warn", "游戏模式无法弹出授权对话框，已自动改用 podman 直接执行")
                method = "podman"
            # ★ uxplay 用 libavahi-compat-libdnssd，它靠**系统 D-Bus** 连 avahi。
            #   容器里若连不上，uxplay 会报 No DNS-SD Server found 并反复退出（客户端永远搜不到设备）。
            #   rootless 容器还有个坑：宿主的 D-Bus socket 虽然挂进来了，但 dbus-send
            #   会 `Did not receive a reply`（userns/uid 映射与 EXTERNAL 鉴权打架），换路径治不好。
            #   所以这里交给 setup_container_mdns 三步走：
            #     宿主默认 D-Bus → 宿主 /run/host 下的候选路径 → 容器内自建 dbus+avahi（最稳）。
            try:
                subprocess.run(["podman", "start", container],
                               capture_output=True, timeout=30.0)
            except Exception:
                pass  # 已在运行会报错，忽略
            dbus_addr = avahi.setup_container_mdns(container, self._log)
            # ★ 音频：容器里默认没有任何可用音频输出（ALSA 无卡、pulse 连不上），
            #   默认 sink 打不开就静音投屏。这里探出宿主的 PipeWire/PulseAudio socket
            #   与 cookie，用 -e 透传给 uxplay。探不到就返回 {}，不影响投屏。
            audio_env = audio.setup_audio(container, self._log)
            if method == "podman":
                # 直接 podman start + exec：rootless 容器无需任何授权，
                # 可绕开游戏模式下 distrobox enter 弹不出 polkit 对话框导致 uxplay 起不来的问题。
                display = os.environ.get("DISPLAY", ":0")
                # ★ X11：不能只试继承来的 DISPLAY（游戏模式下可能是 :1，容器里根本连不上），
                #   要把 DISPLAY × XAUTHORITY 组合逐个实测，选真能连上的那个。
                display, ctr_xauth = x11.probe_container_display(container, display, self._log)
                cmd = x11.build_podman_cmd(
                    container, display, ctr_xauth, binpath, args,
                    os.environ.get("HOME", "/root"),
                    dbus_address=dbus_addr,
                    extra_env=audio_env,
                )
                self._log("info", f"使用 podman 直接执行（容器={container}，免授权）")
            else:
                cmd = ["distrobox", "enter", container, "--"]
                # 用 env 传变量（并清掉 Steam 的 LD_PRELOAD/LD_LIBRARY_PATH 污染），
                # 避免再套一层 sh -c 改变进程树
                cmd += x11.distrobox_env_prefix(dbus_addr, audio_env)
                cmd += [binpath, *args]
                self._log("info", f"使用 distrobox enter 运行 uxplay（容器={container}）")
        else:
            cmd = [binpath, *args]
        self._log("info", "运行模式: " + ("游戏模式(全屏)" if is_gm else "桌面模式(窗口)"))
        # xvimagesink 依赖 Xv 扩展；Xwayland / gamescope 下往往没有硬件加速实现，
        # 画面会发涩、偶尔停顿。这是「桌面模式看着不流畅」的一个常见来源。
        if (self.settings.get("video_sink") or "").strip() == "xvimagesink":
            self._log("warn",
                      "视频后端是 xvimagesink：在 Xwayland / gamescope 下 Xv 扩展通常没有硬件加速，"
                      "容易出现画面发涩、不流畅。建议在「视频」页改回 ximagesink（纯 X11，最稳）。")
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
        self._stutter_hint_shown = False

        # 4) 主循环：仅在 uxplay 真正崩溃时重启
        self._status("waiting")
        early_fails = 0
        while not self._stop.is_set():
            t0 = time.time()
            self._last_data_ts = 0.0
            if early_fails:
                self._status("waiting")  # 重启后重新回到「等待设备连接」
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

            # 视频渲染器起不来时重启多少次都没用，直接停手并报错（避免刷屏）
            if self._fatal:
                self._log("error", "因视频渲染器初始化失败，已停止重试。")
                self._status("error")
                return

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

        self._stop_inhibit()  # 兜底：循环退出时确保释放保活锁
        self._status("idle")

    # ---- 状态防抖：uxplay 常「关掉旧 socket 再开新 socket」，直接跟随会闪烁 ---- #
    def _schedule_waiting(self):
        """看到断开先等 3 秒，期间若又连上就取消，避免状态来回跳。"""
        with self._lock:
            if self._waiting_timer:
                self._waiting_timer.cancel()
            t = threading.Timer(3.0, self._do_waiting)
            t.daemon = True
            self._waiting_timer = t
            t.start()

    def _cancel_waiting(self):
        with self._lock:
            if self._waiting_timer:
                self._waiting_timer.cancel()
                self._waiting_timer = None

    def _mark_fatal(self) -> None:
        """视频渲染器初始化失败：明确报错 + 停止无意义的无限重启。"""
        if self._fatal:
            return
        self._fatal = True
        # 初始化期 X 鉴权失败时，作废 X11 探针缓存，下次 start 重新探测。
        try:
            from . import gamemode_x11
            gamemode_x11.invalidate_probe_cache("uxplay X/renderer init failure")
        except Exception:
            pass
        self._log("error", "uxplay 的视频渲染器初始化失败，无法输出画面。")
        self._log("error",
                  "两类常见原因：\n"
                  "  1) **容器里连不上 X**：宿主机 X cookie 在容器内不存在或不可读（日志里应有"
                  " X11 探测那几行）。可在「检查与日志」调整容器名/进入方式，或临时取消勾选"
                  "「通过容器运行 uxplay」改用本机 uxplay 验证。\n"
                  "  2) **ximagesink 渲染后端缺失/损坏**：若日志出现 `no element \"ximagesink\"` 或 "
                  "`Assertion 'renderer->sink' failed`。注意 ximagesink 由**独立包 gstreamer1.0-x** "
                  "提供（不在 plugins-base）。在构建环境执行：\n"
                  "     podman exec -u 0 uxplay-env bash -lc \"apt-get update && "
                  "apt-get install -y gstreamer1.0-x gstreamer1.0-plugins-base gstreamer1.0-plugins-good "
                  "gstreamer1.0-tools && rm -rf /home/*/.cache/gstreamer-1.0 /root/.cache/gstreamer-1.0 "
                  "&& gst-inspect-1.0 ximagesink\"")
        self._status("error")

    def _do_waiting(self):
        # AirPlay 一条会话里有多个 socket（视频/音频/时钟），中途关掉某一个
        # 并不代表整条投屏断了。只有当「断开」之后彻底没有任何数据迹象，
        # 才真的切回「等待设备连接」——否则继续判定为已连接。
        if time.time() - self._last_data_ts < 4.0:
            self._schedule_waiting()
            return
        with self._lock:
            self._waiting_timer = None
        self._status("waiting")

    def _pump(self, proc: subprocess.Popen) -> None:
        # uxplay 实际输出的关键句（取自用户导出的真实日志）：
        #   "connection request from Tom iPad mini 7 (iPad16,1) ..."
        #   "raop_rtp_mirror starting mirroring"
        #   "Begin streaming to GStreamer video pipeline"
        #   "raop_rtp starting audio"
        #   "Connection closed for socket NN"
        CONNECTED_HINTS = (
            "connection request from", "begin streaming",
            "raop_rtp_mirror starting mirroring", "raop_rtp starting",
            "client connected", "connection open", "connection from",
            "accepted connection", "connection established", "streaming",
        )
        DISCONNECT_HINTS = (
            "connection closed", "client disconnected", "closed connection",
        )
        # 仅把清晰的**初始化**失败视为永久 fatal（阻断重启）。
        # 会话中途的 GStreamer assertion / GST_IS_ELEMENT 等只告警，不粘住 _fatal，
        # 否则一次中途 glitch 会让后续 start() 永远起不来。
        # 这种情况下 uxplay **不会退出**，会继续监听并接受 iOS 连接，
        # 于是出现「搜得到、连得上、有声音、没画面 / 提示无法连接」。
        INIT_FATAL_HINTS = (
            "failed to initialize gstreamer video renderer",
            "could not initialise x output",
            "authorization required",
            'no element "ximagesink"',  # gstreamer1.0-x 未装时的报错（渲染器起不来）
        )
        MID_SESSION_GST_HINTS = (
            "gst_is_element",
            "assertion",
            "renderer->sink",        # UxPlay video_renderer_init 断言：ximagesink 后端不可用
            "video_renderer_init",
        )
        # 卡顿/发涩的信号：uxplay 报「重传失败」或「客户端反馈超时」。
        # 这两句同时出现，基本可以断定是 iPad/iPhone 与 Deck 之间的 Wi-Fi 丢包，
        # 而不是渲染管线的问题。给一次性提示，把排查方向直接指到网络上。
        STUTTER_HINTS = (
            "resend failed",
            "since last client feedback request",
        )
        last_line = None
        dup = 0
        session_useful = False  # 已真正连上/出流后，中途 GST 断言不再设 sticky fatal
        mid_gst_warned = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                low = line.lower()
                # 仍在传输的迹象：只要这些还在刷，就说明投屏没断
                if any(k in low for k in ("raop_rtp", "resend", "rtp", "video", "audio", "packet")):
                    self._last_data_ts = time.time()
                if any(h in low for h in CONNECTED_HINTS):
                    session_useful = True
                if any(h in low for h in INIT_FATAL_HINTS):
                    # 清晰 init 失败：即便中途也几乎总是致命（X 鉴权 / sink 缺失）
                    self._mark_fatal()
                elif (not session_useful) and any(h in low for h in MID_SESSION_GST_HINTS):
                    # 启动早期出现的 renderer 断言：仍视为 init 失败
                    self._mark_fatal()
                elif session_useful and any(h in low for h in MID_SESSION_GST_HINTS):
                    if not mid_gst_warned:
                        mid_gst_warned = True
                        self._log("warn",
                                  "会话中出现 GStreamer 断言/critical（不设为永久 fatal，可重试启动）："
                                  + line[:200])
                if any(h in low for h in DISCONNECT_HINTS):
                    self._schedule_waiting()
                elif any(h in low for h in CONNECTED_HINTS):
                    self._cancel_waiting()
                    self._status("connected")
                    self._start_inhibit()  # 投屏中：保持屏幕常亮

                # 只提示一次，别在 200 行刷屏里反复出现
                # （用 getattr 兜底：这个循环外面包着 except Exception，真抛异常会静默掐掉日志）
                if not getattr(self, "_stutter_hint_shown", False) and any(h in low for h in STUTTER_HINTS):
                    self._stutter_hint_shown = True
                    self._log(
                        "warn",
                        "检测到丢包/重传（画面会表现为卡顿、发涩、偶尔卡住一秒）——"
                        "这几乎都是网络侧的，不是渲染管线的问题。可依次试："
                        "① 手机/平板与 Deck 都连同一个 5GHz 频段、靠近路由器；"
                        "② 路由器关掉「节能/省电」(power save)；"
                        "③ 手机端关掉低电量模式；"
                        "④ 视频后端保持 ximagesink（xvimagesink 在 Xwayland 下常常没有硬件加速）；"
                        "⑤ 帧率降到 30。详见 README「画面卡顿 / 不够流畅」一节。")

                # 完全相同的行（如 raop_rtp resend failed 刷屏）合并计数，避免日志爆炸
                if line == last_line:
                    dup += 1
                    if dup <= 200:
                        continue
                    self._log("uxplay", f"（上一行已重复 {dup} 次，已省略）")
                    dup = 0
                    continue
                if dup > 1:
                    self._log("uxplay", f"（上一行共重复 {dup} 次）")
                last_line = line
                dup = 0
                self._log("uxplay", line)
        except Exception:
            pass