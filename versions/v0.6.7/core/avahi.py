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

try:  # 正常以包导入
    from .x11 import SANITIZE_ENV as _SANITIZE_ENV
except ImportError:  # 直接按文件路径加载本模块（离线自测）时的兜底
    _SANITIZE_ENV = ["-e", "LD_PRELOAD=", "-e", "LD_LIBRARY_PATH="]

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

# 判断「avahi 是否已挂在我连的这条系统总线上」。
#
# ⚠️ 关键坑（v0.6.3 / v0.6.4 一直误判的根因）：
#   avahi-daemon 注册的**总线名**是 `org.freedesktop.Avahi`；
#   `org.freedesktop.Avahi.Server` 是它的**接口名**（对象路径 `/`），不是总线名。
#   过去用 `dbus-send --dest=org.freedesktop.Avahi.Server … GetVersionString` 探测，
#   永远得到
#     ServiceUnknown: The name org.freedesktop.Avahi.Server was not provided
#   于是：① setup_container_mdns() 每次都误判「容器内 avahi 未就绪」，白跑一遍
#   自建流程（还连带每次都 kill/重启 avahi）；② mDNS 明明是好的（手机照样连得上、
#   能投屏），却每次都刷一条吓人的 error，把排查带偏。
#   改用 ListNames 里有没有 `org.freedesktop.Avahi` 判断，与 avahi 版本无关。
#   grep 模式写成字符类 `org[.]freedesktop[.]Avahi`，省掉反斜杠在多层转义里的坑。
_AVAHI_ON_BUS_BODY = (
    "command -v dbus-send >/dev/null 2>&1 || return 2; "
    "dbus-send --system --print-reply --dest=org.freedesktop.DBus / "
    "org.freedesktop.DBus.ListNames 2>/dev/null "
    "| grep -q 'org[.]freedesktop[.]Avahi'"
)

