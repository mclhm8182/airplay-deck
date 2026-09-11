"""首次启动自动安装 / 一键清爽卸载 uxplay 运行环境（distrobox 容器）。

方案 B：AppImage 本身不含 uxplay。新用户首次打开 App 时，自动拉取并构建
`uxplay-env` 容器，容器里装好 uxplay + GStreamer 全套插件，之后即可投屏。
卸载时清掉容器 + 镜像（若不被其它容器占用）+ 应用设置与日志，清爽如初。

所有命令走 rootless 的 distrobox / podman，落在用户家目录，**不需要 sudo**，
也绕开 SteamOS 只读根。镜像拉取与 apt 安装耗时较长，调用方应在后台线程里跑，
并通过 log_fn 逐行回显进度。
"""

import shutil
import subprocess
import time
from typing import Callable, Optional, Tuple

from . import settings as cfg

LogFn = Callable[[str, str], None]   # (level, message)

# 基础镜像：Ubuntu 22.04 与构建容器一致，apt 源正常、无 SteamOS 限制
BASE_IMAGE = "docker.io/library/ubuntu:22.04"

# 容器内要装的包：uxplay 本体 + GStreamer 全套
# （base/good/bad 负责 h264 解码、libav 即 ffmpeg 负责 aac 音频解码、vaapi 可选硬解）
APT_PACKAGES = [
    "uxplay",
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
    "gstreamer1.0-plugins-ugly",
    "gstreamer1.0-libav",
    "gstreamer1.0-vaapi",
    "libavahi-compat-libdnssd-dev",
    "avahi-utils",
    "pkg-config",
]


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
            ["podman", "exec", container, "bash", "-lc", "command -v uxplay"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0 and "uxplay" in (r.stdout or "")
    except Exception:
        return False


def is_ready(container: str) -> bool:
    """运行环境是否就绪：容器存在且里面已装 uxplay。"""
    return container_exists(container) and uxplay_installed(container)


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


def create_and_install(container: str, log_fn: LogFn) -> Tuple[bool, str]:
    """创建容器并安装 uxplay + GStreamer 全套。返回 (成功, 摘要)。"""
    if not distrobox_available() and not podman_available():
        return False, "未找到 distrobox / podman，无法自动创建容器。请先安装 distrobox。"

    log_fn("info",
           f"开始创建容器 {container}（镜像 {BASE_IMAGE}；首次需联网拉取，约 400–800 MB）…")

    # 1) 创建容器（用 distrobox 保证与运行时进入方式一致；无 distrobox 时退化为 podman）
    created_via_podman = False
    if not container_exists(container):
        if distrobox_available():
            rc, err = _stream(
                ["distrobox", "create", "-i", BASE_IMAGE, "-n", container, "--yes"],
                log_fn,
            )
        else:
            created_via_podman = True
            rc, err = _stream(
                ["podman", "create", "--name", container, BASE_IMAGE, "sleep", "infinity"],
                log_fn,
            )
        if rc != 0:
            return False, f"创建容器失败: {err or 'rc=' + str(rc)}"
    else:
        log_fn("info", f"容器 {container} 已存在，跳过创建")

    # 2) 安装软件（distrobox enter 的首屏导出只针对图形应用，apt 安装不会弹授权）
    log_fn("info", "在容器内安装 uxplay 与 GStreamer 插件（apt，可能需要一两分钟）…")
    install_sh = (
        "apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
        + " ".join(APT_PACKAGES)
    )
    if distrobox_available() and not created_via_podman:
        inst_cmd = ["distrobox", "enter", container, "--", "bash", "-lc", install_sh]
    else:
        inst_cmd = ["podman", "exec", container, "bash", "-lc", install_sh]
    rc, err = _stream(inst_cmd, log_fn, timeout_total=1200.0)
    if rc != 0:
        return False, f"安装软件失败: {err or 'rc=' + str(rc)}"

    # 3) 校验
    if uxplay_installed(container):
        log_fn("info", f"uxplay 已就绪：容器 {container}")
        return True, f"运行环境已安装到容器 {container}"
    return False, "安装完成但容器内找不到 uxplay，请检查日志"


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

    # 1) 删除容器
    if container_exists(container):
        log_fn("info", f"删除容器 {container}…")
        cmd = ["distrobox", "rm", "-f", container] if distrobox_available() \
            else ["podman", "rm", "-f", container]
        rc, err = _stream(cmd, log_fn)
        if rc != 0:
            log_fn("warn", f"删除容器返回非 0（可能已不存在）：{err}")
        removed.append("容器")
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
