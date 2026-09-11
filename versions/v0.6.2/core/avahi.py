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
from typing import Callable, Optional, Tuple

LogCb = Callable[[str, str], None]

# 容器内「系统 D-Bus socket」的候选路径。
#
# 为什么需要这个：uxplay 用的是 libavahi-compat-libdnssd，它**靠系统 D-Bus**
# （不是靠 /run/avahi-daemon/socket）去连宿主机的 avahi-daemon。
# 容器里只要没有这个 socket，uxplay 就会：
#     *** ERROR: No DNS-SD Server found (DNSServiceRegister call returned kDNSServiceErr_Unknown)
# 然后退出码 0、存活 1 秒，反复重启 —— 客户端永远搜不到设备。
#
#   * /run/dbus/system_bus_socket        标准路径（distrobox/toolbx 通常会挂进来）
#   * /run/host/run/dbus/system_bus_socket  distrobox 把宿主根挂在 /run/host 时的等价路径
#   * /var/run/dbus/system_bus_socket    /var/run → /run 的等价路径（老镜像）
_CTR_DBUS_CANDIDATES = (
    "/run/dbus/system_bus_socket",
    "/run/host/run/dbus/system_bus_socket",
    "/var/run/dbus/system_bus_socket",
)

# 在容器内逐个候选路径试连 avahi：
#   1) 先不带任何覆盖，试容器默认的 D-Bus 地址；
#   2) 再逐路径设置 DBUS_SYSTEM_BUS_ADDRESS 试；
# 成功输出 DBUS_OK=<addr>；失败输出每个 socket 是否存在 + 是否有 dbus-send。
_CTR_DBUS_PROBE = (
    "_try() { command -v dbus-send >/dev/null 2>&1 || return 2; "
    "dbus-send --system --print-reply --dest=org.freedesktop.Avahi.Server / "
    "org.freedesktop.Avahi.Server.GetVersionString >/dev/null 2>&1; }\n"
    "if _try; then echo 'DBUS_OK=default'; exit 0; fi\n"
    "_seen=''\n"
    "for _p in " + " ".join(_CTR_DBUS_CANDIDATES) + "; do\n"
    "  [ -S \"$_p\" ] || continue\n"
    "  _seen=\"$_seen $_p\"\n"
    "  if DBUS_SYSTEM_BUS_ADDRESS=\"unix:path=$_p\" _try; then echo \"DBUS_OK=unix:path=$_p\"; exit 0; fi\n"
    "done\n"
    "echo \"SOCKETS=${_seen:- none}\"\n"
    "command -v dbus-send >/dev/null 2>&1 && echo 'HAS_DBUS_SEND=1' || echo 'HAS_DBUS_SEND=0'\n"
    "ls -l /run/dbus 2>&1 | head -3\n"
    "exit 1\n"
)


def _podman_exec(container: str, cmd: str, timeout: float = 25.0):
    return subprocess.run(
        ["podman", "exec", container, "sh", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def container_dbus_address(container: str,
                           log: Optional[LogCb] = None) -> str:
    """在容器内探测「能真正连上宿主 avahi」的系统 D-Bus 地址。

    返回要额外导出的 `DBUS_SYSTEM_BUS_ADDRESS` 值：
      * `""`            —— 容器默认地址就能连上（或环境探测不了），不需要额外导出；
      * `"unix:path=…"` —— 默认地址不通、但换成这个路径就通，调用方需要导出它。

    只读探测，不修改容器；不触发任何提权。
    """
    log = log or (lambda *a, **k: None)
    if shutil.which("podman") is None:
        return ""
    try:
        r = _podman_exec(container, _CTR_DBUS_PROBE)
    except Exception as e:
        log("warn", f"容器内 mDNS 探测异常（忽略，继续启动）：{e}")
        return ""
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        detail = out.strip().replace("\n", " | ")[:300]
        log("error",
            "容器内连不上宿主机的 mDNS 服务（avahi）：uxplay 会报 "
            "`No DNS-SD Server found (kDNSServiceErr_Unknown)` 并反复退出，"
            f"设备搜不到。探测详情：{detail or '无输出'}")
        log("error",
            "常见原因：容器缺少宿主的系统 D-Bus socket。可在终端执行下面这条命令看完整情况，"
            "并把输出发我：\n"
            f"  podman exec {container} sh -c 'ls -l /run/dbus /run/host/run/dbus 2>&1; "
            "which dbus-send avahi-browse; "
            "DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/host/run/dbus/system_bus_socket "
            "dbus-send --system --print-reply --dest=org.freedesktop.Avahi.Server / "
            "org.freedesktop.Avahi.Server.GetVersionString'")
        return ""
    for line in out.splitlines():
        line = line.strip()
        if line == "DBUS_OK=default":
            log("info", "容器内可直接连上宿主机 mDNS 服务（avahi）")
            return ""
        if line.startswith("DBUS_OK="):
            addr = line[len("DBUS_OK="):].strip()
            log("info", f"容器默认 D-Bus 不通；已改用 {addr} 连宿主机 mDNS 服务（avahi）")
            return addr
    return ""


def container_avahi_ok(container: str) -> bool:
    """容器内能否连上 avahi（供「一键检查运行环境」用）。探测不了时返回 True。"""
    if shutil.which("podman") is None:
        return True
    try:
        r = _podman_exec(container, _CTR_DBUS_PROBE, timeout=25.0)
        return r.returncode == 0
    except Exception:
        return True


# 一次性把「为什么搜不到设备」需要的全部证据打印出来的只读脚本。
_CTR_MDNS_REPORT = (
    "echo '# /run/dbus:'; ls -l /run/dbus 2>&1 | head -5\n"
    "echo '# /run/host/run/dbus:'; ls -l /run/host/run/dbus 2>&1 | head -5\n"
    "echo '# /run/avahi-daemon:'; ls -l /run/avahi-daemon 2>&1 | head -5\n"
    "echo \"# /run/host present: $([ -d /run/host ] && echo yes || echo no)\"\n"
    "echo \"# dbus-send: $(command -v dbus-send || echo MISSING)\"\n"
    "echo \"# avahi-browse: $(command -v avahi-browse || echo MISSING)\"\n"
    "echo \"# uxplay: $(command -v uxplay || echo MISSING)\"\n"
    "echo \"# DBUS_SYSTEM_BUS_ADDRESS=${DBUS_SYSTEM_BUS_ADDRESS:-<unset>}\"\n"
    "_q() { dbus-send --system --print-reply --dest=org.freedesktop.Avahi.Server / "
    "org.freedesktop.Avahi.Server.GetVersionString 2>&1 | head -3; }\n"
    "echo '# avahi via default bus:'; _q\n"
    "for _p in /run/dbus/system_bus_socket /run/host/run/dbus/system_bus_socket; do\n"
    "  [ -S \"$_p\" ] || continue\n"
    "  echo \"# avahi via $_p:\"\n"
    "  DBUS_SYSTEM_BUS_ADDRESS=\"unix:path=$_p\" _q\n"
    "done\n"
)


def container_mdns_report(container: str) -> str:
    """导出「容器内 mDNS/D-Bus 实况」多行文本，供诊断日志使用。"""
    if shutil.which("podman") is None:
        return "（本机无 podman，跳过容器内 mDNS 探测）"
    try:
        r = _podman_exec(container, _CTR_MDNS_REPORT, timeout=30.0)
        txt = ((r.stdout or "") + (r.stderr or "")).strip()
        return txt or f"（无输出，rc={r.returncode}）"
    except Exception as e:
        return f"（探测异常：{e}）"


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
