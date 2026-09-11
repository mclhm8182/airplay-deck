"""容器内 X11 鉴权的处理：不靠猜，靠实测 + 打日志。

背景
----
uxplay 跑在 distrobox / podman 容器里时，宿主机的 XAUTHORITY 路径
（典型如 /run/user/1000/xauth_XXXX）在容器内通常不存在或不可读，Xlib 会报：

    Authorization required, but no authorization protocol specified
    GStreamer error: Could not initialise X output
    *** ERROR: Failed to initialize GStreamer video renderer

**关键后果**：uxplay 在视频渲染器初始化失败后**并不会退出**，它继续监听 socket
并接受 iOS 的连接（日志里能看到 "Accepted IPv4 client"），于是出现
「iOS 搜得到、连得上、有声音，但没画面 / 提示无法连接」这种极具迷惑性的现象。

不同环境下可用的 cookie 方案差别很大（原路径直接可见 / 需拷贝到 /tmp /
需拷进容器 HOME / gamescope 根本不鉴权），所以这里枚举候选方案，
在容器里用 xdpyinfo 或 xset 实测哪个真的能连上 X，选中可用的那个。
每一步都写日志，下次排错能一眼看出走的哪条路。
"""

import glob
import os
import subprocess
from typing import Callable, Optional, List, Tuple

LogCb = Callable[[str, str], None]

PROBE_SH = (
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


def _exec(container: str, env_args: List[str], cmd: str, timeout: float = 8.0):
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


def probe_container_xauth(
    container: str,
    display: str,
    log: Optional[LogCb] = None,
) -> Optional[str]:
    """返回容器内应该使用的 XAUTHORITY 路径；None = 不传（走无鉴权/容器内默认）。

    依次实测候选方案，选中第一个真的能连上 X 的。全部无法实测时返回 None 并记 warn。
    """
    log = log or (lambda *a, **k: None)
    env_xauth = (os.environ.get("XAUTHORITY") or "").strip()
    host = find_host_xauth()
    if host:
        src = "环境变量" if env_xauth else "自动探测（env 未设置）"
        log("info", f"宿主机 X cookie：{host}（{src}）")
    else:
        log("warn", "宿主机未找到 X cookie 文件（XAUTHORITY 未设置且常见路径也搜不到）；"
                    "将走无鉴权连接")

    noauth: Tuple[str, Optional[str]] = ("不传 XAUTHORITY（无鉴权 / 容器默认）", None)

    # 候选 1..3 都需要 cookie 文件
    cookie_cands: List[Tuple[str, Optional[str]]] = []
    if host:
        # A) 原路径：distrobox 常把 /run/user/$UID 挂进容器，路径可能直接可用
        cookie_cands.append((f"原路径 {host}", host))

        # B) 拷贝到容器 /tmp（权限放宽，避免 subuid 映射导致读不到）
        tmp = "/tmp/.airplay_xauth"
        if _copy_in(container, host, tmp, "644"):
            cookie_cands.append((f"拷贝到容器内 {tmp}", tmp))
        else:
            log("warn", f"podman cp cookie 到 {tmp} 失败")

        # C) 拷贝进容器 HOME（Xlib 的默认查找位置）
        home = _container_home(container)
        dst = f"{home}/.Xauthority"
        if dst != tmp and _copy_in(container, host, dst, "600"):
            cookie_cands.append((f"拷贝到容器 HOME {dst}", dst))

    # 排序很关键：
    #   * 宿主机**显式设置了** XAUTHORITY（桌面模式）→ 优先用 cookie；
    #   * 宿主机**没有**设置（游戏模式 gamescope 常见）→ 说明该会话本来就不需要 cookie，
    #     这时硬传一个（哪怕是空的）反而会让 Xlib 报
    #     "Authorization required, but no authorization protocol specified"，
    #     所以这种情况把「不传」排在最前。
    if env_xauth and cookie_cands:
        candidates = cookie_cands + [noauth]
    else:
        candidates = [noauth] + cookie_cands

    for name, path in candidates:
        env = ["-e", f"DISPLAY={display}"]
        if path:
            env += ["-e", f"XAUTHORITY={path}"]
        try:
            r = _exec(container, env, PROBE_SH, timeout=10.0)
        except Exception as e:
            log("warn", f"X 探测异常（{name}）：{e}")
            continue

        if r.returncode == 0:
            log("info", f"X11 实测可用 → {name}")
            return path

        if r.returncode == 3:
            # 容器内没有 xdpyinfo / xset，无法实测，退化为「文件是否可读」
            if path is None:
                log("warn", "容器内无 xdpyinfo/xset，无法实测 X 连通性；将尝试不传 XAUTHORITY")
                return None
            try:
                rr = _exec(container, env, f"test -r '{path}'", timeout=6.0)
            except Exception:
                continue
            if rr.returncode == 0:
                log("info", f"X11 cookie 在容器内可读 → {name}（容器内无 xdpyinfo/xset，未实测）")
                return path
            log("warn", f"X11 cookie 在容器内不可读：{path}")
        else:
            log("warn", f"X 不可用（{name}），rc={r.returncode}")

    log("error", "所有 X11 鉴权方案都不可用；uxplay 大概率起不来画面（只有声音）。"
                 "请导出日志查看上面各条探测结果。")
    return None
