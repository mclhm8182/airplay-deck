#!/usr/bin/env bash
# ============================================================================
#  steam-deck-launch.sh —— 在 Steam Deck 上「添加为非 Steam 游戏」的启动入口。
#
#  用法：把本文件 chmod +x 后，在 Steam → 添加非 Steam 游戏 → 浏览选中它。
#  游戏模式下点「开始接收」后窗口会自动隐藏，UxPlay 全屏接管；
#  按 Steam 键 → 退出游戏 即可停止（脚本会转发 SIGTERM 给 Python 进程）。
#
#  SteamOS 默认没有 pip、且根目录只读，本脚本会自动引导：
#    1) 若 python 无 pip，先用 ensurepip 引导（无需 sudo）；
#       失败再提示用 steamos-readonly disable + pacman 装 python-pip。
#    2) 首次运行自动 pip 安装 PyQt6（需联网一次，装到用户目录 ~/.local，不碰系统）。
#  DISPLAY / XAUTHORITY 由 gamescope 注入，本脚本原样透传给 Python/uxplay。
# ============================================================================
set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# 1) 确保 pip 可用（SteamOS 默认没装 pip）
if ! python3 -m pip --version >/dev/null 2>&1; then
  echo "[launch] 本机 python 没有 pip，尝试用 ensurepip 引导（无需 sudo）…"
  python3 -m ensurepip --user --upgrade 2>/dev/null || {
    echo "[launch] ensurepip 不可用，需要解锁根目录后用 pacman 装 python-pip："
    echo "    sudo steamos-readonly disable"
    echo "    sudo pacman-key --init && sudo pacman-key --populate archlinux"
    echo "    sudo pacman -S --needed python-pip"
    echo "    sudo steamos-readonly enable   # 装完 pip 后可重新锁回只读"
    exit 1
  }
fi

# 2) 确保 PyQt6 可用（仅首次需联网，装到用户目录）
if ! python3 -c "import PyQt6" >/dev/null 2>&1; then
  echo "[launch] 首次运行：安装 PyQt6（需要网络）…"
  python3 -m pip install --user PyQt6
fi

# 转发 SIGTERM（Steam「退出游戏」）给 Python 进程，确保干净关闭 uxplay
trap 'kill -TERM "$PID" 2>/dev/null' TERM INT

python3 "$HERE/airplay-deck.py" "$@" &
PID=$!
wait "$PID"