# 在容器内逐个候选路径试连 avahi：
#   1) 先不带任何覆盖，试容器默认的 D-Bus 地址；
#   2) 再逐路径设置 DBUS_SYSTEM_BUS_ADDRESS 试；
# 成功输出 DBUS_OK=<addr>；失败输出每个 socket 是否存在 + 是否有 dbus-send。
_CTR_DBUS_PROBE = (
    "_try() { " + _AVAHI_ON_BUS_BODY + "; }\n"
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
        ["podman", "exec", *_SANITIZE_ENV, container, "sh", "-c", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def container_dbus_address(container: str,
                           log: Optional[LogCb] = None) -> Optional[str]:
    """在容器内探测「能真正连上宿主 avahi」的系统 D-Bus 地址。

    返回：
      * `""`            —— 容器默认地址就能连上，不需要额外导出；
      * `"unix:path=…"` —— 默认地址不通、但换成这个路径就通，调用方需要导出它；
      * `None`          —— 宿主 D-Bus 在容器内**根本用不了**（rootless uid 映射 /
                           socket 缺失），调用方应改用容器内自建 mDNS。

    只读探测，不修改容器；不触发任何提权。
    """
    log = log or (lambda *a, **k: None)
    if shutil.which("podman") is None:
        return ""
    try:
        r = _podman_exec(container, _CTR_DBUS_PROBE)
    except Exception as e:
        log("warn", f"容器内 mDNS 探测异常：{e}")
        return None
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        detail = out.strip().replace("\n", " | ")[:300]
        log("info", f"容器默认 D-Bus 连不上宿主 avahi（{detail or '无输出'}）")
        return None
    for line in out.splitlines():
        line = line.strip()
        if line == "DBUS_OK=default":
            log("info", "容器内可直接连上宿主机 mDNS 服务（avahi）")
            return ""
        if line.startswith("DBUS_OK="):
            addr = line[len("DBUS_OK="):].strip()
            log("info", f"容器默认 D-Bus 不通；已改用 {addr} 连宿主机 mDNS 服务（avahi）")
            return addr
    return None


# 在容器内**自建**一套 system D-Bus + avahi-daemon 的脚本（以容器内 root 运行）。
#
# 为什么需要：uxplay 的 libavahi-compat-libdnssd 只能经**系统 D-Bus** 找 avahi。
# 而宿主机的 D-Bus 在 rootless 容器里往往「连得上、要不到回复」
# （dbus-send 报 `Did not receive a reply`）——这是 rootless 的 user-namespace /
# uid 映射与 D-Bus EXTERNAL 鉴权打架导致的，换 socket 路径治不好。
# 最稳的办法是**不再碰宿主的 D-Bus**：容器与宿主共享网络命名空间，
# 在容器内起一套自己的 dbus + avahi，一样能把服务广播到局域网，客户端侧完全无感。
_CTR_MDNS_SETUP = (
    "set -u\n"
    "_say() { echo \"[mdns] $*\"; }\n"
    "export DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket\n"
    "_bus_ok() { dbus-send --system --print-reply --dest=org.freedesktop.DBus / "
    "org.freedesktop.DBus.ListNames >/dev/null 2>&1; }\n"
    "_avahi_ok() { " + _AVAHI_ON_BUS_BODY + "; }\n"
    "# 0) 已经能用就什么都不动\n"
    "if _avahi_ok; then _say '容器内已能连上 avahi，mDNS 无需处理'; exit 0; fi\n"
    "# 1) 依赖：旧容器可能没装 dbus / avahi-daemon（avahi-utils ≠ avahi-daemon）\n"
    "if ! command -v dbus-daemon >/dev/null 2>&1 || ! command -v avahi-daemon >/dev/null 2>&1; then\n"
    "  _say '容器内缺少 dbus / avahi-daemon，先补装（需要联网，约十几秒）…'\n"
    "  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update >/dev/null 2>&1 || true\n"
    "  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y dbus avahi-daemon "
    ">/tmp/airplay-mdns-apt.log 2>&1 || tail -5 /tmp/airplay-mdns-apt.log\n"
    "fi\n"
    "command -v dbus-daemon >/dev/null 2>&1 || { _say 'dbus-daemon 仍缺失，放弃'; exit 9; }\n"
    "command -v avahi-daemon >/dev/null 2>&1 || { _say 'avahi-daemon 仍缺失，放弃'; exit 9; }\n"
    "mkdir -p /run/dbus /run/avahi-daemon /var/lib/dbus\n"
    "if command -v dbus-uuidgen >/dev/null 2>&1 && [ ! -s /var/lib/dbus/machine-id ]; then\n"
    "  dbus-uuidgen --ensure >/dev/null 2>&1 || true\n"
    "fi\n"
    "# distrobox 会在 /run/avahi-daemon/socket 放一个指向宿主的符号链接，会挡路，先清掉\n"
    "[ -L /run/avahi-daemon/socket ] && rm -f /run/avahi-daemon/socket\n"
    "# 2) 容器内私有 system D-Bus。\n"
    "#    刻意用自带的最小配置，而不是 --system：后者依赖发行版 system.conf 和 messagebus 用户，\n"
    "#    在精简容器里任一缺失都会起不来。这是一条容器内的私有总线，放宽策略没有额外风险。\n"
    "cat > /tmp/airplay-dbus.conf <<'DBCONF'\n"
    "<!DOCTYPE busconfig PUBLIC \"-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN\"\n"
    " \"http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd\">\n"
    "<busconfig>\n"
    "  <type>system</type>\n"
    "  <listen>unix:path=/run/dbus/system_bus_socket</listen>\n"
    "  <pidfile>/run/dbus/pid</pidfile>\n"
    "  <auth>EXTERNAL</auth>\n"
    "  <policy context=\"default\">\n"
    "    <allow send_destination=\"*\"/>\n"
    "    <allow receive_sender=\"*\"/>\n"
    "    <allow own=\"*\"/>\n"
    "    <allow user=\"*\"/>\n"
    "  </policy>\n"
    "</busconfig>\n"
    "DBCONF\n"
    "if ! _bus_ok; then\n"
    "  _say '启动容器内私有 system D-Bus…'\n"
    "  rm -f /run/dbus/system_bus_socket /run/dbus/pid\n"
    "  dbus-daemon --config-file=/tmp/airplay-dbus.conf --fork >/tmp/airplay-dbus.log 2>&1 || true\n"
    "  sleep 1\n"
    "fi\n"
    "if ! _bus_ok; then _say '私有 D-Bus 没能起来：'; tail -6 /tmp/airplay-dbus.log 2>&1; exit 10; fi\n"
    "_say '私有 D-Bus 就绪'\n"
    "# 3) 容器内 avahi-daemon（共享宿主网络命名空间 → 直接广播到局域网）\n"
    "if ! _avahi_ok; then\n"
    "  # 先把残留实例清掉：之前失败的 avahi 会变成僵尸/孤儿一直挂着（容器 PID 1 不回收），\n"
    "  # 新实例可能因此拿不到名字。既然 _avahi_ok 为假，说明现存实例本就没在服务，杀掉安全。\n"
    "  for _d in /proc/[0-9]*; do\n"
    "    [ \"$(cat $_d/comm 2>/dev/null)\" = avahi-daemon ] || continue\n"
    "    _p=${_d#/proc/}; echo \"[mdns] 清理残留 avahi-daemon $_p\"; kill -9 \"$_p\" 2>/dev/null || true\n"
    "  done\n"
    "  sleep 1\n"
    "  _say '启动容器内 avahi-daemon…'\n"
    "  rm -f /run/avahi-daemon/pid\n"
    "  : > /tmp/airplay-avahi.log\n"
    "  # ★ 刻意**不加 --daemonize**：它 daemonize 后会关掉 stdio，失败原因就再也看不到\n"
    "  #   （v0.6.3 的日志里只剩 3 行 ld.so 报错，什么线索都没有）。\n"
    "  #   用 setsid 脱离 podman exec 的会话在后台跑，stderr 持续写进日志，配 --debug 更详细。\n"
    "  if command -v setsid >/dev/null 2>&1; then\n"
    "    setsid avahi-daemon --no-drop-root --no-chroot --no-rlimits --debug "
    "</dev/null >>/tmp/airplay-avahi.log 2>&1 &\n"
    "  else\n"
    "    nohup avahi-daemon --no-drop-root --no-chroot --no-rlimits --debug "
    "</dev/null >>/tmp/airplay-avahi.log 2>&1 &\n"
    "  fi\n"
    "  # 最多等 8 秒（avahi 要等 D-Bus 名字申请 + 网络接口就绪）\n"
    "  _i=0\n"
    "  while [ $_i -lt 8 ]; do\n"
    "    sleep 1; _avahi_ok && break; _i=$((_i+1))\n"
    "  done\n"
    "fi\n"
    "if _avahi_ok; then _say '容器内 avahi 就绪（mDNS 将直接广播到局域网）'; exit 0; fi\n"
    "_say 'avahi 仍未就绪，启动日志（含失败原因）：'; tail -20 /tmp/airplay-avahi.log 2>&1; exit 11\n"
)


def _podman_exec_root(container: str, cmd: str, timeout: float = 300.0):
    """以容器内 root 执行（自带 dbus/avahi 的补装与启动需要 root）。

    少数容器的默认用户不是 root 且 `-u 0` 查不到该用户，此时退回默认用户再试一次
    （如果默认用户本来就没有 apt 权限，第二次也会失败，错误照常上报）。
    """
    try:
        r = subprocess.run(
            ["podman", "exec", "-u", "0", *_SANITIZE_ENV, container, "sh", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        raise
    err = (r.stderr or "").lower()
    if r.returncode != 0 and ("unable to find user" in err or "no matching entries" in err):
        r = subprocess.run(
            ["podman", "exec", *_SANITIZE_ENV, container, "sh", "-c", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
    return r


def setup_container_mdns(container: str,
                         log: Optional[LogCb] = None) -> str:
    """确保 uxplay 在容器内能连上 mDNS（avahi），返回要导出的 D-Bus 地址。

    返回 `""` 表示用容器默认地址即可（或没辙了）；否则返回 `unix:path=…`，
    调用方必须把它作为 `DBUS_SYSTEM_BUS_ADDRESS` 传进 uxplay。

    顺序：宿主 D-Bus（默认地址）→ 宿主 D-Bus（/run/host 下的候选路径）
        → 容器内自建 dbus + avahi。
    """
    log = log or (lambda *a, **k: None)
    if shutil.which("podman") is None:
        return ""
    addr = container_dbus_address(container, log)
    if addr is not None:
        return addr

    # 宿主 D-Bus 在容器内不可用 → 容器内自建一套，彻底绕开 userns/uid 映射问题
    log("warn", "宿主机的 D-Bus 在容器内不可用（rootless 容器的 uid 映射问题，"
                "换路径也治不好）。改为在容器内启动独立的 dbus + avahi…")
    try:
        r = _podman_exec_root(container, _CTR_MDNS_SETUP)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        for line in out.splitlines():
            if line.strip():
                log("info", line.strip())
        if r.returncode == 0 and container_avahi_ok(container):
            log("info", "已启用容器内独立 mDNS 服务（avahi）—— 手机/电脑应能正常搜到设备")
            return "unix:path=/run/dbus/system_bus_socket"
    except Exception as e:
        log("error", f"容器内自建 mDNS 失败：{e}")

    log("error",
        "容器内仍无法使用 mDNS（avahi），uxplay 会报 "
        "`No DNS-SD Server found (kDNSServiceErr_Unknown)` 并反复退出，设备搜不到。"
        "「导出日志」里已附带完整诊断（含容器内 dbus/avahi 启动日志），发我即可定位。")
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
    "echo \"# dbus-daemon: $(command -v dbus-daemon || echo MISSING)\"\n"
    "echo \"# avahi-daemon: $(command -v avahi-daemon || echo MISSING)\"\n"
    "echo \"# avahi-browse: $(command -v avahi-browse || echo MISSING)\"\n"
    "echo \"# uxplay: $(command -v uxplay || echo MISSING)\"\n"
    "echo \"# DBUS_SYSTEM_BUS_ADDRESS=${DBUS_SYSTEM_BUS_ADDRESS:-<unset>}\"\n"
    "# 容器内实际在跑的进程（不依赖 pgrep，裸镜像没有 procps）\n"
    "for _p in /proc/[0-9]*; do _c=$(cat $_p/comm 2>/dev/null) || continue; "
    "case \"$_c\" in dbus-daemon|avahi-daemon) echo \"# running: $_c (pid ${_p#/proc/})\";; esac; done\n"
    "_q() { if " + _AVAHI_ON_BUS_BODY + "; then echo '  OK: 总线上有 org.freedesktop.Avahi'; "
    "else echo '  NO: 这条总线上没有 org.freedesktop.Avahi'; fi; }\n"
    "echo '# avahi via default bus:'; _q\n"
    "for _p in /run/dbus/system_bus_socket /run/host/run/dbus/system_bus_socket; do\n"
    "  [ -S \"$_p\" ] || continue\n"
    "  echo \"# avahi via $_p:\"\n"
    "  DBUS_SYSTEM_BUS_ADDRESS=\"unix:path=$_p\" _q\n"
    "done\n"
    "# 自建 mDNS 时留下的启动日志（App 会自动在容器内拉起 dbus + avahi）\n"
    "echo '# /tmp/airplay-dbus.log:'; tail -6 /tmp/airplay-dbus.log 2>&1\n"
    "echo '# /tmp/airplay-avahi.log:'; tail -8 /tmp/airplay-avahi.log 2>&1\n"
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
    # 3) 系统总线上有没有 avahi 的名字（旧方法，某些环境不可用，放最后）
    #    ⚠️ 查的是**总线名** `org.freedesktop.Avahi`，不是接口名
    #    `org.freedesktop.Avahi.Server`——后者永远返回 ServiceUnknown（详见
    #    _AVAHI_ON_BUS_BODY 的说明），这是 v0.6.3/v0.6.4 误报的根因。
    if shutil.which("dbus-send"):
        try:
            r = subprocess.run(
                ["dbus-send", "--system", "--print-reply",
                 "--dest=org.freedesktop.DBus", "/",
                 "org.freedesktop.DBus.ListNames"],
                capture_output=True, timeout=4.0,
            )
            if r.returncode == 0 and b"org.freedesktop.Avahi" in (r.stdout or b""):
                return True
        except Exception:
            pass
    return False


def ensure_avahi(_timeout: float = 0.0) -> bool:
    """确认 avahi 就绪。**刻意不做任何提权操作**（不再 pkexec/sudo）。

    保留 _timeout 参数是为了兼容旧调用点；这里只做一次即时检测。
    """
    return avahi_ready()
