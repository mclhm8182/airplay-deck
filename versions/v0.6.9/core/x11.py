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
import re
import shlex
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

LogCb = Callable[[str, str], None]

# ⚠️ v0.6.6 起**取消**「每个 DISPLAY 最多实测 N 个组合」的硬上限。
# 旧上限（v0.6.4=5 / v0.6.5=8）在游戏模式下是致命的：9/11 的实测日志证明
# /run/user/1000 下有 **6 个** gamescope.* cookie，每个显示号只跑到第 4 个就
# 用光名额，`gamescope.E5RXM9P` / `gamescope.NyKaC6g` **一次都没被试过**
# （实机日志里 :1 / :0 各试 8 次，全是 rc=255）。现在按可信度排好序后**全部实测**，
# 由总预算 _X_PROBE_BUDGET 兜底。
# 整个 X11 探测的总预算（秒）。候选多了（多显示号 × 多 cookie），给足时间。
_X_PROBE_BUDGET = 150.0

# 判断「这个文件到底是不是真的 X cookie」——靠**解析二进制结构**，不靠文件名。
# 游戏模式下 /run/user/1000 有一堆 gamescope* 文件，其中 `gamescope-environment`
# / `gamescope-stats` 之类是文本/统计文件，旧版按名字猜会把它们当 cookie 去试。
_XAUTH_COOKIE_NAME = "MIT-MAGIC-COOKIE-1"
_XAUTH_COOKIE_LEN = 16

# 探测失败时，从输出里挑一行「像原因」的话回显（下次一眼定位，不用再猜）。
_REASON_HINTS = (
    "authorization required", "cannot open display", "unable to open display",
    "not authorized", "no element", "could not", "failed", "error", "denied",
)

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
#
# ⚠️ 只丢 stdout，**保留 stderr**：失败原因（`Authorization required…` /
# `Cannot open display` / `no element "ximagesink"`）就在 stderr 里。
# 旧版写的是 `>/dev/null 2>&1`，把所有线索都吞了，日志里只剩一句 `rc=255`，
# 9/11 两轮实机排查都卡在这里。
GST_PROBE = (
    "timeout 5 gst-launch-1.0 -q videotestsrc num-buffers=1 "
    "! video/x-raw,width=32,height=32 ! ximagesink >/dev/null"
)
XDPY_PROBE = (
    'command -v xdpyinfo >/dev/null 2>&1 && xdpyinfo >/dev/null 2>&1 && exit 0; '
    'command -v xset >/dev/null 2>&1 && xset q >/dev/null 2>&1 && exit 0; '
    'exit 3'
)


def _short_reason(r) -> str:
    """从探测输出里挑一行最像「失败原因」的话（截断到 160 字符）。"""
    txt = ((getattr(r, "stderr", b"") or b"") + b"\n"
           + (getattr(r, "stdout", b"") or b"")).decode("utf-8", "replace")
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    if not lines:
        return ""
    for l in lines:
        low = l.lower()
        if any(h in low for h in _REASON_HINTS):
            return l[:160]
    return lines[0][:160]


def _parse_xauthority(path: str) -> Optional[List[Tuple[str, str]]]:
    """解析 Xauthority 二进制。**不是合法 cookie 文件时返回 None**。

    为什么要自己解析：游戏模式 `/run/user/<uid>` 下有一堆 `gamescope*` 文件，
    `gamescope-environment` / `gamescope-stats` 之类是文本文件，按文件名根本分不出来，
    只能看内容结构。Xauthority 的格式（大端，重复若干条目）：

        family(2) | addr_len(2) | addr | num_len(2) | num
                  | name_len(2) | name | data_len(2) | data
    """
    try:
        with open(path, "rb") as f:
            buf = f.read()
    except OSError:
        return None
    n = len(buf)
    if n < 8:
        return None
    entries: List[Tuple[str, str]] = []
    i = 0
    while i < n:
        if i + 2 > n:
            return None
        i += 2                                   # family
        fields: List[bytes] = []
        for _ in range(3):                       # address / number / name
            if i + 2 > n:
                return None
            ln = int.from_bytes(buf[i:i + 2], "big")
            i += 2
            if i + ln > n:
                return None
            fields.append(buf[i:i + ln])
            i += ln
        if i + 2 > n:
            return None
        dlen = int.from_bytes(buf[i:i + 2], "big")
        i += 2
        if i + dlen > n:
            return None
        data = buf[i:i + dlen]
        i += dlen
        num = fields[1].decode("ascii", "replace").strip()
        name = fields[2].decode("ascii", "replace").strip()
        if not name:
            return None
        # MIT-MAGIC-COOKIE-1 的 data 固定 16 字节；长度不对说明结构解析跑偏了
        if name == _XAUTH_COOKIE_NAME and dlen != _XAUTH_COOKIE_LEN:
            return None
        entries.append((":" + num if num else "", name))
    return entries or None


