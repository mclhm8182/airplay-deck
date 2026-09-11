"""把设置翻译成 uxplay 命令行参数。

逻辑移植自 Decky 插件的 launch_airplay.sh，但去掉了容器相关部分——独立 App
本身就在用户的图形会话里，DISPLAY/XAUTHORITY 天然正确，直接拼参数跑 uxplay 即可。

关键约定（与插件版保持一致）：
  * 30fps + -vsync no：不等待垂直同步，落后直接丢帧追新，低延迟。
  * resolution=auto 时向客户端请求 1280x800（Deck 原生）流，避免窗口超出 1280x800 被裁切。
  * display_mode=auto：游戏模式无边框全屏；桌面模式把 uxplay 窗口拉到铺满 1280×800 屏幕
    （靠 -s 1280x800 保证全画面、不裁切），但保留正常窗口（非无边框全屏）。
    fullscreen=始终 -fs 无边框全屏；window=保留可拖动窗口（横屏内容可能超出屏幕）。
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

    vsync = s.get("vsync") or "off"
    if vsync == "off":
        args += ["-vsync", "no"]
    elif vsync == "sync":
        args += ["-vsync"]
    else:
        args += ["-vsync", str(vsync)]

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

    display_mode = s.get("display_mode") or "auto"
    # 桌面模式「自动全屏」= 把 uxplay 窗口拉到铺满 1280×800 屏幕（靠 -s 1280x800 保证全画面、
    # 不裁切），但保留正常窗口（非无边框全屏），便于多任务、不抢占桌面面板。
    # 仅「全屏」选项与游戏模式才用 -fs 无边框全屏（gamescope 只呈现全屏窗口）。
    if display_mode == "window":
        pass  # 普通可拖动窗口（横屏内容可能超出屏幕，用户自选）
    elif display_mode == "fullscreen":
        args += ["-fs"]
    else:  # auto：游戏模式无边框全屏；桌面模式窗口铺满屏幕（非无边框）
        if is_gamemode:
            args += ["-fs"]

    if s.get("legacy_ports"):
        args += ["-p"]
    if s.get("keep_window"):
        args += ["-nc"]

    reset = s.get("reset")
    if reset:
        try:
            reset_n = int(reset)
            if reset_n > 0:
                args += ["-reset", str(reset_n)]
        except (TypeError, ValueError):
            pass

    # 手动指定视频后端时才传 -vs；auto 不传，让 UxPlay 自己选
    if sink:
        args += ["-vs", sink]

    audio = s.get("audio_sink") or "auto"
    if audio and audio != "auto":
        args += ["-as", str(audio)]

    extra = (s.get("extra") or "").strip()
    if extra:
        # 用户自己负责正确性；这里只做最基础的空白拆词
        args += extra.split()

    return args
