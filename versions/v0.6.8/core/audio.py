"""容器内音频输出（PulseAudio / PipeWire）的探测与接入。

背景
----
uxplay 跑在容器里，默认音频 sink 是 autoaudiosink（第一优先 pulsesink）。但容器里
通常**没有任何可用的音频输出**，于是静音投屏。2026-09-11 实机日志（桌面模式）长这样：

    Home directory not accessible: Permission denied
    ALSA lib confmisc.c:855:(parse_card) cannot find card '0'
    ALSA lib pcm.c:2664:(snd_pcm_open_noupdate) Unknown PCM default
    AL lib: (EE) ALCplaybackAlsa_open: Could not open playback device 'default'
    Cannot connect to server socket err = No such file or directory
    jack server is not running or cannot be started

画面正常、就是没声音。

做法
----
SteamOS 的音频是 PipeWire（对外提供 PulseAudio 兼容 socket）。容器与宿主共享挂载，
所以只要找到宿主那个 socket、再带上正确的 cookie，容器里的 pulsesink 就能出声。
这里做三件事：

1. 在容器内列出所有候选 socket / cookie（含 distrobox 挂在 `/run/host` 下的路径）；
2. 用 `gst-launch-1.0 audiotestsrc volume=0 ! pulsesink`（**静音**，不会真响）
   逐个**实测**，不靠猜；
3. 返回 `{"PULSE_SERVER": ..., "PULSE_COOKIE": ...}`，由 launcher 用 `-e` 透传给 uxplay。

探测不到就返回空字典、只记一条 warn —— 绝不能因为音频探测失败而挡住投屏。
"""

import shlex
import shutil
import subprocess
from typing import Callable, Dict, List, Optional

try:  # 正常以包导入
    from .x11 import SANITIZE_ENV
except ImportError:  # 直接按文件路径加载本模块（离线自测）时的兜底
    SANITIZE_ENV = ["-e", "LD_PRELOAD=", "-e", "LD_LIBRARY_PATH="]

LogCb = Callable[[str, str], None]

# 只读探测：列出容器内可见的 pulse socket / cookie，以及 pulsesink 是否可用。
_CTR_AUDIO_PROBE = (
    "for _u in 1000 1001 0; do\n"
    "  for _p in /run/user/$_u/pulse/native /var/run/user/$_u/pulse/native "
    "/run/host/run/user/$_u/pulse/native /run/host/var/run/user/$_u/pulse/native "
    "/tmp/pulse-$_u/native /run/host/tmp/pulse-$_u/native; do\n"
    "    [ -S \"$_p\" ] && echo \"SOCK=$_p\"\n"
    "  done\n"
    "done\n"
    "for _c in /home/deck/.config/pulse/cookie /run/host/home/deck/.config/pulse/cookie "
    "/root/.config/pulse/cookie /home/*/.config/pulse/cookie "
    "/run/host/root/.config/pulse/cookie; do\n"
    "  [ -r \"$_c\" ] && echo \"COOKIE=$_c\"\n"
    "done\n"
    "echo \"PULSESINK=$( (gst-inspect-1.0 pulsesink >/dev/null 2>&1 && echo 1) || echo 0)\"\n"
    "echo \"DEVSND=$([ -d /dev/snd ] && echo 1 || echo 0)\"\n"
)

# pulsesink（libgstpulseaudio.so）在老容器里可能缺失。注意它不是 plugins-good 的一部分。
_INSTALL_PULSESINK = (
    "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update >/dev/null 2>&1 || true\n"
    "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y "
    "gstreamer1.0-pulseaudio >/tmp/airplay-audio-apt.log 2>&1 || tail -5 /tmp/airplay-audio-apt.log\n"
    "rm -rf /home/*/.cache/gstreamer-1.0 /root/.cache/gstreamer-1.0\n"
    "exit 0\n"
)

_MAX_TRIES = 6          # 最多实测几个组合，避免启动被拖太久
_TEST_TIMEOUT = 9.0     # 单次实测上限（gst-launch 内部还有 timeout）


