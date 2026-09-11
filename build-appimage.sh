#!/usr/bin/env bash
# ============================================================================
#  build-appimage.sh —— 在 x86_64 Linux 上把 AirPlay Deck 打包成自包含 AppImage。
#
#  【重要】本脚本必须在 Linux x86_64 环境运行：
#    * SteamOS 自身因只读根目录 + PEP668 + 仓库签名不可信，无法在此构建；
#    * macOS 不支持 AppImage 工具链，也无法构建。
#  推荐构建环境（任选其一，都能正常 pip install PySide6）：
#    1) Deck 上的 distrobox Ubuntu 容器（已自带 podman，且家目录与主机共享）
#    2) 任意 Linux 虚拟机 / 物理机
#    3) GitHub Actions（本仓库已附 .github/workflows/build-appimage.yml，push 即自动构建）
#
#  产物：AirPlayDeck-<版本号>-x86_64.AppImage（单文件，拷到 Deck 的 ~/Applications 即可，
#        系统更新不会清掉，运行时无需 pip/pacman）。
#
#  说明：uxplay 不打进 AppImage，运行时通过 distrobox 容器 uxplay-env 调用（见 README）。
# ============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# 从源码读 APP_VERSION 作为产物文件名版本号（每次迭代只需改 airplay-deck.py 里的常量）
# grep -P（PCRE）在部分精简环境不可用，故失败后再用 sed 兜底，避免静默变成 0.1.0。
VERSION=$(grep -oP 'APP_VERSION\s*=\s*"\K[^"]+' airplay-deck.py 2>/dev/null || true)
if [ -z "$VERSION" ]; then
  VERSION=$(sed -n 's/^APP_VERSION[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' airplay-deck.py 2>/dev/null | head -n1)
fi
if [ -z "$VERSION" ]; then
  VERSION="0.1.0"
fi
echo "[build] 读取版本号：$VERSION"

# ---------------------------------------------------------------------------
# 0) 环境自检：绝不能在 SteamOS 主机上直接跑
#    主机没有 pip（本次报错 `No module named pip` 就是因为在主机跑），
#    且根目录只读 + PEP668，装不了任何 python 包。必须进构建容器。
# ---------------------------------------------------------------------------
if [ -r /etc/os-release ] && grep -qiE '^ID=.*steamos' /etc/os-release 2>/dev/null; then
  echo "[build] ✗ 检测到当前是 SteamOS 主机环境，不能在这里构建："
  echo "         · 主机没有 pip（No module named pip）"
  echo "         · 根目录只读 + PEP668，无法安装 python 包"
  echo ""
  echo "        请先退出到主机后进入构建容器再跑："
  echo "          distrobox enter airplay-builder"
  echo "          cd ~/airplay-deck && ./build-appimage.sh"
  exit 1
fi

# pip 引导：有些精简镜像 / 容器没有 pip，这里自动补上
ensure_pip() {
  if python3 -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  echo "[build] 未检测到 pip，尝试引导…"
  python3 -m ensurepip --upgrade >/dev/null 2>&1 && return 0
  if command -v apt-get >/dev/null 2>&1; then
    (sudo -n true 2>/dev/null && sudo -n apt-get install -y python3-pip >/dev/null 2>&1) && return 0
    apt-get install -y python3-pip >/dev/null 2>&1 && return 0
  fi
  return 1
}
ensure_pip || {
  echo "[build] 无法获取 pip。请在构建容器内执行：sudo apt install -y python3-pip 后重试。"
  exit 1
}

