"""设置持久化：读取 / 保存 / 迁移。

配置目录遵循 XDG：~/.config/airplay-deck/settings.json
日志目录：~/.local/state/airplay-deck/airplay-deck.log
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

APP_NAME = "airplay-deck"

_xdg_config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
_xdg_state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")

CONFIG_DIR = Path(_xdg_config) / APP_NAME
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LOG_DIR = Path(_xdg_state) / APP_NAME
LOG_FILE = LOG_DIR / "airplay-deck.log"

# 与上游 Decky 插件保持一致的默认值：30fps / ximagesink 最稳
DEFAULT_SETTINGS: Dict[str, Any] = {
    "device_name": "SteamDeck",
    "append_hostname": False,
    "fps": 30,
    "decoder": "auto",           # auto | sw | vaapi | v4l2
    "resolution": "auto",        # auto | 1920x1080 | 1280x800 | ...（帧率由 fps 字段决定）
    "display_mode": "window",  # auto | fullscreen | window（默认窗口：靠 -s 1280x800 铺满屏幕不裁剪；UxPlay 1.46 无 -fs，三模式画面表现一致，区别仅在 App 是否接管屏幕）
    "rotate": "none",            # none | R | L
    "flip": "none",              # none | H | V | I
    "legacy_ports": False,
    "keep_window": False,
    "video_sink": "ximagesink",  # 纯 X11，不依赖 GL；Xwayland/gamescope 下最可靠
    "audio_sink": "auto",
    # 音频同步：auto = 交给 uxplay 默认的基于时间戳的 A/V 同步（看电影最稳，但刷短视频时
    # 每个新视频开头声音会晚几秒才跟上）；off = 传 -vsync no，改走「直播式」即时出声、不丢帧、
    # 不再为对齐视频而延迟音频——刷小红书/玩游戏/投屏浏览时的卡顿感消失。默认 off 直接修掉
    # Round-2 反馈的「下一个视频声音跟不上」问题；想看电影追求严丝合缝可改回 auto。
    "audio_sync": "off",        # auto | off
    "anti_sleep": True,
    "extra": "",                 # 追加到命令行的原始参数
    "uxplay_path": "",           # 留空 => 用 PATH 里的 uxplay
    "use_distrobox": True,       # True => 通过容器跑 uxplay（方案 B：首启自动建 uxplay-env 容器）
    "env_bootstrap_seen": False,  # 首启自动安装引导是否已弹过（避免每次启动都弹）
    "distrobox_container": "uxplay-env",  # 容器名（与 Decky 插件默认一致）
    # 进入容器的方式：
    #   "enter"  -> distrobox enter <c> -- uxplay ...（首次/部分环境需 polkit 授权）
    #   "podman" -> podman start <c> + podman exec ...（rootless 容器无需任何授权，
    #               可绕开游戏模式下无法弹出授权对话框导致 uxplay 起不来的问题）
    "distrobox_method": "enter",
    "autostart": False,
    "language": "auto",          # auto（跟随系统）| zh_CN | zh_TW | en | ja
    # 界面主题：auto（跟随系统深浅色）| dark | light
    "theme": "auto",
}

VIDEO_SINKS = ["ximagesink", "xvimagesink", "autovideosink", "glimagesink", "vaapisink"]
# 帧率只给 30 / 60 两档：自由填值（如 24/50/120）会让部分 App 的
# AirPlay 会话卡死（B站/相册卡住、小红书正常就是典型症状），固定档位最稳。
FPS_CHOICES = [30, 60]
# 帧率由独立的「帧率 (fps)」选项决定，渲染分辨率下拉只放纯分辨率字符串。
# auto = 跟随投屏设备的原生分辨率（不缩放、不裁剪）。
# 固定尺寸时 videoscale 默认「填满并裁掉」，比例不一致会切掉画面上下或左右，
# 所以只在需要限制窗口大小时才显式选一个。
RESOLUTION_CHOICES = [
    "auto", "1920x1080", "1600x900", "1280x800",
    "1280x720", "1024x768", "854x480",
]
DECODER_CHOICES = ["auto", "sw", "vaapi", "v4l2"]
DISPLAY_MODE_CHOICES = ["auto", "fullscreen", "window"]
# 界面主题：auto = 跟随系统深浅色（Qt 的 colorScheme）| dark | light 为手动锁定
THEME_CHOICES = ["auto", "dark", "light"]


def load_settings() -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        if SETTINGS_FILE.exists():
            loaded = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        data = {}

    merged = dict(DEFAULT_SETTINGS)
    merged.update(_migrate(data))
    return merged


def _migrate(data: Dict[str, Any]) -> Dict[str, Any]:
    """把历史上会导致黑屏/花屏的组合强制纠偏（复用插件版的教训）。"""
    out = dict(data)
    # 清理已废弃的旧键：UxPlay 1.46 移除了 -vsync/-reset 选项，留着会误导
    for obsolete in ("reset", "vsync"):
        out.pop(obsolete, None)
    # 视频后端若为 auto/空，改成 ximagesink（避免 Xwayland 下 GL 上下文建不出黑屏）
    vs = out.get("video_sink")
    if vs in (None, "", "auto"):
        out["video_sink"] = "ximagesink"
    # 帧率收档到 30/60：旧版允许 1–120 自由填，异常值会让部分 App 投屏卡死
    try:
        fps = int(out.get("fps") or 30)
    except (TypeError, ValueError):
        fps = 30
    if fps not in FPS_CHOICES:
        out["fps"] = 30 if fps < 45 else 60
    return out


def save_settings(s: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        SETTINGS_FILE.write_text(
            json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        # 设置保存失败不应崩掉 UI；交给调用方日志
        raise RuntimeError(f"保存设置失败: {e}")


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
