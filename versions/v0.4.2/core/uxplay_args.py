"""把设置翻译成 uxplay 命令行参数。

逻辑移植自 Decky 插件的 launch_airplay.sh，但去掉了容器相关部分——独立 App
本身就在用户的图形会话里，DISPLAY/XAUTHORITY 天然正确，直接拼参数跑 uxplay 即可。

关键约定（与插件版保持一致）：
  * 30fps + -vsync no：不等待垂直同步，落后直接丢帧追新，低延迟。
  * video_sink=ximagesink 时不自动缩放，resolution=auto 则锁定 1280x800（Deck 原生）。
  * display_mode=auto：游戏模式强制 -fs（gamescope 只呈现全屏 X11 窗口），桌面默认窗口。
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
    # 【重要】resolution=auto 时**不要**强制 -s。
    # 之前这里会把 ximagesink 锁到 1280x800（Deck 原生），但 videoscale 默认
    # add-borders=false —— 比例不一致时是「填满并裁掉」，iPhone 竖屏/ iPad 4:3
    # 投过来就会把底部（或顶部）裁掉一截。跟随设备分辨率才不会裁。
    # 需要固定尺寸时，用户在下拉里显式选一个即可。
    if res_for_s is None and sink == "ximagesink":
        res_for_s = None  # 保持 None：不传 -s，交给 uxplay 用设备原生分辨率
    if res_for_s:
        args += ["-s", res_for_s]

    rotate = s.get("rotate") or "none"
    if rotate in ("R", "L"):
        args += ["-r", rotate]

    flip = s.get("flip") or "none"
    if flip in ("H", "V", "I"):
        args += ["-f", flip]

    display_mode = s.get("display_mode") or "auto"
    if display_mode == "fullscreen":
        args += ["-fs"]
    elif display_mode == "window":
        pass  # 普通窗口
    else:  # auto
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