# appimagetool 运行必须要 file（部分环境还要 patchelf）。精简 ubuntu 镜像/新容器常缺，
# 9/9 首次构建就卡在 "file command is missing but required"。这里在容器内自动补上，
# 免得每次新建构建容器都要手动装一遍。
ensure_build_tools() {
  local missing=""
  command -v file >/dev/null 2>&1 || missing="$missing file"
  command -v patchelf >/dev/null 2>&1 || missing="$missing patchelf"
  if [ -z "$missing" ]; then
    return 0
  fi
  echo "[build] 缺少构建工具:$missing，尝试自动安装（容器内 apt）…"
  if command -v apt-get >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null; then
      sudo -n apt-get -o DPkg::Lock::Timeout=300 update -qq 2>/dev/null || true
      sudo -n apt-get -o DPkg::Lock::Timeout=300 install -y $missing >/dev/null 2>&1 && return 0
    fi
    apt-get -o DPkg::Lock::Timeout=300 update -qq 2>/dev/null || true
    apt-get -o DPkg::Lock::Timeout=300 install -y $missing >/dev/null 2>&1 && return 0
  fi
  command -v file >/dev/null 2>&1
}
ensure_build_tools || {
  echo "[build] 自动安装 file/patchelf 失败。请退出脚本后在主机执行（免密码，rootless 容器内 root）："
  echo "          podman exec -u 0 airplay-builder bash -lc \"apt-get update && apt-get install -y file patchelf\""
  echo "        然后重新运行本脚本。"
  exit 1
}

# 本脚本不依赖 linuxdeploy 的 python/qt 插件（那些插件下载地址已失效），改为：
#   1) 把系统 python 解释器 + 标准库整体复制进 AppDir/usr
#   2) pip 把 PySide6 装进 AppDir 的 dist-packages（Ubuntu/Debian 的 python 只认 dist-packages，不是 site-packages）
#   3) 用 appimagetool（单文件二进制）把 AppDir 压成 AppImage
# PySide6 自带的 Qt 库/平台插件由 AppRun 在运行时用环境变量指过去，无需 qt 插件部署。

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
# 2) 组装 AppDir：系统 python + PySide6 + 应用代码
# ---------------------------------------------------------------------------
echo "[build] 组装 AppDir（系统 python + PySide6）…"
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

# 瘦身：删掉标准库里用不到的模块（测试/包管理/IDE/tk），可省几十 MB。
rm -rf AppDir/usr/lib/python$PYV/test \
       AppDir/usr/lib/python$PYV/ensurepip \
       AppDir/usr/lib/python$PYV/idlelib \
       AppDir/usr/lib/python$PYV/tkinter \
       AppDir/usr/lib/python$PYV/turtle.py \
       AppDir/usr/lib/python$PYV/lib2to3 \
       AppDir/usr/lib/python$PYV/distutils 2>/dev/null || true
# 清掉所有 __pycache__（运行时会重新生成），进一步压缩。
find AppDir/usr/lib/python$PYV -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 安装 PySide6 到 AppDir 的 dist-packages（manylinux 轮子自带完整 Qt6；Ubuntu/Debian 的 python 只认 dist-packages）
echo "[build] pip 安装 PySide6（需联网下载 Qt 轮子，可能几十秒）…"
python3 -m pip install --upgrade pip >/dev/null 2>&1 || true

# pip 安装到指定目录。PEP668 环境（Debian/Ubuntu 新版）会拦「外部管理」，
# 加 --break-system-packages 兜底重试一次。
pip_install_target() {
  local target="$1"; shift
  python3 -m pip install --target="$target" "$@" && return 0
  python3 -m pip install --break-system-packages --target="$target" "$@"
}

# 注意：Ubuntu/Debian 的 python 按规则从 dist-packages 找包（不是 site-packages），
# 必须装到 dist-packages，否则打包后的 python 会找不到 PySide6（报 ModuleNotFoundError）。
if ! pip_install_target "AppDir/usr/lib/python$PYV/dist-packages" "PySide6>=6.4"; then
  echo "[build] pip 安装 PySide6 失败，请检查网络 / PyPI 可达性后重试。"
  echo "        若在容器内且网络慢，可换国内镜像后重试："
  echo "        python3 -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --target=AppDir/usr/lib/python$PYV/dist-packages PySide6"
  exit 1
