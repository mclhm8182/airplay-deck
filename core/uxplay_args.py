"""把设置翻译成 uxplay 命令行参数。

逻辑移植自 Decky 插件的 launch_airplay.sh，但去掉了容器相关部分——独立 App
本身就在用户的图形会话里，DISPLAY/XAUTHORITY 天然正确，直接拼参数跑 uxplay 即可。

关键约定（与插件版保持一致）：
  * resolution=auto 时向客户端请求 1280x800（Deck 原生）流，窗口即铺满 1280×800 屏幕、
    全画面不裁切。
  * 引擎为**自编译的 UxPlay 1.73.7**（见 core/container.py）：它支持 -fs 全屏
    （man page：「-fs Full-screen (only with X11, Wayland, VAAPI, D3D11, kms)」，
    构建时需带 X11 支持）。因此 display_mode 现在会真实产生 -fs：
      window     → 桌面不传 -fs；游戏模式仍强制 -fs（gamescope 下窗口会缩成一小块）
      fullscreen → -fs
      auto       → 游戏模式 -fs；桌面模式不传
    注意：旧 apt 版 1.46 没有任何全屏选项（-fs/-vsync/-reset 都会 unknown option 退出），
    升级引擎后这些都已经可用。
"""

from typing import Any, Dict, List, Optional, Tuple


def _choose_sink(video_sink: str) -> Tuple[Optional[str], Optional[str]]:
    """返回 (传给 -vs 的 sink 名 或 None, ximagesink 下的分辨率兜底 或 None)。"""
    if video_sink == "auto":
        return None, None
    sink = video_sink
    res_override = "1280x800" if sink == "ximagesink" else None
    return sink, res_override


def _resolution_for_s(resolution: str) -> Optional[str]:
    """把 '1280x800@60' 形式的设置剥成 uxplay -s 要的 '1280x800'；auto 返回 None。"""
    if not resolution or resolution == "auto":
        return None
    return resolution.split("@")[0]


def build_args(s: Dict[str, Any], is_gamemode: bool) -> List[str]:
    args: List[str] = []

    device = s.get("device_name") or "SteamDeck"
    args += ["-n", str(device)]
    if not s.get("append_hostname"):
        args += ["-nh"]

    fps = s.get("fps") or 30
    args += ["-fps", str(fps)]

    decoder = s.get("decoder") or "auto"
    if decoder == "sw":
        args += ["-avdec"]
    elif decoder == "vaapi":
        args += ["-vd", "vaapih264dec"]
    elif decoder == "v4l2":
        args += ["-v4l2"]

    sink, res_override = _choose_sink(s.get("video_sink") or "ximagesink")
    res_for_s = _resolution_for_s(s.get("resolution") or "auto")
    # resolution=auto：始终向客户端请求 Deck 原生 1280x800 的流。
    # 依据 UxPlay 源码 renderers/video_renderer.c：uxplay 的 X 窗口尺寸由
    # 「协商到的 AirPlay 流分辨率」决定，而 -s 只是向客户端请求的编码分辨率
    # （高度取请求值，宽度按设备朝向自动推导）。不传 -s 时客户端默认发 1920x1080
    # 或设备原生分辨率，窗口会远超 1280x800 → 在 Deck 上被裁掉（0.4.3 的回归）。
    # 传 -s 1280x800：高度锚定 800（Deck 高），全屏模式下 ximagesink 填满屏幕不再裁切；
    # 显式选了固定分辨率时尊重用户选择（res_for_s 已非空，不覆盖）。
    if res_for_s is None:
        res_for_s = res_override or "1280x800"
    if res_for_s:
        args += ["-s", res_for_s]

    rotate = s.get("rotate") or "none"
    if rotate in ("R", "L"):
        args += ["-r", rotate]

    flip = s.get("flip") or "none"
    if flip in ("H", "V", "I"):
        args += ["-f", flip]

    # display_mode → -fs。UxPlay 1.73.7（自编译版）**有** -fs 全屏选项——man page 原文：
    #   「-fs  Full-screen (only with X11, Wayland, VAAPI, D3D11, kms)」
    # 需构建时带 X11 支持（container.py 里装了 libx11-dev）。语义（用户 2026-09-09 明确）：
    #   window     = 普通可拖动窗口，不传 -fs（靠上方 -s 1280x800 铺满屏幕、全画面不裁切）
    #   fullscreen = 无边框全屏，传 -fs
    #   auto       = 游戏模式无边框全屏(-fs)；桌面模式不传（正常窗口铺满 1280x800 屏幕）
    # Game Mode：始终传 -fs。gamescope 下 window 模式常把画面缩成一小块，不可接受。
    # 桌面模式仍按 display_mode 原有行为。
    # 注：旧的 apt 版 1.46 根本没有 -fs，传了直接 unknown option 退出——这正是升级引擎的原因之一。
    display_mode = s.get("display_mode") or "window"
    if is_gamemode:
        args += ["-fs"]
    elif display_mode == "fullscreen":
        args += ["-fs"]

    if s.get("legacy_ports"):
        args += ["-p"]
    if s.get("keep_window"):
        args += ["-nc"]

    # 注：UxPlay 1.46 已移除 -reset 选项（传了会报 unknown option 直接退出），不再发射

    # 手动指定视频后端时才传 -vs；auto 不传，让 UxPlay 自己选
    if sink:
        args += ["-vs", sink]

    # 音频同步：off → -vsync no（直播式即时出声，不再为对齐视频延迟音频，
    # 修掉刷短视频时「下一个视频声音晚几秒才跟上」；auto/其它 → 不传，保留 uxplay
    # 默认的基于时间戳 A/V 同步，看电影更严丝合缝）。
    # 注：UxPlay 1.73.7 的 -vsync 选项完整可用（1.46 才没有），见 1.73.7 man page。
    if (s.get("audio_sync") or "off") == "off":
        args += ["-vsync", "no"]

    audio = s.get("audio_sink") or "auto"
    if audio and audio != "auto":
        args += ["-as", str(audio)]

    extra = (s.get("extra") or "").strip()
    if extra:
        # 用户自己负责正确性；这里只做最基础的空白拆词
        args += extra.split()

    return args