def _podman_exec(container: str, cmd: str, timeout: float = 30.0, root: bool = False):
    argv = ["podman", "exec"]
    if root:
        argv += ["-u", "0"]
    argv += [*SANITIZE_ENV, container, "sh", "-c", cmd]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _pulse_test_cmd(sock: str, cookie: Optional[str]) -> str:
    """静音实测：能打开 pulsesink 就说明这套 socket/cookie 是通的。"""
    prefix = f"PULSE_SERVER=unix:{shlex.quote(sock)}"
    if cookie:
        prefix += f" PULSE_COOKIE={shlex.quote(cookie)}"
    return (
        prefix + " timeout 8 gst-launch-1.0 -q audiotestsrc num-buffers=2 volume=0 "
        "! audioconvert ! audio/x-raw,rate=48000,channels=2 ! pulsesink "
        ">/dev/null 2>&1"
    )


def _pulse_works(container: str, sock: str, cookie: Optional[str]) -> bool:
    try:
        r = _podman_exec(container, _pulse_test_cmd(sock, cookie), timeout=_TEST_TIMEOUT)
        return r.returncode == 0
    except Exception:
        return False


def _pulsesink_ok(container: str) -> bool:
    try:
        r = _podman_exec(container, "gst-inspect-1.0 pulsesink >/dev/null 2>&1",
                         timeout=25.0)
        return r.returncode == 0
    except Exception:
        return False


def setup_audio(container: str, log: Optional[LogCb] = None) -> Dict[str, str]:
    """探测并返回要透传给 uxplay 的音频环境变量；探测不到返回 `{}`（绝不阻塞投屏）。"""
    log = log or (lambda *a, **k: None)
    if shutil.which("podman") is None:
        return {}

    try:
        r = _podman_exec(container, _CTR_AUDIO_PROBE, timeout=30.0)
    except Exception as e:
        log("warn", f"容器音频探测异常：{e}")
        return {}
    out = (r.stdout or "") + (r.stderr or "")

    socks: List[str] = []
    cookies: List[str] = []
    has_sink = False
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("SOCK="):
            p = line[5:]
            if p not in socks:
                socks.append(p)
        elif line.startswith("COOKIE="):
            p = line[7:]
            if p not in cookies:
                cookies.append(p)
        elif line.startswith("PULSESINK="):
            has_sink = line.split("=", 1)[1].strip() == "1"

    if not socks:
        log("warn", "容器内找不到宿主机的 PulseAudio/PipeWire socket —— 投屏会没有声音。"
                    "（容器与宿主共享挂载，正常应能在 /run/host/run/user/1000/pulse/native 下看到）")
        return {}

    if not has_sink:
        log("info", "容器内缺 pulsesink（gstreamer1.0-pulseaudio），先补装（需要联网）…")
        try:
            _podman_exec(container, _INSTALL_PULSESINK, timeout=300.0, root=True)
        except Exception as e:
            log("warn", f"补装 pulsesink 失败：{e}")
        has_sink = _pulsesink_ok(container)

    if not has_sink:
        log("warn", "容器内没有 pulsesink，无法接入宿主机音频 —— 投屏会没有声音。"
                    "「导出日志」里已带容器音频探测结果。")
        return {}

    log("info", "容器内可见的音频 socket：" + " , ".join(socks[:4]))

    tries = 0
    for sock in socks:
        # 同一个 socket 先配 cookie，再试不带 cookie（有些宿主配置是匿名的）
        for cookie in (cookies[:2] + [None]):
            if tries >= _MAX_TRIES:
                break
            tries += 1
            if _pulse_works(container, sock, cookie):
                env = {"PULSE_SERVER": f"unix:{sock}"}
                if cookie:
                    env["PULSE_COOKIE"] = cookie
                log("info", "音频已接入宿主机 PulseAudio（"
                            + f"{sock}" + (f"，cookie={cookie}" if cookie else "，无 cookie")
                            + "）")
                return env
        if tries >= _MAX_TRIES:
            break

    log("warn", "找到音频 socket 但实测都打不开（pulsesink 连不上）—— 投屏会没有声音。"
                "排查：容器内执行 `apt-get install -y pulseaudio-utils && pactl info`。")
    return {}
