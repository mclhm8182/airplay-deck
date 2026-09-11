"""首次启动自动安装 / 一键清爽卸载 uxplay 运行环境（distrobox 容器）。

方案 B：AppImage 本身不含 uxplay。新用户首次打开 App 时，自动拉取并构建
`uxplay-env` 容器，容器里装好 uxplay + GStreamer 全套插件，之后即可投屏。
卸载时清掉容器 + 镜像（若不被其它容器占用）+ 应用设置与日志，清爽如初。

所有命令走 rootless 的 distrobox / podman，落在用户家目录，**不需要 sudo**，
也绕开 SteamOS 只读根。镜像拉取与 apt 安装耗时较长，调用方应在后台线程里跑，
并通过 log_fn 逐行回显进度。

踩过的坑（写在前面）
----------------------
1. 安装时必须用 root 跑 apt：`distrobox enter` 默认以宿主普通用户（deck）进入，
   apt-get 需要 root，否则报 "Could not open lock file ... Permission denied"。
   因此安装步骤统一用 `podman exec -u 0` 以容器内的 root 跑 apt，不依赖 sudo/密码。
2. 卸载时容器可能卡在坏状态（conmon exited prematurely），`distrobox rm -f` /
   普通 `podman rm -f` 会卡在「无法停止」。这里用多策略强制删除：
   先 raw `podman rm -f`，失败则杀掉该容器的 conmon 进程再重试，最后回退 distrobox。
3. 重建前先判容器是否可用（`podman exec <c> true`）；不可用（含卡死）则强制删掉重建，
   避免出现「容器已存在 → 跳过创建 → 但里面 uxplay 没装好」的死循环。
"""

import os
import shutil
import signal
import subprocess
import time
from typing import Callable, List, Optional, Tuple

from . import settings as cfg

LogFn = Callable[[str, str], None]   # (level, message)

# 基础镜像：Ubuntu 22.04 与构建容器一致，apt 源正常、无 SteamOS 限制
BASE_IMAGE = "docker.io/library/ubuntu:22.04"

# UxPlay 目标版本。Ubuntu 22.04 仓库里的 uxplay 只有 1.46（2022 年版）：
#   * 选项集很旧（-fs / -vsync / -reset 等在 1.46 里都没有，我们为此踩了一串
#     「unknown option … stopping」→ mDNS 不广播 → iPhone 搜不到设备的坑）；
#   * 含未修复的安全问题——直到 1.73.7 才修掉 lib/raop_handlers.h 的栈缓冲溢出 +
#     空指针解引用（安全公告 GHSA-479c-ww7g-wgp8），并适配 iOS 27 的 TEARDOWN 变更。
# 因此不再用 apt 的 uxplay，改为在容器内**从源码编译**该版本（见 install_sh 第 2.5 步）。
UXPLAY_VERSION = "1.73.7"

# 源码下载地址（按序尝试）。GitHub 在部分网络下不可达，用户可手动放包走离线分支。
UXPLAY_SRC_URLS = [
    f"https://github.com/FDH2/UxPlay/archive/refs/tags/v{UXPLAY_VERSION}.tar.gz",
    f"https://codeload.github.com/FDH2/UxPlay/tar.gz/refs/tags/v{UXPLAY_VERSION}",
    f"https://api.github.com/repos/FDH2/UxPlay/tarball/v{UXPLAY_VERSION}",
]

