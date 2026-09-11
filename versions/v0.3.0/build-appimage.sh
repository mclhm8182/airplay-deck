#!/usr/bin/env bash
# ============================================================================
#  build-appimage.sh —— 在 x86_64 Linux 上把 AirPlay Deck 打包成自包含 AppImage。
#
#  【重要】本脚本必须在 Linux x86_64 环境运行：
#    * SteamOS 自身因只读根目录 + PEP668 + 仓库签名不可信，无法在此构建；
#    * macOS 不支持 AppImage 工具链，也无法构建。
#  推荐构建环境（任选其一，都能正常 pip install PyQt6）：
#    1) Deck 上的 distrobox Ubuntu 容器（已自带 podman，且家目录与主机共享）
#    2) 任意 Linux 虚拟机 / 物理机
#    3) GitHub Actions（本仓库已附 .github/workflows/build-appimage.yml，push 即自动构建）
#
#  产物：AirPlayDeck-x86_64.AppImage（单文件，拷到 Deck 的 ~/Applications 即可，
#        系统更新不会清掉，运行时无需 pip/pacman）。
#
#  说明：uxplay 不打进 AppImage，运行时通过 distrobox 容器 uxplay-env 调用（见 README）。
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# 从源码读 APP_VERSION 作为产物文件名版本号（每次迭代只需改 airplay-deck.py 里的常量）
VERSION=$(grep -oP 'APP_VERSION\s*=\s*"\K[^"]+' airplay-deck.py 2>/dev/null || echo "0.1.0")
echo "[build] 读取版本号：$VERSION"

# 本脚本不依赖 linuxdeploy 的 python/qt 插件（那些插件下载地址已失效），改为：
#   1) 把系统 python 解释器 + 标准库整体复制进 AppDir/usr
#   2) pip 把 PyQt6 装进 AppDir 的 dist-packages（Ubuntu/Debian 的 python 只认 dist-packages，不是 site-packages）
#   3) 用 appimagetool（单文件二进制）把 AppDir 压成 AppImage
# PyQt6 自带的 Qt 库/平台插件由 AppRun 在运行时用环境变量指过去，无需 qt 插件部署。

echo "[build] 检测系统 python…"
command -v python3 >/dev/null 2>&1 || { echo "[build] 缺少 python3，请先：sudo apt install -y python3 python3-pip"; exit 1; }
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
echo "[build] 系统 python 版本：$PYV"

# ---------------------------------------------------------------------------
# 1) 下载 appimagetool（经典 AppImageKit 版，单文件，URL 已验证可用）
# ---------------------------------------------------------------------------
download() {
  local out="$1" url="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fSL -o "$out" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$out" "$url"
  else
    echo "[build] 需要 curl 或 wget 来下载工具，请先：sudo apt install -y curl"
    return 1
  fi
}

echo "[build] 下载 appimagetool…"
if [ ! -x appimagetool-x86_64.AppImage ]; then
  download appimagetool-x86_64.AppImage \
    "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage" \
    || { echo "[build] 下载 appimagetool 失败，请检查网络 / 代理后重试。"; exit 1; }
  chmod +x appimagetool-x86_64.AppImage
fi
# appimagetool 自身是 AppImage，运行需要 libfuse2；若直接跑失败，先提取再运行
if ! ./appimagetool-x86_64.AppImage --version >/dev/null 2>&1; then
  echo "[build] appimagetool 直接运行失败（多为缺少 libfuse2），尝试 --appimage-extract 提取后运行…"
  ./appimagetool-x86_64.AppImage --appimage-extract >/dev/null 2>&1 || true
  if [ -x squashfs-root/AppRun ]; then
    APPIMAGETOOL="./squashfs-root/AppRun"
  else
    echo "[build] 提取失败，请先在构建环境安装 libfuse2：sudo apt install -y libfuse2"
    exit 1
  fi
else
  APPIMAGETOOL="./appimagetool-x86_64.AppImage"
fi

# ---------------------------------------------------------------------------
# 2) 组装 AppDir：系统 python + PyQt6 + 应用代码
# ---------------------------------------------------------------------------
echo "[build] 组装 AppDir（系统 python + PyQt6）…"
rm -rf AppDir
mkdir -p AppDir/usr/bin AppDir/usr/lib AppDir/opt/airplay-deck \
         AppDir/usr/share/applications \
         AppDir/usr/share/icons/hicolor/scalable/apps

# 复制 python 解释器与标准库（保留系统目录布局，解释器能自动找到自己的 lib）
cp -f "/usr/bin/python$PYV"        "AppDir/usr/bin/python$PYV"
ln -sf "python$PYV"               "AppDir/usr/bin/python3"
ln -sf "python$PYV"               "AppDir/usr/bin/python"
cp -rf "/usr/lib/python$PYV"      "AppDir/usr/lib/python$PYV"
# libpython 动态库（解释器启动时需要）
cp -f /usr/lib/x86_64-linux-gnu/libpython$PYV*.so* "AppDir/usr/lib/" 2>/dev/null || true