fi

# ---- AppImage 瘦身 ----
# PySide6 的 manylinux 轮子把整套 Qt6（Qml/Quick/3D/Charts…）+ Shiboken 一起打包，
# 实际本程序只用到 Core/Gui/Widgets/Svg。下面删掉用不到的模块与插件、并剥离调试符号，
# 可把 236MB 量级降到 140–170MB 左右，且不影响功能（其余 Qt 模块本程序从不 import）。
PYSIDE_LIB="AppDir/usr/lib/python$PYV/dist-packages/PySide6"
if [ -d "$PYSIDE_LIB/Qt/lib" ]; then
  QT_LIB_DIR="$PYSIDE_LIB/Qt/lib"
elif [ -d "$PYSIDE_LIB/Qt6/lib" ]; then
  QT_LIB_DIR="$PYSIDE_LIB/Qt6/lib"
else
  QT_LIB_DIR="$PYSIDE_LIB/Qt/lib"
fi

# 1) 删除用不到的 Qt6 共享库（只保留 Core/Gui/Widgets/Svg/DBus/OpenGL/XcbQpa 及其支撑库）
echo "[build] 瘦身：删除未使用的 Qt6 模块…"
if [ -d "$QT_LIB_DIR" ]; then
  find "$QT_LIB_DIR" -maxdepth 1 -type f -name 'libQt6*.so*' | while read -r f; do
    case "$(basename "$f")" in
      libQt6Core.so*|libQt6Gui.so*|libQt6Widgets.so*|libQt6Svg.so*| \
      libQt6DBus.so*|libQt6OpenGL.so*|libQt6OpenGLWidgets.so*|libQt6XcbQpa.so*| \
      libQt6EglFS.so*|libQt6EglSupport.so*|libQt6DeviceDiscoverySupport.so*| \
      libQt6FbSupport.so*|libQt6GLSupport.so*|libQt6ThemeSupport.so*| \
      libQt6XdgSupport.so*|libQt6InputSupport.so*|libQt6ServiceSupport.so*| \
      libQt6EventDispatcherSupport.so*|libQt6FontDatabaseSupport.so*| \
      libQt6PrintSupport.so*|libQt6Xml.so*|libQt6Network.so*|libQt6Concurrent.so*| \
      libQt6Sql.so*)
        ;;  # 保留
      *)
        rm -f "$f" ;;
    esac
  done
fi

# 2) 删除用不到的 PySide6 模块绑定（本程序未 import 的）
echo "[build] 瘦身：删除未使用的 PySide6 模块…"
for mod in Qml Quick Quick3D QuickWidgets QuickControls2 Charts Datavisualization \
           Graphs Multimedia MultimediaWidgets Positioning Location Sensors \
           SerialPort Scxml SerialBus RemoteObjects TextToSpeech SpatialAudio \
           HttpServer NetworkAuth StateMachineQml StateMachine Help WebEngineCore \
           WebEngineWidgets WebChannel WebView 3DRender 3DLogic 3DCore 3DExtras \
           3DInput 3DAnimation 3DQuick 3DQuickExtras 3DQuickRender 3DQuickInput \
           3DQuickScene2D VirtualKeyboard; do
  rm -rf "$PYSIDE_LIB/$mod" 2>/dev/null || true
  rm -f  "$PYSIDE_LIB/Qt${mod}.so" 2>/dev/null || true
done
# 删除 qml 资源目录（Qt6/Qml、Qt6/qml，仅 Qml/Quick 用到）
rm -rf "$PYSIDE_LIB/Qt/qml" "$PYSIDE_LIB/Qt6/qml" 2>/dev/null || true

# 3) 删除用不到的 Qt 插件目录 / 图片格式（只留 svg/png/ico）
echo "[build] 瘦身：删除未使用的 Qt 插件…"
for pdir in qmltooling; do
  rm -rf "$PYSIDE_LIB/Qt/plugins/$pdir" "$PYSIDE_LIB/Qt6/plugins/$pdir" 2>/dev/null || true