def xauth_file_info(path: str) -> Tuple[bool, List[str], float]:
    """返回 (像不像真 cookie, 它授权的显示号列表, mtime)。"""
    entries = _parse_xauthority(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    if entries is None:
        return False, [], mtime
    return True, sorted({d for d, _ in entries}), mtime


def _display_number_of(display: str) -> Optional[str]:
    """:1.0 → '1'；取不到返回 None。"""
    d = (display or "").strip()
    if ":" not in d:
        return None
    tail = d.split(":", 1)[1].split(".", 1)[0].strip()
    return tail or None


def find_host_xauth_candidates() -> List[str]:
    """列出宿主机上所有可能的 X cookie 文件（**按可信度排序**）。

    游戏模式（gamescope）下 XAUTHORITY 往往是空的、`~/.Xauthority` 也不一定对得上，
    真正的 cookie 常常躺在 /run/user/<uid>/ 下的临时文件里，所以这里一次全列出来，
    交给容器那边逐个**实测**，而不是猜一个。

    v0.6.6 的排序规则（此前只会「按文件名把 environment/stats 沉底」，不够）：
      1) 真正能解析成 Xauthority 结构的文件（_parse_xauthority 判定）
      2) mtime 最新的排最前 —— 当前会话 Xwayland 的 -auth 文件总是最新创建的那个，
         旧会话残留的 cookie 文件会一直留着，按 mtime 倒序天然把它们排到后面
      3) 解析不出结构的可疑文件沉到最后（有真 cookie 时直接丢掉）
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
    raw: List[str] = []
    for p in patterns:
        if not p:
            continue
        matches = [p] if os.path.isfile(p) else sorted(glob.glob(p))
        for m in matches:
            if m in raw:
                continue
            try:
                if os.path.getsize(m) > 0:
                    raw.append(m)
            except OSError:
                continue

    real: List[Tuple[str, float]] = []
    junk: List[str] = []
    for p in raw:
        ok, _disps, mtime = xauth_file_info(p)
        if ok:
            real.append((p, mtime))
        else:
            junk.append(p)
    if real:
        real.sort(key=lambda t: -t[1])
        return [p for p, _ in real]
    return junk


def _env_xauth_map() -> Dict[str, str]:
    """第二个权威来源：宿主机 `/proc/<pid>/environ` 里的 DISPLAY + XAUTHORITY。

    Xwayland 的 -auth 文件有时读不到（游戏模式下 App 可能跑在命名空间里，
    /proc 看不到宿主的 Xwayland），但只要**任何**一个同 uid 进程的环境里有
    XAUTHORITY，就能拿到当前会话真正在用的那个 cookie 文件。
    """
    out: Dict[str, str] = {}
    if not os.path.isdir(_PROC):
        return out
    for d in glob.glob(os.path.join(_PROC, "[0-9]*")):
        try:
            with open(os.path.join(d, "environ"), "rb") as f:
                raw = f.read(65536)
        except OSError:
            continue
        if not raw:
            continue
        disp = xauth = ""
        for item in raw.split(b"\x00"):
            k, sep, v = item.decode("utf-8", "replace").partition("=")
            if not sep:
                continue
            if k == "DISPLAY":
                disp = v.strip()
            elif k == "XAUTHORITY":
                xauth = v.strip()
        num = _display_number_of(disp)
        if not (num and xauth) or (":" + num) in out:
            continue
        try:
            if os.path.isfile(xauth) and os.path.getsize(xauth) > 0:
                out[":" + num] = xauth
        except OSError:
            continue
    return out


def find_host_xauth() -> str:
    """找出宿主机当前会话的 X cookie 文件（第一个候选）。"""
    c = find_host_xauth_candidates()
    return c[0] if c else ""


# X 服务器的可执行名（宿主机 /proc 里可能出现的形式）。
_X_SERVER_EXES = ("Xwayland", "Xorg", "X", "Xephyr", "Xvfb")
# proc 挂载点（单独抽出来，方便离线自测时替换成桩目录）。
_PROC = "/proc"


def find_x_server_auth_map() -> Dict[str, str]:
    """得到「显示号 → 这个会话真正在用的 -auth / XAUTHORITY 文件」。

    两个权威来源，cmdline 优先（它一定属于当前 X 服务器）：
      1) 宿主机 `/proc/<pid>/cmdline`：Xwayland / Xorg 命令行里的 `-auth <文件>`
      2) 宿主机 `/proc/<pid>/environ`：任何同 uid 进程环境里的 DISPLAY + XAUTHORITY

    返回形如 `{":1": "/run/user/1000/gamescope.Tj3k9Q", ":0": "/run/user/1000/xauth_ab12CD"}`。
    取不到的显示号不会出现在结果里。
    """
    out: Dict[str, str] = {}
    if not os.path.isdir(_PROC):
        return out
    for d in glob.glob(os.path.join(_PROC, "[0-9]*")):
        try:
            with open(os.path.join(d, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if not raw:
            continue
        argv = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
        if not argv or os.path.basename(argv[0]) not in _X_SERVER_EXES:
            continue
        disp, auth = "", ""
        for i, a in enumerate(argv):
            # 显示号通常写作 `:1` / `:0`（有时带 .screen，如 `:0.0`）
            if not disp and len(a) >= 2 and a[0] == ":" and a[1:].split(".")[0].isdigit():
                disp = ":" + a[1:].split(".")[0]
            if a == "-auth" and i + 1 < len(argv):
                auth = argv[i + 1]
            elif a.startswith("-auth="):
                auth = a.split("=", 1)[1]
        if not (disp and auth) or disp in out:
            continue
        try:
            if os.path.isfile(auth) and os.path.getsize(auth) > 0:
                out[disp] = auth
        except OSError:
            continue

    # cmdline 里没有的显示号，用别的进程的环境变量补上
    for disp, auth in _env_xauth_map().items():
        out.setdefault(disp, auth)
    return out


def host_proc_report() -> str:
    """宿主机 /proc 的「我是谁、看不看得到 X 进程」快照，用于诊断游戏模式。"""
    bits: List[str] = []
    try:
        with open(os.path.join(_PROC, "1", "comm"), "r", encoding="utf-8",
                  errors="replace") as f:
            bits.append("pid1=" + f.read().strip())
    except OSError:
        bits.append("pid1=?")
    bits.append(f"uid={os.getuid()}")
    bits.append("run_host=" + ("yes" if os.path.isdir("/run/host") else "no"))
    try:
        pids = glob.glob(os.path.join(_PROC, "[0-9]*"))
        bits.append(f"pids={len(pids)}")
    except Exception:
        pass
    # 有没有看得到带 -auth 的 X 服务器进程
    xsrv: List[str] = []
    for d in glob.glob(os.path.join(_PROC, "[0-9]*")):
        try:
            with open(os.path.join(d, "cmdline"), "rb") as f:
                raw = f.read()
        except OSError:
            continue
        argv = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
        if argv and os.path.basename(argv[0]) in _X_SERVER_EXES:
            xsrv.append(" ".join(argv)[:120])
    bits.append("X进程=" + (" | ".join(xsrv[:3]) if xsrv else "无"))
    return "，".join(bits)


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


def container_x_report(container: str, log: LogCb) -> List[str]:
    """把「容器里到底有没有 X、能不能连」的原始证据打出来，一次导出即可定位。

    返回从容器内 `/tmp/.X11-unix` 看到的显示号列表（如 `[":0", ":1"]`）——
    容器里连得上的显示号只可能是这些，拿它当候选比硬编码 `:0/:1` 靠谱。
    """
    out = ""
    try:
        r = _exec(container, [], "printf '容器内 uid=%s gid=%s HOME=%s\\n' "
                                 "\"$(id -u)\" \"$(id -g)\" \"$HOME\"; "
                                 "ls -l /tmp/.X11-unix 2>&1 | head -8; "
                                 "echo '--- /run/user/<uid>:'; ls -l /run/user/*/ 2>&1 | head -12; "
                                 "echo '--- 容器 sees /run/host:'; "
                                 "[ -d /run/host ] && echo yes || echo no",
                  timeout=10.0)
        out = (r.stdout or b"").decode(errors="replace").strip()
    except Exception:
        pass
    if out:
        log("info", "容器内 X 实况：" + out.replace("\n", " | "))

    found: List[str] = []
    try:
        for m in re.finditer(r"\bX(\d{1,2})\b", out):
            d = ":" + m.group(1)
            if d not in found:
                found.append(d)
    except Exception:
        pass
    return found


_HOST_PROBE_SNIPPET = (
    "import ctypes,sys\n"
    "lib=None\n"
    "for name in ('libX11.so.6','libX11.so'):\n"
    "    try:\n"
    "        lib=ctypes.CDLL(name);break\n"
    "    except OSError:\n"
    "        pass\n"
    "if lib is None: sys.exit(3)\n"
    "lib.XOpenDisplay.restype=ctypes.c_void_p\n"
    "lib.XOpenDisplay.argtypes=[ctypes.c_char_p]\n"
    "lib.XCloseDisplay.argtypes=[ctypes.c_void_p]\n"
    "d=lib.XOpenDisplay(None)\n"
    "if not d: sys.exit(1)\n"
    "lib.XCloseDisplay(d)\n"
    "sys.exit(0)\n"
)


def host_can_open_display(xauth: Optional[str]) -> Optional[bool]:
    """在**宿主机**上用 libX11 真连一次 X（不经过容器）。

    这是判断「容器失败到底怪 cookie 还是怪别的」的关键对照：App 自己的 Qt 窗口
    就在宿主机的这台上显示，所以宿主机一定是能连的——看它是「不需要 cookie」
    还是「需要某个 cookie」，就能立刻定性。
    返回 True=能连 / False=连不上 / None=测不了（没有 libX11 或没有 python）。
    """
    env = dict(os.environ)
    if xauth is None:
        env.pop("XAUTHORITY", None)
    else:
        env["XAUTHORITY"] = xauth
    try:
        r = subprocess.run(
            [sys.executable, "-c", _HOST_PROBE_SNIPPET],
            capture_output=True, timeout=15.0, env=env,
        )
    except Exception:
        return None
    if r.returncode == 3:
        return None
    return r.returncode == 0


def _host_probe_report(hosts: List[str]) -> str:
    """宿主机 libX11 实测小结（继承环境 / 无鉴权 / 头几个 cookie）。"""
    cases: List[Tuple[str, Optional[str]]] = [("继承环境", None),
                                              ("无鉴权(/dev/null)", "/dev/null")]
    for h in hosts[:3]:
        cases.append((os.path.basename(h), h))
    bits: List[str] = []
    for label, xa in cases:
        r = host_can_open_display(xa)
        if r is None:
            return ""            # libX11 不可用，整段跳过
        bits.append(f"{label}={'可连' if r else '连不上'}")
    return "，".join(bits)


def _probe_xauth_for_display(container: str, display: str, log: LogCb,
                             server_auth: str = "") -> Optional[Tuple[str, str, Optional[str]]]:
    """针对**一个** DISPLAY，逐个 XAUTHORITY 形态实测。返回 (name, mode, path) 或 None。

    `server_auth`：该显示号对应的 X 服务器 **-auth 文件**（由 `find_x_server_auth_map()`
    从 `/proc/<pid>/cmdline` 里读出来的）。这是权威答案，排在最前先试。

    候选顺序（v0.6.6 重排）：
      1) X 服务器 -auth 文件（权威）
      2) 能解析成 Xauthority 的真 cookie，**mtime 最新的在前**（当前会话的 -auth
         文件总是最新创建的，旧会话残留的 cookie 自然排后面）
      3) 无 cookie 的两种形态（/dev/null、unset）
      4) 解析不出结构的可疑文件（gamescope-environment / *.log 之类）丢到最后
    并且**不再有「每个显示号最多试 N 个」的硬上限**——旧上限 8 正好把 6 个
    gamescope.* cookie 砍掉 2 个（实测日志里 `E5RXM9P`/`NyKaC6g` 从没被试过）。
    """
    hosts = find_host_xauth_candidates()
    want = _display_number_of(display)

    # —— 无 cookie 的两种「干净」形态 —— #
    noauth_cands: List[Tuple[str, str, Optional[str]]] = [
        ("XAUTHORITY=/dev/null（强制不提供鉴权）", "path", "/dev/null"),
        ("unset XAUTHORITY（交给容器内默认）", "unset", None),
    ]

    # —— 权威候选：X 服务器命令行里 -auth 指定的那个文件 —— #
    auth_cands: List[Tuple[str, str, Optional[str]]] = []
    if server_auth:
        auth_cands.append((f"X 服务器 -auth 文件 {server_auth}", "path", server_auth))
        tmp = "/tmp/.airplay_xauth_srv"
        if _copy_in(container, server_auth, tmp, "644"):
            auth_cands.append((f"拷贝到容器内 {tmp}（源 {server_auth}）", "path", tmp))

    # —— 带 cookie 的候选：能解析成 Xauthority 的排前，mtime 最新优先 —— #
    real: List[Tuple[str, float, bool]] = []      # (路径, mtime, 是否覆盖当前显示号)
    junk: List[str] = []
    for host in hosts:
        if host == server_auth:
            continue
        ok, disps, mtime = xauth_file_info(host)
        if not ok:
            junk.append(host)
            continue
        covers = (want is None) or (not disps) or ("" in disps) or (want in disps)
        real.append((host, mtime, covers))
    real.sort(key=lambda t: (not t[2], -t[1]))

    cookie_cands: List[Tuple[str, str, Optional[str]]] = []
    for i, (host, _mt, covers) in enumerate(real):
        tag = "" if covers else "（不含当前显示号）"
        cookie_cands.append((f"原路径 {host}{tag}", "path", host))
        if i < 2:                                  # 只有最可能的两个再拷一份进容器
            tmp = f"/tmp/.airplay_xauth{i}"
            if _copy_in(container, host, tmp, "644"):
                cookie_cands.append((f"拷贝到容器内 {tmp}（源 {host}）", "path", tmp))
    if hosts:
        home = _container_home(container)
        dst = f"{home}/.Xauthority"
        # hosts[0] 可能正是 -auth 文件，也照拷一份到 HOME（Xlib 在 XAUTHORITY 未设时读它）
        if _copy_in(container, hosts[0], dst, "600"):
            cookie_cands.append((f"拷贝到容器 HOME {dst}", "path", dst))
    junk_cands: List[Tuple[str, str, Optional[str]]] = [
        (f"原路径 {h}", "path", h) for h in junk
    ]

    # 顺序：-auth 文件（权威）→ 真 cookie（新→旧）→ 无 cookie 形态 → 可疑文件。
    # ⚠️ 「无 cookie」排在 cookie 之后：9/11 游戏模式实机证明这台机器的 X 服务器
    #    是**要鉴权**的（/dev/null 与 unset 两种形态都 rc=255），先试它们纯属浪费。
    if auth_cands or cookie_cands:
        candidates = auth_cands + cookie_cands + noauth_cands + junk_cands
    else:
        candidates = noauth_cands + junk_cands

    for name, mode, path in candidates:
        shell = _prefix_for(mode, path) + GST_PROBE
        rc = -1
        why = ""
        try:
            r = _exec(container, ["-e", f"DISPLAY={display}"], shell)
            rc = r.returncode
            if rc == 0:
                log("info", f"X11 实测可用（ximagesink，DISPLAY={display}）→ {name}")
                return name, mode, path
            why = _short_reason(r)
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
                why = _short_reason(r2) or why
            except Exception:
                pass
        log("warn", f"X 不可用（DISPLAY={display} {name}），rc={rc}"
                    + (f"：{why}" if why else ""))
    return None


def probe_container_display(
    container: str,
    display: str,
    log: Optional[LogCb] = None,
) -> Tuple[str, Tuple[str, Optional[str]]]:
    """返回真正可用的 (DISPLAY, (mode, path))。

    ⚠️ 只试继承来的 DISPLAY 是不够的：游戏模式下 App 由 Steam/gamescope 拉起，
    环境里的 `DISPLAY=:1`，但 :1 上的 X 服务器可能不接受我们手上的 cookie。
    所以这里把候选显示号也一起枚举，逐个**实测**。

    ⚠️ 旧注释曾断言「gamescope 真正的 X 显示号是 `:0`」——**这是错的**。
    9/11 的实机日志里 `:1` 与 `:0` 的所有组合都 rc=255。真因不在显示号，
    而在 cookie 候选被截断（每显示号只试 8 个，6 个 gamescope.* 里漏掉 2 个）。
    """
    log = log or (lambda *a, **k: None)
    # 容器里看得见哪些 X socket（拿它当显示号候选，比硬编码 :0/:1 靠谱）
    socket_disps = container_x_report(container, log)

    # 宿主机 /proc 快照：判断「是不是因为 /proc 看不到宿主 X 进程」才读不到 -auth
    try:
        log("info", "宿主机 /proc 快照：" + host_proc_report())
    except Exception:
        pass

    # ★ 权威线索：X 服务器命令行里 `-auth` 指定的文件（或别的进程环境里的 XAUTHORITY）。
    auth_map = find_x_server_auth_map()
    if auth_map:
        log("info", "X 服务器 -auth 映射：" + " ; ".join(
            f"{d} → {p}" for d, p in sorted(auth_map.items())))
    else:
        log("warn", "没能从 /proc 读到任何 X 服务器的 -auth 文件（见上一行 /proc 快照），"
                    "只能按 cookie 结构 + mtime 猜")

    hosts = find_host_xauth_candidates()
    if hosts:
        log("info", "宿主机 X cookie 候选（已按可信度排序）：" + " , ".join(hosts[:8]))
    else:
        log("warn", f"宿主机未找到任何 X cookie 文件；DISPLAY={display}")

    # 宿主机 libX11 实测：区分「这台机器的 X 根本不要鉴权」还是「需要某个 cookie」
    try:
        rep = _host_probe_report(hosts)
        if rep:
            log("info", "宿主机 libX11 实测：" + rep)
    except Exception:
        pass

    cands: List[str] = []
    # 顺序：继承来的 DISPLAY → 环境里的 DISPLAY → 容器里看得见的 socket →
    #       常见 :0/:1 → -auth 映射里出现过的显示号
    for d in [display, os.environ.get("DISPLAY", ""), *socket_disps, ":0", ":1",
              *sorted(auth_map.keys())]:
        d = (d or "").strip()
        if d and d not in cands:
            cands.append(d)

    deadline = time.time() + _X_PROBE_BUDGET
    for disp in cands:
        if time.time() > deadline:
            log("warn", "X11 探测超时，停止继续尝试其它显示号")
            break
        got = _probe_xauth_for_display(container, disp, log, auth_map.get(disp, ""))
        if got:
            _name, mode, path = got
            if disp != display:
                log("info", f"继承的 DISPLAY={display} 在容器里不可用，已改用 {disp}")
            return disp, (mode, path)

    log("error",
        "所有 X11 显示号 / 鉴权方案都不可用，uxplay 会报 "
        "`Failed to initialize GStreamer video renderer`（现象：搜得到、连得上、没有画面）。"
        "日志里已带：容器内 X socket 实况、宿主机 /proc 快照（看得到 X 进程吗）、"
        "宿主机 libX11 实测（宿主是不需要 cookie 还是需要某个 cookie）、"
        "以及每个候选的失败原话。请把「导出日志」发我。")
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
        # XDG_RUNTIME_DIR：SteamOS 上是 /run/user/1000。容器里默认可能没有这个变量，
        # 而 GStreamer/GLib 会拿它去放各种 socket；显式给上更接近「宿主机原生环境」。
        "-e", f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}",
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
