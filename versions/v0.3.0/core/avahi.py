"""avahi（mDNS）就绪门闸。

iPhone / iPad / Mac 靠 DNS-SD 发现本机，所以 uxplay 启动前必须确认 avahi 已在
系统 D-Bus 上注册好服务，否则会出现「No DNS-SD Server found」导致设备搜不到、
进程静默退出。这里复用插件版的轮询等待思路，但去掉了容器/sudo 部分——
独立 App 跑在用户会话，avahi 是系统服务，只需等待它就绪即可。
"""

import subprocess
import time


def avahi_ready(timeout: float = 20.0) -> bool:
    """轮询系统 D-Bus 上的 Avahi.Server，直到能响应 GetVersionString。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = subprocess.run(
                [
                    "dbus-send", "--system", "--print-reply",
                    "--dest=org.freedesktop.Avahi.Server",
                    "/", "org.freedesktop.Avahi.Server.GetVersionString",
                ],
                capture_output=True, timeout=2.0,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def ensure_avahi(timeout: float = 20.0) -> bool:
    """确认 avahi 就绪；未就绪时 best-effort 尝试拉起（多数桌面已默认运行）。"""
    if avahi_ready(2.0):
        return True

    # 尝试用系统工具拉起（需要权限；SteamOS 上 avahi 通常已经在跑）
    for cmd in (
        ["pkexec", "systemctl", "start", "avahi-daemon"],
        ["sudo", "systemctl", "start", "avahi-daemon"],
        ["avahi-daemon", "--daemonize"],
    ):
        try:
            subprocess.run(cmd, capture_output=True, timeout=10.0)
        except Exception:
            pass
        if avahi_ready(2.0):
            return True

    # 最后再给一轮完整等待
    return avahi_ready(timeout)