done
for fmt in qgif qjpeg qwebp qtiff qmng qwbmp qicns qjp2 qtaiff qtga; do
  rm -f "$PYSIDE_LIB/Qt/plugins/imageformats/$fmt.so" \
        "$PYSIDE_LIB/Qt6/plugins/imageformats/$fmt.so" 2>/dev/null || true
done

# 4) 剥离所有 .so 的调试符号（最稳妥、零功能风险的大头瘦身）
echo "[build] 瘦身：剥离 .so 调试符号…"
find "$PYSIDE_LIB" -type f -name '*.so*' -exec strip --strip-unneeded {} \; 2>/dev/null || true
find AppDir/usr/lib -type f -name '*.so*' -exec strip --strip-unneeded {} \; 2>/dev/null || true

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

# 自定义 AppRun：直接用打包好的 python3 跑 GUI，并把 PySide6 自带的 Qt 库 / 平台插件
# 指给环境（无需 qt 插件部署）。否则 PySide6 能 import 却找不到 libqxcb.so 而静默退出。
cat > AppDir/AppRun <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"

# 定位 python 目录（版本号不固定，用通配）。
PYDIR=$(ls -d "$HERE"/usr/lib/python3* 2>/dev/null | head -n1)
PYSITE="$PYDIR/site-packages"
PYDIST="$PYDIR/dist-packages"

# Ubuntu/Debian 的 python 默认只搜 dist-packages；PySide6 已装到 dist-packages。
# 这里动态探测 PySide6 实际落在哪个目录（dist 优先，回退 site），避免路径写死导致
# "No module named PySide6" 或 Qt 库/平台插件找不到。
if [ -d "$PYDIST/PySide6" ]; then
  PKGDIR="$PYDIST"
elif [ -d "$PYSITE/PySide6" ]; then
  PKGDIR="$PYSITE"
else
  PKGDIR="$PYDIST"
fi
if [ -d "$PKGDIR/PySide6/Qt/lib" ]; then
  QT_LIB="$PKGDIR/PySide6/Qt/lib"
  QT_PLUG="$PKGDIR/PySide6/Qt/plugins"
elif [ -d "$PKGDIR/PySide6/Qt6/lib" ]; then
  QT_LIB="$PKGDIR/PySide6/Qt6/lib"
  QT_PLUG="$PKGDIR/PySide6/Qt6/plugins"
else
  QT_LIB="$PKGDIR/PySide6/Qt/lib"
  QT_PLUG="$PKGDIR/PySide6/Qt/plugins"
fi

export LD_LIBRARY_PATH="$HERE/usr/lib:$HERE/usr/lib/x86_64-linux-gnu:$QT_LIB:${LD_LIBRARY_PATH:-}"
export QT_PLUGIN_PATH="$HERE/usr/plugins:$HERE/usr/lib/plugins:$HERE/usr/lib/qt6/plugins:$QT_PLUG:${QT_PLUGIN_PATH:-}"

# 应用代码路径 + PySide6 包目录都加进 PYTHONPATH（双保险，确保能被 import）
# 默认走 XCB（SteamOS 走 Xwayland，最稳；可被外部环境变量覆盖）
export PYTHONPATH="$HERE/opt/airplay-deck:$PYSITE:$PYDIST:${PYTHONPATH:-}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"

exec "$HERE/usr/bin/python3" "$HERE/opt/airplay-deck/airplay-deck.py" "$@"
EOF
chmod +x AppDir/AppRun

