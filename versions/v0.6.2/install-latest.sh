#!/usr/bin/env bash
# ============================================================================
#  install-latest.sh —— 在【SteamOS 主机】上执行（不要在容器里跑）
#
#  作用：把刚构建好的 AppImage 装到 ~/Applications，并建立一个固定名字的软链
#        ~/Applications/AirPlayDeck.AppImage -> 最新版本
#
#  为什么要软链：AppImage 文件名带版本号（AirPlayDeck-0.4.1-x86_64.AppImage），
#  每升级一次文件名就变一次，Steam「添加非 Steam 游戏」里填的启动路径会失效，
#  得重新添加一遍。用软链后 Steam 只添加一次固定路径，以后升级只换软链指向即可。
# ============================================================================
set -euo pipefail

SRC_DIR="${HOME}/airplay-deck"
DEST_DIR="${HOME}/Applications"
LINK_NAME="AirPlayDeck.AppImage"

echo "[install] 查找 ${SRC_DIR} 下最新的 AppImage…"
APPIMAGE=$(ls -1t "${SRC_DIR}"/AirPlayDeck-*-x86_64.AppImage 2>/dev/null | head -n1 || true)
if [ -z "${APPIMAGE}" ]; then
  echo "[install] ✗ 没找到任何 AirPlayDeck-*-x86_64.AppImage"
  echo "          请先在构建容器里跑完 ./build-appimage.sh"
  exit 1
fi
echo "[install] 使用：$(basename "${APPIMAGE}")"

mkdir -p "${DEST_DIR}"

# 拷到 ~/Applications（保留带版本号的文件名，便于回溯是哪一版）
cp -f "${APPIMAGE}" "${DEST_DIR}/"
chmod +x "${DEST_DIR}/$(basename "${APPIMAGE}")"

# 固定名软链：Steam 里只添加这个路径
ln -sf "${DEST_DIR}/$(basename "${APPIMAGE}")" "${DEST_DIR}/${LINK_NAME}"
chmod +x "${DEST_DIR}/${LINK_NAME}"

echo "[install] ✓ 完成"
ls -lh "${DEST_DIR}/${LINK_NAME}"
echo ""
echo "现在可以在 Steam 里添加非 Steam 游戏："
echo "  ${DEST_DIR}/${LINK_NAME}"
echo "（已经添加过的不用重复添加，软链已指向最新版）"