# 安装 PyQt6 到 AppDir 的 dist-packages（manylinux 轮子自带完整 Qt6；Ubuntu/Debian 的 python 只认 dist-packages）
echo "[build] pip 安装 PyQt6（需联网下载 Qt 轮子，可能几十秒）…"
python3 -m pip install --upgrade pip >/dev/null 2>&1 || true
# 注意：Ubuntu/Debian 的 python 按规则从 dist-packages 找包（不是 site-packages），
# 必须装到 dist-packages，否则打包后的 python 会找不到 PyQt6（报 ModuleNotFoundError）。
python3 -m pip install --target="AppDir/usr/lib/python$PYV/dist-packages" "PyQt6>=6.4" \
  || { echo "[build] pip 安装 PyQt6 失败，请检查网络 / PyPI 可达性后重试。"; exit 1; }

# 应用代码
cp -f airplay-deck.py            AppDir/opt/airplay-deck/
cp -rf core                      AppDir/opt/airplay-deck/
cp -rf resources                 AppDir/opt/airplay-deck/

# 图标：优先 iOS squircle 矢量 icon.svg，回退位图 icon.png。
# appimagetool 按 Icon=airplay-deck 在 AppDir 根目录找 airplay-deck.{png,svg,xpm}，
# 标准 hicolor 位置它不扫，故根目录也放一份，否则报图标缺失导致打包失败。
if [ -f resources/icon.svg ]; then
  cp -f resources/icon.svg AppDir/usr/share/icons/hicolor/scalable/apps/airplay-deck.svg
  cp -f resources/icon.svg AppDir/airplay-deck.svg
elif [ -f resources/icon.png ]; then
  cp -f resources/icon.png AppDir/usr/share/icons/hicolor/scalable/apps/airplay-deck.png
  cp -f resources/icon.png AppDir/airplay-deck.png
fi

# 桌面入口。
# 注意：AppImageKit 版 appimagetool 扫描 .desktop 时会排除 usr/share/* 路径，
# 只扫 AppDir 根目录，所以除了标准位置 usr/share/applications 外，必须在 AppDir 根也放一份，
# 否则 appimagetool 报 "Desktop file not found, aborting"。
cat > AppDir/usr/share/applications/airplay-deck.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=AirPlay Deck
Comment=AirPlay receiver for Steam Deck / Linux
Exec=AppRun
Icon=airplay-deck
Categories=Network;Utility;
Terminal=false
EOF
cp -f AppDir/usr/share/applications/airplay-deck.desktop AppDir/airplay-deck.desktop

# 自定义 AppRun：直接用打包好的 python3 跑 GUI，并把 PyQt6 自带的 Qt 库 / 平台插件
# 指给环境（无需 qt 插件部署）。否则 PyQt6 能 import 却找不到 libqxcb.so 而静默退出。
cat > AppDir/AppRun <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"

# 定位 python 目录（版本号不固定，用通配）。
PYDIR=$(ls -d "$HERE"/usr/lib/python3* 2>/dev/null | head -n1)
PYSITE="$PYDIR/site-packages"
PYDIST="$PYDIR/dist-packages"

# Ubuntu/Debian 的 python 默认只搜 dist-packages；PyQt6 已装到 dist-packages。
# 这里动态探测 PyQt6 实际落在哪个目录（dist 优先，回退 site），避免路径写死导致
# "No module named PyQt6" 或 Qt 库/平台插件找不到。
if [ -d "$PYDIST/PyQt6" ]; then
  PKGDIR="$PYDIST"
elif [ -d "$PYSITE/PyQt6" ]; then
  PKGDIR="$PYSITE"
else
  PKGDIR="$PYDIST"
fi
QT_LIB="$PKGDIR/PyQt6/Qt6/lib"
QT_PLUG="$PKGDIR/PyQt6/Qt6/plugins"

export LD_LIBRARY_PATH="$HERE/usr/lib:$HERE/usr/lib/x86_64-linux-gnu:$QT_LIB:${LD_LIBRARY_PATH:-}"
export QT_PLUGIN_PATH="$HERE/usr/plugins:$HERE/usr/lib/plugins:$HERE/usr/lib/qt6/plugins:$QT_PLUG:${QT_PLUGIN_PATH:-}"

# 应用代码路径 + PyQt6 包目录都加进 PYTHONPATH（双保险，确保能被 import）
# 默认走 XCB（SteamOS 走 Xwayland，最稳；可被外部环境变量覆盖）
export PYTHONPATH="$HERE/opt/airplay-deck:$PYSITE:$PYDIST:${PYTHONPATH:-}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"

exec "$HERE/usr/bin/python3" "$HERE/opt/airplay-deck/airplay-deck.py" "$@"
EOF
chmod +x AppDir/AppRun

# ---------------------------------------------------------------------------
# 3) 打包成 AppImage
# ---------------------------------------------------------------------------
echo "[build] 运行 appimagetool 生成 AppImage…"
OUTPUT_APPIMAGE="$HERE/AirPlayDeck-${VERSION}-x86_64.AppImage"
$APPIMAGETOOL "$HERE/AppDir" "$OUTPUT_APPIMAGE" \
  || { echo "[build] appimagetool 失败。常见原因：缺少 file/patchelf（sudo apt install -y file patchelf）或 libfuse2（sudo apt install -y libfuse2）"; exit 1; }

echo "[build] 完成。产物："
ls -lh "$HERE"/*.AppImage 2>/dev/null