# ---- 瘦身自检：确认删掉 Qt 库/插件后，AppDir 里的 PySide6 仍然可用 ----
# 瘦身靠「删除用不到的模块」实现，一旦误删（例如某个模块实际被依赖），
# 打出来的包能正常生成、但运行时才会崩。这里在打包前用 AppDir 自带的 python
# 真跑一遍 Qt（offscreen，无需显示器），不通过就直接让构建失败。
echo "[build] 自检：验证瘦身后的 AppDir 可用…"
PYDIR_APP="$(ls -d "$HERE/AppDir"/usr/lib/python3* 2>/dev/null | head -n1)"
if [ -d "$HERE/$PYSIDE_LIB/Qt/lib" ]; then
  QT_LIB_APP="$HERE/$PYSIDE_LIB/Qt/lib"; QT_PLUG_APP="$HERE/$PYSIDE_LIB/Qt/plugins"
else
  QT_LIB_APP="$HERE/$PYSIDE_LIB/Qt6/lib"; QT_PLUG_APP="$HERE/$PYSIDE_LIB/Qt6/plugins"
fi
SELFTEST_OUT=$(LD_LIBRARY_PATH="$HERE/AppDir/usr/lib:$HERE/AppDir/usr/lib/x86_64-linux-gnu:$QT_LIB_APP" \
  QT_PLUGIN_PATH="$QT_PLUG_APP" \
  QT_QPA_PLATFORM=offscreen \
  PYTHONPATH="$HERE/AppDir/opt/airplay-deck:$PYDIR_APP/dist-packages:$PYDIR_APP/site-packages" \
  "$HERE/AppDir/usr/bin/python3" -c "
from PySide6.QtWidgets import QApplication, QPushButton, QMainWindow
from PySide6.QtGui import QIcon, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer
app = QApplication([])
w = QMainWindow(); w.setCentralWidget(QPushButton('x')); w.show()
print('SLIM_SELFTEST_OK')" 2>&1) || true
echo "$SELFTEST_OUT" | tail -n 8
if echo "$SELFTEST_OUT" | grep -q "SLIM_SELFTEST_OK"; then
  echo "[build] 自检通过：瘦身后的 Qt 可用。"
elif echo "$SELFTEST_OUT" | grep -Eq "libGL\.so|libEGL\.so|libX11\.so"; then
  # 缺的是系统级图形库（Mesa 等），不是被我们删掉的 Qt 库：属于构建机缺依赖，
  # 运行环境（桌面 Linux / Steam Deck）自带这些库，不应因此中断构建。
  echo "[build] ⚠ 自检跳过：构建环境缺少系统图形库（libGL/libEGL/libX11），无法离屏启动 Qt。"
  echo "        这是构建机缺依赖（apt install -y libgl1），不是瘦身删错了库；继续打包。"
else
  echo "[build] ✗ 瘦身自检失败：被删掉的 Qt 库/插件可能是必需的。"
  echo "        请回退瘦身步骤（或把缺失模块加回保留列表）后再打包，不要发布这个产物。"
  exit 1
fi

# ---------------------------------------------------------------------------
# 3) 打包成 AppImage
# ---------------------------------------------------------------------------
echo "[build] 运行 appimagetool 生成 AppImage…"
OUTPUT_APPIMAGE="$HERE/AirPlayDeck-${VERSION}-x86_64.AppImage"
# 若 appimagetool 支持 --comp，则用 xz 进一步减小体积（不支持则忽略）。
# 注意：只能填 gzip 或 xz——该版本明确只支持这两种，填 zstd 会直接报错退出。
COMP_FLAG=""
if $APPIMAGETOOL --help 2>&1 | grep -q -- '--comp'; then
  COMP_FLAG="--comp xz"
fi
$APPIMAGETOOL $COMP_FLAG "$HERE/AppDir" "$OUTPUT_APPIMAGE" \
  || { echo "[build] appimagetool 失败。常见原因：缺少 file/patchelf（sudo apt install -y file patchelf）或 libfuse2（sudo apt install -y libfuse2）"; exit 1; }

echo "[build] 完成。产物："
ls -lh "$HERE"/*.AppImage 2>/dev/null
