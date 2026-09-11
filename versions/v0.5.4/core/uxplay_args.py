"""把设置翻译成 uxplay 命令行参数。

逻辑移植自 Decky 插件的 launch_airplay.sh，但去掉了容器相关部分——独立 App
本身就在用户的图形会话里，DISPLAY/XAUTHORITY 天然正确，直接拼参数跑 uxplay 即可。

关键约定（与插件版保持一致）：
  * resolution=auto 时向客户端请求 1280x800（Deck 原生）流，窗口即铺满 1280×800 屏幕、
    全画面不裁切。
  * UxPlay 1.46（Ubuntu 22.04 仓库版，man page 已核对）没有任何全屏选项：
    -fs/-vsync/-reset 全都不存在，传了直接 unknown option 退出。因此 display_mode
    不再产生任何 uxplay 参数——三种模式都靠 -s 1280x800 的正常窗口铺满屏幕；
    游戏模式下 gamescope 会把与屏幕同尺寸的窗口呈现为满屏。
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

    # display_mode 不再影响 uxplay 参数：UxPlay 1.46 无 -fs（全屏）选项，
    # 传了直接 unknown option 退出（v0.5.1 用户日志实证）。三种模式统一靠
    # 上方 -s 1280x800 的正常窗口铺满屏幕、全画面不裁切；
    # 游戏模式下 gamescope 会把同尺寸窗口呈现为满屏。
    # 该设置仍保留在 UI 中，只控制 App 自身的接管行为（隐藏主窗口/悬浮条）。

    if s.get("legacy_ports"):
        args += ["-p"]
    if s.get("keep_window"):
        args += ["-nc"]

    # 注：UxPlay 1.46 已移除 -reset 选项（传了会报 unknown option 直接退出），不再发射

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
