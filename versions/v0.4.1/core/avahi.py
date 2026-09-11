"""avahi（mDNS）就绪检测。

iPhone / iPad / Mac 靠 DNS-SD 发现本机，所以 uxplay 启动前最好确认 avahi 在跑。

【重要教训 — 不要再用 pkexec/sudo】
旧版本在未就绪时会尝试 `pkexec systemctl start avahi-daemon` / `sudo systemctl start ...`，
而 SteamOS 上的 D-Bus 检测经常**误报未就绪**（实际 avahi 是活的，设备照样能搜到），
导致每次点「开始接收」都弹一次 polkit 密码框——用户反馈「每次都要重新输密码」就是它。
现在这里**只做无权限的只读检测**，绝不触发任何提权：检测不到就记一条 warn 继续跑，
反正 uxplay 自己也能注册成功。
"""

import shutil
import subprocess


def _run(cmd, timeout=3.0):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def avahi_ready() -> bool:
    """只读检测 avahi 是否在跑（不需要任何权限，不会弹密码框）。"""
    # 1) systemd 状态查询（is-active 是只读操作，不需要 root）
    if shutil.which("systemctl") and _run(["systemctl", "is-active", "avahi-daemon"]):
        return True
    # 2) 进程是否存在
    if shutil.which("pgrep") and _run(["pgrep", "-x", "avahi-daemon"]):
        return True
    # 3) D-Bus 上能否问到版本号（旧方法，某些环境不可用，放最后）
    if shutil.which("dbus-send") and _run([
        "dbus-send", "--system", "--print-reply",
        "--dest=org.freedesktop.Avahi.Server", "/",
        "org.freedesktop.Avahi.Server.GetVersionString",
    ]):
        return True
    return False


def ensure_avahi(_timeout: float = 0.0) -> bool:
    """确认 avahi 就绪。**刻意不做任何提权操作**（不再 pkexec/sudo）。

    保留 _timeout 参数是为了兼容旧调用点；这里只做一次即时检测。
    """
    return avahi_ready()
