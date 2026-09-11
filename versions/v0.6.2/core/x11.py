"""容器内 X11 鉴权的处理：枚举候选 + 用真实 sink 实测，不靠猜。

背景
----
uxplay 跑在容器里时，宿主机的 XAUTHORITY 在容器内往往不存在/不可读/不匹配，Xlib 报：

    Authorization required, but no authorization protocol specified
    GStreamer error: Could not initialise X output
    *** ERROR: Failed to initialize GStreamer video renderer

**关键后果**：uxplay 在视频渲染器初始化失败后**不会退出**，继续监听 socket 并接受
iOS 连接，于是出现「搜得到、连得上、有声音，但没画面 / iOS 提示无法连接」。

踩过的坑（写在前面，避免再犯）
------------------------------
1. 只判断「cookie 文件在容器内可读」是不够的 —— 可读不等于能连上目标 DISPLAY
   （cookie 里可能是 :0 的条目，游戏模式却是 :1）。必须**真连一次**。
2. 容器里常常没有 xdpyinfo / xset，所以探针要有 GStreamer 兜底：
   直接用 `ximagesink` 起一条 1 帧管线 —— 这正是 uxplay 真正要用的 sink。
3. 「不传 XAUTHORITY」不等于「无鉴权」：容器镜像 / distrobox 可能自带
   XAUTHORITY 指向别处，Xlib 会去读 ~/.Xauthority 并拿着错的条目去连。
   所以需要显式提供「干净的」无 cookie 形态：
     * `XAUTHORITY=/dev/null`  —— 可读但零条目，强制不提供任何鉴权
     * `unset XAUTHORITY`      —— 彻底交给 Xlib 默认逻辑
"""

import glob
import os
import shlex
import subprocess
from typing import Callable, List, Optional, Tuple

LogCb = Callable[[str, str], None]

# 用 uxplay 真正依赖的 ximagesink 实测：这是最贴近真实的探针。
# 起一条 1 帧的测试管线，能连上 X 就正常退出，连不上会报 "Could not initialise X output"。
GST_PROBE = (
    "timeout 8 gst-launch-1.0 -q videotestsrc num-buffers=1 "
    "! video/x-raw,width=32,height=32 ! ximagesink >/dev/null 2>&1"
)
XDPY_PROBE = (
    'command -v xdpyinfo >/dev/null 2>&1 && xdpyinfo >/dev/null 2>&1 && exit 0; '
    'command -v xset >/dev/null 2>&1 && xset q >/dev/null 2>&1 && exit 0; '
    'exit 3'
)


def find_host_xauth() -> str:
    """找出宿主机当前会话的 X cookie 文件。"""
    env = (os.environ.get("XAUTHORITY") or "").strip()
    if env and os.path.exists(env):
        return env

    uid = os.getuid()
    rt = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    patterns = [
        os.path.join(rt, "xauth_*"),
        os.path.join(rt, ".mutter-Xwaylandauth.*"),
        os.path.join(rt, ".Xauthority"),
        f"/run/user/{uid}/xauth_*",
        f"/run/user/{uid}/.mutter-Xwaylandauth.*",
        f"/run/user/{uid}/.xauth*",
        os.path.expanduser("~/.Xauthority"),
        "/tmp/xauth_*",
    ]
    for p in patterns:
        for m in sorted(glob.glob(p)):
            try:
                if os.path.getsize(m) > 0:
                    return m
            except OSError:
                continue
    return ""


def _exec(container: str, env_args: List[str], cmd: str, timeout: float = 15.0):
    return subprocess.run(
        ["podman", "exec", *env_args, container, "sh", "-c", cmd],
        capture_output=True, timeout=timeout,
    )


def _container_home(container: str) -> str:
    try:
        r = _exec(container, [], "printenv HOME || echo /root", timeout=6.0)
        v = (r.stdout or b"").decode(errors="replace").strip().splitlines()
        return v[-1].strip() if v else "/root"
    except Exception:
        return "/root"


def _copy_in(container: str, src: str, dst: str, chmod: str) -> bool:
    try:
        cp = subprocess.run(
            ["podman", "cp", src, f"{container}:{dst}"],
            capture_output=True, timeout=25.0,
        )
    except Exception:
        return False
    if cp.returncode != 0:
        return False
    try:
        subprocess.run(
            ["podman", "exec", container, "sh", "-c",
             f"chmod {chmod} '{dst}' 2>/dev/null; true"],
            capture_output=True, timeout=10.0,
        )
    except Exception:
        pass
    return True


def _prefix_for(mode: str, path: Optional[str]) -> str:
    """生成在容器内 sh -c 里控制 XAUTHORITY 的前缀。"""
    if mode == "unset":
        return "unset XAUTHORITY; "
    if mode == "path" and path:
        return f"export XAUTHORITY={shlex.quote(path)}; "
    return ""