# 容器内要装的包：编译 UxPlay 的工具链/开发库 + GStreamer 全套运行时插件
# （base/good/bad 负责 h264 解码、libav 即 ffmpeg 负责 aac 音频解码、vaapi 可选硬解）
APT_PACKAGES = [
    # --- 编译 UxPlay 所需（官方 README「Building UxPlay on Linux」一节）---
    "build-essential", "cmake", "pkg-config",
    "libssl-dev", "libplist-dev", "libavahi-compat-libdnssd-dev",
    "libgstreamer1.0-dev", "libgstreamer-plugins-base1.0-dev",
    "libx11-dev",          # 装了才会编译出 X11 支持，-fs 全屏才可用（1.59+ 行为）
    # --- 下载 / 解压源码 ---
    "curl", "ca-certificates", "xz-utils",
    # --- GStreamer 运行时插件 ---
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
    "gstreamer1.0-plugins-ugly",
    "gstreamer1.0-libav",
    "gstreamer1.0-vaapi",
    # ★ 关键：ximagesink（libgstximagesink.so）在独立包 gstreamer1.0-x 里，
    #   不在 plugins-base！漏装会导致 uxplay 报 no element "ximagesink" 后崩溃。
    "gstreamer1.0-x",
    "gstreamer1.0-tools",  # 提供 gst-inspect-1.0，用于校验 ximagesink 渲染后端
    # --- mDNS ---
    "avahi-utils",
]


def _uxplay_build_sh(ver: str, urls: List[str]) -> str:
    """生成「下载并源码编译 UxPlay」的 bash 片段（拼进安装脚本）。

    * 优先用宿主家目录手动放置的 tar.gz（`~/uxplay-<ver>.tar.gz`）——GitHub 不可达时的
      离线兜底，也方便用户自行选择下载渠道（distrobox 把 /home/deck 挂进容器）。
    * 否则依次尝试若干官方下载地址。
    * 装到 /usr/local/bin/uxplay（make install 默认前缀 /usr/local），PATH 优先于
      /usr/bin 下可能存在的旧版 apt uxplay。
    """
    urls_lines = ("for _u in \\\n"
                  + "".join(f'  "{u}" \\\n' for u in urls[:-1])
                  + f'  "{urls[-1]}"; do\n')
    manual = f"/home/deck/uxplay-{ver}.tar.gz"
    return (
        f"# 2.5) 从源码编译 UxPlay {ver}（jammy 仓库只有 1.46，太旧且含安全漏洞）\n"
        f"if [ -s \"{manual}\" ]; then\n"
        f"  echo \"[install] 使用手动放置的 {manual}\"\n"
        f"  cp \"{manual}\" /tmp/uxplay.tar.gz\n"
        f"else\n"
        f"  echo \"[install] 下载 UxPlay {ver} 源码…\"\n"
        f"  rm -f /tmp/uxplay.tar.gz\n"
        + urls_lines +
        f"    echo \"[install] 尝试：$_u\"\n"
        f"    if curl -fL --retry 3 --retry-delay 3 --connect-timeout 25 -o /tmp/uxplay.tar.gz \"$_u\"; then break; fi\n"
        f"    echo \"[install] 该地址失败，换下一个…\"\n"
        f"  done\n"
        f"fi\n"
        f"if [ ! -s /tmp/uxplay.tar.gz ]; then\n"
        f"  echo \"[install] 错误：UxPlay 源码下载失败（GitHub 可能不可达）。\"\n"
        f"  echo \"[install] 可手动下载 v{ver} 的 tar.gz，放到宿主 ~/uxplay-{ver}.tar.gz 后重试。\"\n"
        f"  exit 11\n"
        f"fi\n"
        f"rm -rf /tmp/uxplay-src && mkdir -p /tmp/uxplay-src\n"
        f"if ! tar -xzf /tmp/uxplay.tar.gz -C /tmp/uxplay-src --strip-components=1; then\n"
        f"  echo \"[install] 错误：源码包解压失败（文件可能不完整）\"; exit 11; fi\n"
        f"cd /tmp/uxplay-src && mkdir -p build && cd build\n"
        f"echo \"[install] cmake 配置…\"\n"
        f"if ! cmake .. >/tmp/uxplay-cmake.log 2>&1; then\n"
        f"  echo \"[install] cmake 失败：\"; tail -30 /tmp/uxplay-cmake.log; exit 12; fi\n"
        f"echo \"[install] 编译中（约 1–3 分钟）…\"\n"
        f"if ! make -j$(nproc) >/tmp/uxplay-make.log 2>&1; then\n"
        f"  echo \"[install] 编译失败：\"; tail -40 /tmp/uxplay-make.log; exit 13; fi\n"
        f"if ! make install >/tmp/uxplay-install.log 2>&1; then\n"
        f"  echo \"[install] make install 失败：\"; tail -30 /tmp/uxplay-install.log; exit 14; fi\n"
        f"# 清掉可能存在的旧版 apt uxplay，避免与新编译版本混淆\n"
        f"if [ -x /usr/bin/uxplay ] || [ -x /usr/sbin/uxplay ]; then\n"
        f"  DEBIAN_FRONTEND=noninteractive apt-get remove -y uxplay >/dev/null 2>&1 || true\n"
        f"fi\n"
        f"ldconfig\n"
        f"cd /\n"
        f"echo \"[install] UxPlay 就绪：$(command -v uxplay)  版本=$(timeout 5 uxplay -v 2>&1 | head -1)\"\n"
    )


