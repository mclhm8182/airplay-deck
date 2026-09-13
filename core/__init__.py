"""AirPlay Deck —— 核心模块包。

纯 Python，不依赖 PyQt，便于在任意平台做语法/逻辑校验。
GUI 层（airplay-deck.py）负责把这里的回调接到界面上。
"""

__version__ = "0.1.0"

# Game Mode X11 auth: host uid for podman exec, clearer probe reasons, cookie fallback.
# Safe on Desktop Mode (same host uid). Applied once at package import.
from . import gamemode_x11 as _gamemode_x11
_gamemode_x11.apply()
