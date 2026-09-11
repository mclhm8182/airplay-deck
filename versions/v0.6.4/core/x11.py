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
import time
from typing import Callable, Dict, List, Optional, Tuple

LogCb = Callable[[str, str], None]

# 每个 DISPLAY 最多实测几个 (XAUTHORITY) 组合 —— 覆盖住真实可用的组合，又不至于太慢。
_MAX_PER_DISPLAY = 5
# 整个 X11 探测的总预算（秒）。最坏情况：继承的 :1 全部失败 → 再试 :0 才成功。
_X_PROBE_BUDGET = 100.0

# 每次 podman exec 都显式清掉这两个变量。
#
# 为什么必须清：游戏模式下 App 是由 Steam 拉起来的，环境里带着
#   LD_PRELOAD=/home/deck/.local/share/Steam/ubuntu12_32/gameoverlayrenderer.so
#   LD_LIBRARY_PATH=/home/deck/.local/share/Steam/ubuntu12_64:...
# podman exec 会把客户端环境带进容器，于是容器里的 avahi-daemon / dbus-daemon /
# uxplay 都会去加载宿主的 Steam runtime 库（32 位覆盖层还会刷 ld.so 报错），
# 直接把 mDNS 和音频搞挂。传 `-e VAR=` 是覆盖而不是"不传"，最稳。
SANITIZE_ENV = ["-e", "LD_PRELOAD=", "-e", "LD_LIBRARY_PATH="]

# 用 uxplay 真正依赖的 ximagesink 实测：这是最贴近真实的探针。
# 起一条 1 帧的测试管线，能连上 X 就正常退出，连不上会报 "Could not initialise X output"。
GST_PROBE = (
    "timeout 4 gst-launch-1.0 -q videotestsrc num-buffers=1 "
    "! video/x-raw,width=32,height=32 ! ximagesink >/dev/null 2>&1"
)
XDPY_PROBE = (
    'command -v xdpyinfo >/dev/null 2>&1 && xdpyinfo >/dev/null 2>&1 && exit 0; '
    'command -v xset >/dev/null 2>&1 && xset q >/dev/null 2>&1 && exit 0; '
    'exit 3'
)


def find_host_xauth_candidates() -> List[str]:
    """列出宿主机上所有可能的 X cookie 文件（按优先级）。

    游戏模式（gamescope）下 XAUTHORITY 往往是空的、`~/.Xauthority` 也不一定对得上，
    真正的 cookie 常常躺在 /run/user/<uid>/ 下的临时文件里，所以这里一次全列出来，
    交给容器那边逐个**实测**，而不是猜一个。
    """
    uid = os.getuid()
    rt = (os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}").rstrip("/")
    patterns = [
        # 显式指定的排最前
        (os.environ.get("XAUTHORITY") or "").strip(),
        # 桌面模式 Xorg 的 cookie
        os.path.join(rt, "xauth_*"),
        os.path.join(rt, ".Xauthority"),
        os.path.join(rt, ".mutter-Xwaylandauth.*"),
        # 游戏模式 gamescope 的 cookie（名字随版本变，全兜一遍）
        os.path.join(rt, "gamescope*"),
        os.path.join(rt, ".gamescope*"),
        "/tmp/gamescope*",
        "/tmp/xauth_*",
        f"/run/user/{uid}/xauth_*",
        f"/run/user/{uid}/.xauth*",
        f"/run/user/{uid}/gamescope*",
        os.path.expanduser("~/.Xauthority"),
        "/tmp/.Xauthority",
    ]
    out: List[str] = []
    for p in patterns:
        if not p:
            continue
        matches = [p] if os.path.isfile(p) else sorted(glob.glob(p))
        for m in matches:
            if m in out:
                continue
            try:
                if os.path.getsize(m) > 0:
                    out.append(m)
            except OSError:
                continue
    return out


def find_host_xauth() -> str:
    """找出宿主机当前会话的 X cookie 文件（第一个候选）。"""
    c = find_host_xauth_candidates()
    return c[0] if c else ""