def distrobox_available() -> bool:
    return shutil.which("distrobox") is not None


def podman_available() -> bool:
    return shutil.which("podman") is not None


def container_exists(container: str) -> bool:
    if podman_available():
        try:
            r = subprocess.run(
                ["podman", "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                names = [l.strip() for l in r.stdout.splitlines() if l.strip()]
                return container in names
        except Exception:
            pass
    if distrobox_available():
        try:
            r = subprocess.run(
                ["distrobox", "list", "--name"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                names = [l.strip() for l in r.stdout.splitlines() if l.strip()]
                return container in names
        except Exception:
            pass
    return False


def uxplay_installed(container: str) -> bool:
    """容器内是否已有 uxplay 可执行文件。

    用 podman exec 探测：rootless 容器免授权，游戏模式/桌面模式都能用，
    不触发 distrobox 的图形导出弹窗。
    """
    if not podman_available():
        return False
    try:
        r = subprocess.run(
            ["podman", "exec", container, "bash", "-lc",
             "if command -v uxplay >/dev/null 2>&1; then command -v uxplay; "
             "elif [ -x /usr/local/bin/uxplay ]; then echo /usr/local/bin/uxplay; "
             "else exit 1; fi"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0 and "uxplay" in (r.stdout or "")
    except Exception:
        return False


def uxplay_version(container: str) -> str:
    """取容器内 uxplay 的版本行（取不到返回 ""）。UxPlay 1.73.7 支持 `-v` 显示版本。"""
    if not podman_available():
        return ""
    try:
        r = subprocess.run(
            ["podman", "exec", container, "bash", "-lc",
             "command -v uxplay >/dev/null 2>&1 || [ -x /usr/local/bin/uxplay ] || exit 1; "
             "timeout 5 uxplay -v 2>&1 | head -1"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except Exception:
        pass
    return ""


def uxplay_ready(container: str) -> bool:
    """容器内 uxplay 是否可用**且为目标版本** UXPLAY_VERSION。

    为什么要查版本：旧容器里可能已有 apt 装的 UxPlay 1.46（/usr/bin/uxplay），
    若只判断「有没有 uxplay」，点「安装/重建运行环境」会被判「已就绪」直接跳过，
    永远升不到 1.73.7。这里要求版本匹配，才能触发重新编译。
    取不到版本号时退回「存在即就绪」，避免因 `-v` 输出格式差异导致每次都重装。
    """
    if not uxplay_installed(container):
        return False
    v = uxplay_version(container)
    if not v:
        return True
    return UXPLAY_VERSION in v


def video_sink_ready(container: str) -> bool:
    """容器内是否存在 ximagesink 渲染后端（uxplay 出画面必需）。

    ★ 关键：ximagesink **不在** gstreamer1.0-plugins-base 里，而由独立包
    ``gstreamer1.0-x`` 提供（libgstximagesink.so）。旧版安装清单漏装该包，
    结果 uxplay 编译/启动都正常，一投屏就 ``no element "ximagesink"`` → 渲染器崩溃
    （现象：搜得到、连得上、有声音、没画面）。故「环境是否就绪」必须连带查它，
    否则点「安装/重建运行环境」会被判「已就绪」直接跳过、永远修不好。

    探测失败（podman 不可用 / 容器未跑 / 异常）时返回 True——不能确定时不要凭空
    判定「未就绪」，以免每次启动都弹安装引导。
    """
    if not podman_available():
        return True
    try:
        r = subprocess.run(
            ["podman", "exec", container, "bash", "-lc",
             "command -v gst-inspect-1.0 >/dev/null 2>&1 && gst-inspect-1.0 ximagesink >/dev/null 2>&1"],
            capture_output=True, text=True, timeout=25,
        )
        return r.returncode == 0
    except Exception:
        return True


def is_ready(container: str) -> bool:
    """运行环境是否就绪：容器存在、uxplay 为目标版本、且 ximagesink 渲染后端可用。"""
    return (container_exists(container)
            and uxplay_ready(container)
            and video_sink_ready(container))


def _container_id(container: str) -> Optional[str]:
    if not podman_available():
        return None
    try:
        r = subprocess.run(
            ["podman", "ps", "-a", "--filter", f"name=^{container}$",
             "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except Exception:
        pass
    return None


def _container_image_id(container: str) -> Optional[str]:
    """取出容器当前使用的镜像 id（删除容器前先取，删后就查不到了）。"""
    if not podman_available():
        return None
    try:
        r = subprocess.run(
            ["podman", "inspect", "-f", "{{.Image}}", container],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except Exception:
        pass
    return None


def _image_used_elsewhere(image_id: str, except_container: str) -> bool:
    """该镜像是否还被其它容器引用；不确定时保守返回 True（不删）。"""
    if not image_id or not podman_available():
        return True
    try:
        r = subprocess.run(
            ["podman", "ps", "-a", "--format", "{{.Names}} {{.Image}}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return True
        for ln in r.stdout.splitlines():
            parts = ln.split(None, 1)
            if len(parts) < 2:
                continue
            name, img = parts[0].strip(), parts[1].strip()
            if name == except_container:
                continue
            if img == image_id:
                return True
        return False
    except Exception:
        return True


def _container_usable(container: str) -> bool:
    """容器是否处于健康可 exec 状态。

    先尝试启动（若已停），再 `podman exec <c> true`；失败（含卡死/坏 conmon）返回 False，
    调用方应强制删掉重建。无 podman 时无法判断，返回 True 交给后续步骤报错。
    """
    if not podman_available():
        return True
    try:
        subprocess.run(["podman", "start", container], capture_output=True, timeout=30)
        r = subprocess.run(
            ["podman", "exec", container, "true"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _kill_conmon(container: str) -> None:
    """杀掉该容器残留的 conmon 进程（解决 'conmon exited prematurely' 导致删不掉）。"""
    try:
        r = subprocess.run(
            ["pgrep", "-f", f"conmon.*{container}"],
            capture_output=True, text=True, timeout=10,
        )
        for pid in [p for p in r.stdout.split() if p.strip().isdigit()]:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except Exception:
                pass
    except Exception:
        pass
    # 也按容器短 id 兜底杀一次
    cid = _container_id(container)
    if cid:
        try:
            r = subprocess.run(
                ["pgrep", "-f", f"conmon.*{cid}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid in [p for p in r.stdout.split() if p.strip().isdigit()]:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except Exception:
                    pass
        except Exception:
            pass


def _force_remove_container(container: str, log_fn: LogFn) -> bool:
    """多策略强制删除容器，尽量清掉卡在坏状态的容器。

    返回是否删除成功。
    """
    for cmd in (["podman", "rm", "-f", container],
                ["podman", "container", "rm", "-f", container]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return True
            err = ((r.stderr or r.stdout) or "").strip().splitlines()[-1] \
                if (r.stderr or r.stdout) else ""
        except Exception as e:
            err = str(e)
        log_fn("warn", f"{' '.join(cmd)} 失败：{err}")
        _kill_conmon(container)
    # 回退：distrobox rm
    try:
        r = subprocess.run(
            ["distrobox", "rm", "-f", container],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


def _stream(cmd: list[str], log_fn: LogFn, timeout_total: float = 900.0) -> Tuple[int, str]:
    """运行命令并逐行回调 log_fn(level, line)。返回 (returncode, 末尾错误信息)。"""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        log_fn("error", f"找不到命令: {e}")
        return -1, str(e)
    assert proc.stdout is not None
    last_err = ""
    t0 = time.time()
    for line in proc.stdout:
        if time.time() - t0 > timeout_total:
            try:
                proc.kill()
            except Exception:
                pass
            log_fn("error", "操作超时，已中止")
            return -2, "timeout"
        line = line.rstrip("\n")
        if not line:
            continue
        low = line.lower()
        if any(k in low for k in ("error", "e:", "err:", "fatal", "failed", "cannot")):
            last_err = line
            log_fn("error", line)
        elif any(k in low for k in ("warning", "warn")):
            log_fn("warn", line)
        else:
            log_fn("info", line)
    rc = proc.wait()
    return rc, last_err


def _repair_sink(container: str, log_fn: LogFn) -> Tuple[bool, str]:
    """轻量修复：只补 GStreamer 渲染后端（`gstreamer1.0-x`）+ 清注册表缓存。

    用于「uxplay 版本已正确、但容器里缺 ximagesink」的情形——不需要重新下载源码
    编译（那要 1–3 分钟 + 联网），一次 apt 补包即可，几秒钟。
    返回 (成功, 摘要)。
    """
    sh = (
        "#!/bin/bash\n"
        "# 抢回可能被孤儿 apt 抱住的 /var/cache/apt/archives/lock（同 install_sh 的 0) 步）\n"
        "for _d in /proc/[0-9]*; do\n"
        "  [ -r \"$_d/cmdline\" ] || continue\n"
        "  _c=$(tr '\\0' ' ' < \"$_d/cmdline\" 2>/dev/null) || continue\n"
        "  _p=${_d#/proc/}; [ \"$_p\" = \"$$\" ] && continue\n"
        "  case \"${_c%% *}\" in\n"
        "    */apt-get|apt-get|*/apt|apt|*/dpkg|dpkg|*/dpkg-query|dpkg-query)\n"
        "      echo \"[repair] 清理残留 apt/dpkg 进程 $_p\"; kill -9 \"$_p\" 2>/dev/null ;;\n"
        "  esac\n"
        "done\n"
        "sleep 1\n"
        "dpkg --configure -a >/dev/null 2>&1 || true\n"
        "apt-get -o DPkg::Lock::Timeout=600 update || true\n"
        "# ★ ximagesink 由独立包 gstreamer1.0-x 提供（不在 plugins-base）\n"
        "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y "
        "gstreamer1.0-x gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-tools || true\n"
        "# 清 GStreamer 注册表缓存：uxplay 经 podman 以宿主 /home/deck 运行，缓存落在那里\n"
        "rm -rf /home/*/.cache/gstreamer-1.0 /root/.cache/gstreamer-1.0\n"
        "command -v gst-inspect-1.0 >/dev/null 2>&1 && gst-inspect-1.0 ximagesink >/dev/null 2>&1\n"
    )
    if podman_available():
        try:
            subprocess.run(["podman", "start", container], capture_output=True, timeout=30)
        except Exception:
            pass
        cmd = ["podman", "exec", "-u", "0", container, "bash", "-lc", sh]
    else:
        cmd = ["distrobox", "enter", container, "--", "sudo", "bash", "-lc", sh]
    rc, err = _stream(cmd, log_fn, timeout_total=900.0)
    if rc != 0:
        return False, err or f"rc={rc}"
    return True, "ximagesink 渲染后端已修复"


def create_and_install(container: str, log_fn: LogFn) -> Tuple[bool, str]:
    """创建容器并安装 uxplay + GStreamer 全套。返回 (成功, 摘要)。"""
    if not distrobox_available() and not podman_available():
        return False, "未找到 distrobox / podman，无法自动创建容器。请先安装 distrobox。"

    log_fn("info",
           f"开始创建容器 {container}（镜像 {BASE_IMAGE}；首次需联网拉取，约 400–800 MB）…")

    # 1) 容器不存在 → 创建；存在但不可用（卡死）→ 强制删掉重建
    if not container_exists(container):
        log_fn("info", f"容器 {container} 不存在，开始创建")
    else:
        if _container_usable(container) and uxplay_ready(container) and video_sink_ready(container):
            log_fn("info", f"容器 {container} 已存在，uxplay {UXPLAY_VERSION} 就绪，跳过")
            return True, f"运行环境已就绪（容器 {container}）"
        if _container_usable(container) and uxplay_ready(container):
            # uxplay 版本没问题，只是缺渲染后端（多为旧版清单漏装 gstreamer1.0-x）
            # → 走轻量修复，不重新下载/编译 uxplay。
            log_fn("info", "uxplay 已就绪但 ximagesink 渲染后端缺失，执行轻量修复（补 gstreamer1.0-x + 清缓存）…")
            ok, msg = _repair_sink(container, log_fn)
            if ok and video_sink_ready(container):
                log_fn("info", "渲染后端已修复，无需重新编译 uxplay")
                return True, f"运行环境已就绪（容器 {container}）"
            log_fn("warn", f"轻量修复未成功（{msg}），改为完整重装 uxplay…")
        if _container_usable(container):
            log_fn("info", f"容器 {container} 已存在但 uxplay/sink 未就绪，直接补装/升级…")
        else:
            log_fn("warn", f"容器 {container} 处于异常状态，强制删除后重建…")
            _force_remove_container(container, log_fn)

    if not container_exists(container):
        create_cmd = (["distrobox", "create", "-i", BASE_IMAGE, "-n", container, "--yes"]
                      if distrobox_available()
                      else ["podman", "create", "--name", container,
                            BASE_IMAGE, "sleep", "infinity"])
        rc, err = _stream(create_cmd, log_fn)
        if rc != 0:
            return False, f"创建容器失败: {err or 'rc=' + str(rc)}"

    # 2) 安装软件（必须用 root 跑 apt，否则 Permission denied）
    log_fn("info", "在容器内安装 uxplay 与 GStreamer 插件（apt，可能需要几分钟）…")
    # apt 有**三把**不同的锁，踩过两把：
    #   a) /var/lib/apt/lists/lock、/var/lib/dpkg/lock-frontend —— distrobox create 初始化
    #      时自己跑 apt-get update/upgrade 会与我们抢。可用 `DPkg::Lock::Timeout=600`
    #      让 apt 自己排队等（最可靠，不依赖任何外部命令）。
    #   b) /var/cache/apt/archives/lock（下载缓存锁）—— **DPkg::Lock::Timeout 管不到它**。
    #      上一轮安装被中断/超时后，容器里的 apt-get 会变成孤儿一直抱着这把锁，之后
    #      每次安装都立刻 "E: Could not get lock /var/cache/apt/archives/lock" 失败
    #      （9/9 23:42 日志实证：8 次重试全部秒失败）。
    #   对策：开装前遍历 /proc 找出仍在跑的 apt/dpkg（裸 ubuntu 镜像没有 procps/pgrep，
    #   所以不用 pgrep），先等 60s 看它是否自己结束，仍活着就判定为孤儿强杀，
    #   再 `dpkg --configure -a` 修复可能被打断的半装状态。
    #   **绝不 rm 锁文件**：Debian 官方明确反对，删了反而可能让两个 apt 并发损坏 dpkg 库。
    pkgs = " ".join(APT_PACKAGES)
    install_sh = (
        "#!/bin/bash\n"
        "# 0) 清理上一轮被中断而卡死的 apt/dpkg，抢回 /var/cache/apt/archives/lock\n"
        "_stale_apt() {\n"
        "  local _d _c _p\n"
        "  for _d in /proc/[0-9]*; do\n"
        "    [ -r \"$_d/cmdline\" ] || continue\n"
        "    _c=$(tr '\\0' ' ' < \"$_d/cmdline\" 2>/dev/null) || continue\n"
        "    _p=${_d#/proc/}\n"
        "    [ \"$_p\" = \"$$\" ] && continue\n"
        "    case \"${_c%% *}\" in\n"
        "      */apt-get|apt-get|*/apt|apt|*/dpkg|dpkg|*/dpkg-query|dpkg-query|*/unattended-upgrades|unattended-upgrades) echo \"$_p\" ;;\n"
        "    esac\n"
        "  done\n"
        "}\n"
        "_stale=$(_stale_apt)\n"
        "if [ -n \"$_stale\" ]; then\n"
        "  echo \"[install] 检测到 apt/dpkg 正在运行（pid: $_stale），最多等 60s 让它自己结束…\"\n"
        "  for _i in $(seq 1 60); do\n"
        "    sleep 1\n"
        "    _stale=$(_stale_apt)\n"
        "    [ -z \"$_stale\" ] && break\n"
        "  done\n"
        "fi\n"
        "if [ -n \"$_stale\" ]; then\n"
        "  echo \"[install] 判定为上一轮残留的孤儿进程，强制清理（pid: $_stale）\"\n"
        "  for _p in $_stale; do kill -9 \"$_p\" 2>/dev/null; done\n"
        "  sleep 2\n"
        "  dpkg --configure -a >/dev/null 2>&1 || true\n"
        "fi\n"
        "for _a in 1 2 3 4 5 6 7 8; do\n"
        "  if apt-get -o DPkg::Lock::Timeout=600 update"
        " && DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600"
        " install -y --no-install-recommends "
        + pkgs
        + "; then break; fi\n"
        "  sleep 10\n"
        "done\n"
        + _uxplay_build_sh(UXPLAY_VERSION, UXPLAY_SRC_URLS)
        + "# 校验渲染后端 ximagesink（缺失会触发 video_renderer_init 的 renderer->sink 断言崩溃）\n"
        "# 关键：若此前 apt 锁中断导致 plugins-base 装坏，--reinstall 只修复系统 .so，\n"
        "# 但 GStreamer 注册表缓存（~/.cache/gstreamer-1.0/registry.x86_64.bin）仍是旧的\n"
        "# 「插件不可用」记录，uxplay 读到过期缓存会继续断言崩溃。必须清掉缓存让它重新探测。\n"
        "# uxplay 运行时 HOME 取宿主 /home/deck（distrobox 挂载同样的家目录），缓存也在那，\n"
        "# 而本步以 -u 0 跑、HOME=/root，故两个目录都要清，否则只清一个仍可能读到旧缓存。\n"
        "_verify_sink() { command -v gst-inspect-1.0 >/dev/null 2>&1 && gst-inspect-1.0 ximagesink >/dev/null 2>&1; }\n"
        "if ! _verify_sink; then\n"
        "  echo \"[install] ximagesink 渲染后端缺失，尝试修复（补装 gstreamer1.0-x + 重装插件 + 清注册表缓存）…\"\n"
        "  apt-get -o DPkg::Lock::Timeout=600 install -y -f >/dev/null 2>&1 || true\n"
        # ximagesink 由独立包 gstreamer1.0-x 提供（不在 plugins-base），必须先确保它装上
        "  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y gstreamer1.0-x >/dev/null 2>&1 || true\n"
        "  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y --reinstall gstreamer1.0-x gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-tools >/dev/null 2>&1 || true\n"
        "  rm -rf /home/*/.cache/gstreamer-1.0 /root/.cache/gstreamer-1.0 ~/.cache/gstreamer-1.0\n"
        "fi\n"
        "if _verify_sink; then echo \"[install] ximagesink 校验通过\"; else echo \"[install] 警告：ximagesink 仍不可用，UxPlay 启动可能崩溃\"; fi\n"
    )
    if podman_available():
        # 确保容器在跑，并以容器内 root(-u 0) 安装，无需 sudo/密码
        try:
            subprocess.run(["podman", "start", container],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        inst_cmd = ["podman", "exec", "-u", "0", container, "bash", "-lc", install_sh]
    else:
        inst_cmd = ["distrobox", "enter", container, "--", "sudo", "bash", "-lc", install_sh]
    rc, err = _stream(inst_cmd, log_fn, timeout_total=1800.0)
    if rc != 0:
        return False, f"安装软件失败: {err or 'rc=' + str(rc)}"

    # 3) 校验
    if not uxplay_installed(container):
        return False, "安装完成但容器内找不到 uxplay，请检查日志"
    log_fn("info", f"uxplay 已就绪：容器 {container}")
    # ximagesink 渲染后端缺失会导致 UxPlay 启动即断言崩溃，必须一并确认
    try:
        r = subprocess.run(
            ["podman", "exec", container, "bash", "-lc",
             "command -v gst-inspect-1.0 >/dev/null 2>&1 && gst-inspect-1.0 ximagesink >/dev/null 2>&1"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return False, ("容器已装好 uxplay，但 ximagesink 渲染后端不可用（UxPlay 启动会"
                          "video_renderer_init 断言崩溃）。ximagesink 由独立包 gstreamer1.0-x 提供，"
                          "请在构建环境执行：podman exec -u 0 %s bash -lc "
                          "\"apt-get update && apt-get install -y gstreamer1.0-x gstreamer1.0-plugins-base "
                          "gstreamer1.0-plugins-good gstreamer1.0-tools && rm -rf /home/*/.cache/gstreamer-1.0 "
                          "/root/.cache/gstreamer-1.0 && gst-inspect-1.0 ximagesink\"" % container)
    except Exception:
        pass  # 校验失败不致命，交给用户运行日志暴露
    return True, f"运行环境已安装到容器 {container}"


def uninstall(
    container: str,
    remove_image: bool = True,
    remove_appdata: bool = True,
    log_fn: Optional[LogFn] = None,
) -> Tuple[bool, str]:
    """一键清爽卸载：删容器 + 镜像（若不被其它容器占用）+ 应用设置与日志。

    返回 (成功, 摘要)。AppImage 自身不会被删除（进程正在跑，且删除自己无意义），
    用户可手动删除文件与 Steam「非 Steam 游戏」条目。
    """
    log_fn = log_fn or (lambda *a, **k: None)
    removed: list[str] = []

    # 先取镜像 id（删容器后就查不到了），并判断是否被其它容器共享
    img = _container_image_id(container)

    # 1) 删除容器（多策略强制删除，处理卡死状态）
    if container_exists(container):
        log_fn("info", f"删除容器 {container}…")
        if _force_remove_container(container, log_fn):
            removed.append("容器")
        else:
            cid = _container_id(container) or container
            log_fn("warn", "容器删除失败（可能处于异常状态）。可手动执行："
                          f"podman rm -f {cid}")
    else:
        log_fn("info", f"容器 {container} 不存在，跳过")

    # 2) 删除镜像（若该镜像不被其它容器引用）
    if remove_image:
        if img and not _image_used_elsewhere(img, container):
            log_fn("info", f"删除镜像 {img}…")
            try:
                r = subprocess.run(
                    ["podman", "rmi", "-f", img],
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode == 0:
                    removed.append("镜像")
                else:
                    tail = ""
                    if r.stderr:
                        tail = r.stderr.strip().splitlines()[-1]
                    log_fn("warn", f"删除镜像失败（可能仍被占用）：{tail}")
            except Exception as e:
                log_fn("warn", f"删除镜像失败: {e}")
        elif img:
            log_fn("info", "镜像仍被其它容器使用，已保留（不影响本 App 卸载）")
        else:
            log_fn("info", "无法确定镜像，跳过删除")

    # 3) 删除应用数据（设置 + 运行日志）
    if remove_appdata:
        for d in (cfg.CONFIG_DIR, cfg.LOG_DIR):
            try:
                if d.exists():
                    import shutil as _sh
                    _sh.rmtree(d)
                    removed.append(f"数据({d.name})")
                    log_fn("info", f"已删除 {d}")
            except Exception as e:
                log_fn("warn", f"删除 {d} 失败: {e}")

    if not removed:
        return True, "无可清理项"
    return True, "已清理：" + "、".join(removed)