def probe_container_xauth(
    container: str,
    display: str,
    log: Optional[LogCb] = None,
) -> Tuple[str, Optional[str]]:
    """返回 (mode, path)，mode ∈ {"path", "unset"}。

    按「当前会话是否设置了 XAUTHORITY」排序候选，逐个**实测**，选第一个真能连上的。
    """
    log = log or (lambda *a, **k: None)
    env_xauth = (os.environ.get("XAUTHORITY") or "").strip()
    host = find_host_xauth()

    if host:
        src = "环境变量" if env_xauth else "自动探测（env 未设置，可能与当前 DISPLAY 不匹配）"
        log("info", f"宿主机 X cookie：{host}（{src}）")
    else:
        log("warn", f"宿主机未找到 X cookie 文件；DISPLAY={display}")

    # —— 无 cookie 的两种「干净」形态 —— #
    noauth_cands: List[Tuple[str, str, Optional[str]]] = [
        ("XAUTHORITY=/dev/null（强制不提供鉴权）", "path", "/dev/null"),
        ("unset XAUTHORITY（交给容器内默认）", "unset", None),
    ]

    # —— 带 cookie 的候选 —— #
    cookie_cands: List[Tuple[str, str, Optional[str]]] = []
    if host:
        cookie_cands.append((f"原路径 {host}", "path", host))
        tmp = "/tmp/.airplay_xauth"
        if _copy_in(container, host, tmp, "644"):
            cookie_cands.append((f"拷贝到容器内 {tmp}", "path", tmp))
        else:
            log("warn", f"podman cp cookie 到 {tmp} 失败")
        home = _container_home(container)
        dst = f"{home}/.Xauthority"
        if dst != tmp and _copy_in(container, host, dst, "600"):
            cookie_cands.append((f"拷贝到容器 HOME {dst}", "path", dst))

    # 宿主机显式设了 cookie（桌面模式）→ 优先带 cookie；
    # 没设（游戏模式 gamescope 常见）→ 优先无 cookie，避免拿错条目去连。
    if env_xauth and cookie_cands:
        candidates = cookie_cands + noauth_cands
    else:
        candidates = noauth_cands + cookie_cands

    for name, mode, path in candidates:
        shell = _prefix_for(mode, path) + GST_PROBE
        try:
            r = _exec(container, ["-e", f"DISPLAY={display}"], shell)
            rc = r.returncode
            if rc == 0:
                log("info", f"X11 实测可用（ximagesink）→ {name}")
                return mode, path
        except Exception as e:
            log("warn", f"X 探测异常（{name}）：{e}")
            continue

        # gst-launch 不存在（rc=127）时退回 xdpyinfo/xset
        if rc == 127:
            try:
                r2 = _exec(container, ["-e", f"DISPLAY={display}"],
                           _prefix_for(mode, path) + XDPY_PROBE)
                if r2.returncode == 0:
                    log("info", f"X11 实测可用（xdpyinfo）→ {name}")
                    return mode, path
                if r2.returncode == 3:
                    log("warn", "容器内既无 gst-launch 也无 xdpyinfo/xset，无法实测；"
                                f"按候选顺序采用：{name}")
                    return mode, path
            except Exception:
                pass
        log("warn", f"X 不可用（{name}），rc={rc}")

    log("error",
        "所有 X11 鉴权方案都不可用，uxplay 很可能只有声音没画面。建议："
        "在容器内执行 `sudo apt install -y x11-utils` 后重试，"
        "或取消勾选「通过容器运行 uxplay」改用本机 uxplay。")
    return "unset", None


def build_podman_cmd(
    container: str,
    display: str,
    xauth: Tuple[str, Optional[str]],
    binpath: str,
    args: List[str],
    home: str,
    dbus_address: str = "",
) -> List[str]:
    """按探测结果拼出 podman exec 命令。

    XAUTHORITY 一律在容器内的 sh 里设置（或 unset），
    这样不会被容器镜像 / distrobox 自带的环境变量覆盖。

    `dbus_address`（可选）：容器默认的系统 D-Bus 连不上宿主 avahi 时，
    由 `avahi.container_dbus_address()` 探出的可用地址，用 `-e` 传进去，
    否则 uxplay 会报 `No DNS-SD Server found` 反复退出。
    """
    mode, path = xauth
    inner = " ".join(shlex.quote(x) for x in [binpath, *args])
    shell = f"{_prefix_for(mode, path)}exec {inner}"
    cmd = [
        "podman", "exec",
        "-e", f"DISPLAY={display}",
        "-e", f"HOME={home}",
    ]
    if dbus_address:
        cmd += ["-e", f"DBUS_SYSTEM_BUS_ADDRESS={dbus_address}"]
    cmd += [container, "sh", "-c", shell]
    return cmd