def _exec(container: str, env_args: List[str], cmd: str, timeout: float = 15.0):
    return subprocess.run(
        ["podman", "exec", *SANITIZE_ENV, *env_args, container, "sh", "-c", cmd],
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
            ["podman", "exec", *SANITIZE_ENV, container, "sh", "-c",
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


def container_x_report(container: str, log: LogCb) -> None:
    """把「容器里到底有没有 X、能不能连」的原始证据打出来，一次导出即可定位。"""
    try:
        r = _exec(container, [], "ls -l /tmp/.X11-unix 2>&1 | head -8; "
                                 "echo '--- /run/user/<uid>:'; ls -l /run/user/*/ 2>&1 | head -12; "
                                 "echo '--- 容器 sees /run/host:'; "
                                 "[ -d /run/host ] && echo yes || echo no",
                  timeout=10.0)
        out = (r.stdout or b"").decode(errors="replace").strip()
        if out:
            log("info", "容器内 X 实况：" + out.replace("\n", " | "))
    except Exception:
        pass


def _probe_xauth_for_display(container: str, display: str, log: LogCb) -> Optional[Tuple[str, str, Optional[str]]]:
    """针对**一个** DISPLAY，逐个 XAUTHORITY 形态实测。返回 (name, mode, path) 或 None。"""
    env_xauth = (os.environ.get("XAUTHORITY") or "").strip()
    hosts = find_host_xauth_candidates()

    # —— 无 cookie 的两种「干净」形态 —— #
    noauth_cands: List[Tuple[str, str, Optional[str]]] = [
        ("XAUTHORITY=/dev/null（强制不提供鉴权）", "path", "/dev/null"),
        ("unset XAUTHORITY（交给容器内默认）", "unset", None),
    ]

    # —— 带 cookie 的候选：对宿主机找到的每个 cookie 都试「原路径」和「拷进容器」—— #
    cookie_cands: List[Tuple[str, str, Optional[str]]] = []
    for i, host in enumerate(hosts[:4]):
        cookie_cands.append((f"原路径 {host}", "path", host))
        tmp = f"/tmp/.airplay_xauth{i}"
        if _copy_in(container, host, tmp, "644"):
            cookie_cands.append((f"拷贝到容器内 {tmp}（源 {host}）", "path", tmp))
    if hosts:
        home = _container_home(container)
        dst = f"{home}/.Xauthority"
        if _copy_in(container, hosts[0], dst, "600"):
            cookie_cands.append((f"拷贝到容器 HOME {dst}", "path", dst))

    # 宿主机显式设了 cookie（桌面模式）→ 优先带 cookie；
    # 没设（游戏模式 gamescope 常见）→ 优先无 cookie，避免拿错条目去连。
    if env_xauth and cookie_cands:
        candidates = cookie_cands + noauth_cands
    else:
        candidates = noauth_cands + cookie_cands

    # 每个显示号最多试几个组合：候选太多会把「开始接收」拖到一两分钟，
    # 而真正能用的组合一定在靠前位置（cookie 顺序按可信度排过）。
    for name, mode, path in candidates[:_MAX_PER_DISPLAY]:
        shell = _prefix_for(mode, path) + GST_PROBE
        rc = -1
        try:
            r = _exec(container, ["-e", f"DISPLAY={display}"], shell)
            rc = r.returncode
            if rc == 0:
                log("info", f"X11 实测可用（ximagesink，DISPLAY={display}）→ {name}")
                return name, mode, path
        except Exception as e:
            log("warn", f"X 探测异常（DISPLAY={display} {name}）：{e}")
            continue

        # gst-launch 不存在（rc=127）时退回 xdpyinfo/xset
        if rc == 127:
            try:
                r2 = _exec(container, ["-e", f"DISPLAY={display}"],
                           _prefix_for(mode, path) + XDPY_PROBE)
                if r2.returncode == 0:
                    log("info", f"X11 实测可用（xdpyinfo，DISPLAY={display}）→ {name}")
                    return name, mode, path
                if r2.returncode == 3:
                    log("warn", "容器内既无 gst-launch 也无 xdpyinfo/xset，无法实测；"
                                f"按候选顺序采用：DISPLAY={display} {name}")
                    return name, mode, path
            except Exception:
                pass
        log("warn", f"X 不可用（DISPLAY={display} {name}），rc={rc}")
    return None


def probe_container_display(
    container: str,
    display: str,
    log: Optional[LogCb] = None,
) -> Tuple[str, Tuple[str, Optional[str]]]:
    """返回真正可用的 (DISPLAY, (mode, path))。

    ⚠️ 只试继承来的 DISPLAY 是不够的 —— 游戏模式下 App 由 Steam/gamescope 拉起，
    环境里的 `DISPLAY` 可能指向一个 uxplay 连不上的显示号（实测：`:1` 全部鉴权方案
    都报 `Authorization required`），而 gamescope 真正的 X 显示号是 `:0`。
    所以这里把候选显示号也一起枚举，逐个实测。
    """
    log = log or (lambda *a, **k: None)
    container_x_report(container, log)

    hosts = find_host_xauth_candidates()
    if hosts:
        log("info", "宿主机 X cookie 候选：" + " , ".join(hosts[:6]))
    else:
        log("warn", f"宿主机未找到任何 X cookie 文件；DISPLAY={display}")

    cands: List[str] = []
    for d in [display, os.environ.get("DISPLAY", ""), ":0", ":1"]:
        d = (d or "").strip()
        if d and d not in cands:
            cands.append(d)

    deadline = time.time() + _X_PROBE_BUDGET
    for disp in cands:
        if time.time() > deadline:
            log("warn", "X11 探测超时，停止继续尝试其它显示号")
            break
        got = _probe_xauth_for_display(container, disp, log)
        if got:
            _name, mode, path = got
            if disp != display:
                log("info", f"继承的 DISPLAY={display} 在容器里不可用，已改用 {disp}")
            return disp, (mode, path)

    log("error",
        "所有 X11 显示号 / 鉴权方案都不可用，uxplay 会报 "
        "`Failed to initialize GStreamer video renderer`（现象：搜得到、连得上、没有画面）。"
        "请把「导出日志」发我——里面已带容器内 X socket 实况与全部候选的实测结果。")
    return display, ("unset", None)


def probe_container_xauth(
    container: str,
    display: str,
    log: Optional[LogCb] = None,
) -> Tuple[str, Optional[str]]:
    """兼容旧调用点：只返回 (mode, path)。"""
    _disp, pair = probe_container_display(container, display, log)
    return pair


def build_podman_cmd(
    container: str,
    display: str,
    xauth: Tuple[str, Optional[str]],
    binpath: str,
    args: List[str],
    home: str,
    dbus_address: str = "",
    extra_env: Optional[Dict[str, str]] = None,
) -> List[str]:
    """按探测结果拼出 podman exec 命令。

    说明
    ----
    * XAUTHORITY 一律在容器内的 sh 里设置（或 unset），这样不会被容器镜像 /
      distrobox 自带的环境变量覆盖。
    * `dbus_address`：uxplay 靠系统 D-Bus 找 avahi，容器默认地址不通时由
      `avahi.setup_container_mdns()` 给出的可用地址（宿主的候选路径，或容器内
      自建 bus 的 `unix:path=/run/dbus/system_bus_socket`）。
    * `extra_env`：音频等额外变量（`PULSE_SERVER` / `PULSE_COOKIE`），
      由 `audio.setup_audio()` 探出来。
    """
    mode, path = xauth
    inner = " ".join(shlex.quote(x) for x in [binpath, *args])
    shell = f"{_prefix_for(mode, path)}exec {inner}"
    cmd = [
        "podman", "exec",
        *SANITIZE_ENV,
        "-e", f"DISPLAY={display}",
        "-e", f"HOME={home}",
    ]
    if dbus_address:
        cmd += ["-e", f"DBUS_SYSTEM_BUS_ADDRESS={dbus_address}"]
    for k, v in (extra_env or {}).items():
        if v:
            cmd += ["-e", f"{k}={v}"]
    cmd += [container, "sh", "-c", shell]
    return cmd


def distrobox_env_prefix(dbus_address: str = "",
                         extra_env: Optional[Dict[str, str]] = None) -> List[str]:
    """`distrobox enter <c> -- ` 后面要跟的 `env …` 前缀（清污染 + 注入变量）。"""
    out = ["env", "-u", "LD_PRELOAD", "-u", "LD_LIBRARY_PATH"]
    if dbus_address:
        out.append(f"DBUS_SYSTEM_BUS_ADDRESS={dbus_address}")
    for k, v in (extra_env or {}).items():
        if v:
            out.append(f"{k}={v}")
    return out
