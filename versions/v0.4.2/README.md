# AirPlay Deck —— 独立桌面 App（替代 Decky 插件路线）

把 Steam Deck / 任意 Linux 桌面变成 AirPlay 接收端，让 iPhone / iPad / Mac 一键镜像投屏。
本程序包装成熟的 **UxPlay** 引擎，提供图形化一键启动、友好设置，并支持通过「添加为非 Steam 游戏」
在 SteamOS 游戏模式下全屏出画面。

> 为什么不做成 Decky 插件？插件是跑在图形会话之外的后台服务，拿不到 gamescope 在游戏模式下
> 发放的 `DISPLAY` / `XAUTHORITY`，导致「只有声音没有画面」的结构性死结。独立 App 作为非 Steam
> 游戏运行时，本身活在用户的图形会话里，这些环境变量天然正确。

---

## 〇、先说结论与硬约束

**目标形态：一个自包含的 `AirPlayDeck-x86_64.AppImage` 单文件**，放到 Deck 的 `~/Applications`，
「添加为非 Steam 游戏」即可在游戏模式全屏出画面。AppImage 是单文件、不依赖运行时装包，
且放在家目录，**SteamOS 系统更新不会清掉它**（更新只重映像系统分区，`/home` 不变）。

**硬约束（务必先读懂）：AppImage 只能在 x86_64 Linux 上构建一次，然后拷到 Deck 用。**
- ❌ **不能在 Deck 自身构建**：SteamOS 根目录只读 + `python3` 被 PEP 668 标记为外部托管（连
  `ensurepip` 都装不进 pip）+ 仓库签名密钥不可信（pacman 装包 PGP 校验失败）。你之前正是卡在这三步。
- ❌ **不能在 macOS 构建**：AppImage 工具链只支持 Linux。
- ✅ **必须在一个正常的 Linux x86_64 环境构建一次**：Deck 上的 distrobox 容器 / 任意 Linux 虚拟机 /
  GitHub Actions。构建环境只要能正常 `pip install PyQt6` 即可（Ubuntu 22.04 最稳）。

下面给两条构建路径，**任选其一**。构建出的 `.AppImage` 是一样的，拷到 Deck 后用法完全相同。

---

## 一、构建 AppImage 的路径 1：在 Deck 的 distrobox 容器里构建（推荐，零额外工具）

你已有 distrobox（Deck 自带 podman），且容器**家目录与 Deck 主机共享**，构建产物直接落在 `~/` 里可用。

> **【前提，务必先做】先把本仓库全部源码传到 Deck 的 `~/airplay-deck/`**（即 `/home/deck/airplay-deck/`），
> 包含 `airplay-deck.py`、`core/`、`resources/`、`build-appimage.sh`、`requirements.txt`、`.github/`。
> 构建是从**本地源码**打包，脚本不会自动下载这些文件——若 `~/airplay-deck/` 里没有源码，构建会直接报错、不出产物。
> 因为 distrobox 共享家目录，文件放进 Deck 的 `~/airplay-deck/` 后，容器里 `cd ~/airplay-deck` 即可见。

```bash
# 1) 在 Deck 桌面模式终端，建一个 Ubuntu 构建容器（只需一次）
distrobox create -i docker.io/library/ubuntu:22.04 -n airplay-builder
distrobox enter airplay-builder

# 2) 容器内（标准 Ubuntu，apt/pip 都正常，无任何 SteamOS 限制）：
sudo apt update
sudo apt install -y python3-pip curl libfuse2 squashfs-tools file patchelf
cd ~/airplay-deck            # 家目录共享，源码直接可见
chmod +x build-appimage.sh
./build-appimage.sh

# 3) 产物 AirPlayDeck-x86_64.AppImage 已生成在 ~/airplay-deck/（与 Deck 主机同一份家目录）
exit                         # 退出容器
```

> 若 `docker.io/library/ubuntu:22.04` 拉取慢，换 `ubuntu:22.04` 或 `debian:12` 亦可。

---

## 二、构建 AppImage 的路径 2：GitHub Actions（零本地 Linux，自动出产物）

不需要任何本地 Linux / Docker / macOS 构建。把本仓库推到 GitHub，Actions 在标准 Ubuntu runner 上
自动跑 `build-appimage.sh` 并产出可下载的 AppImage。

1. 在 GitHub 新建一个仓库（如 `airplay-deck`）。
2. 把本目录所有文件（含 `.github/workflows/build-appimage.yml`）推上去。
3. 仓库 → Actions → 选 `Build AirPlay Deck AppImage` → `Run workflow`。
4. 跑完后在 Actions 页面的 **Artifacts** 里下载 `AirPlayDeck-AppImage`（即 `.AppImage` 文件）。

（你也可以直接 `git push` 到 `main`/`master` 分支触发。）

---

## 三、在 Steam Deck 上使用 AppImage（运行时无任何安装）

1. 把 `AirPlayDeck-x86_64.AppImage` 放到 Deck 的 `~/Applications/`（如不存在就建一个）。
2. 终端执行：`chmod +x ~/Applications/AirPlayDeck-x86_64.AppImage`
3. Steam → 库 → 添加非 Steam 游戏 → 浏览，选中这个 `.AppImage`。
4. 右键该条目 → 属性 → 名称改成「AirPlay Deck」。
5. 第一次运行前，确保 uxplay 可用（见下）；启动后：
   - **游戏模式**：点「开始接收」窗口自动隐藏，UxPlay 全屏接管；iPhone 控制中心 → 屏幕镜像 → 选设备名。
     按 **Steam 键 → 退出游戏** 停止。
   - **桌面模式**：默认也全屏接管（隐藏本 App 窗口，顶部留一条悬浮控制条）；若想保留可拖动窗口，把「显示模式」设「窗口」即可。

### uxplay 从哪来？（AppImage 不含 uxplay）
App 默认带「**通过 distrobox 容器运行 uxplay**」开关（容器名 `uxplay-env`）。你之前装 Decky 插件时
已经建好了这个容器，里面就有 uxplay —— 勾上开关即可**免本机原生安装**，App 会把 gamescope 给的
`DISPLAY`/`XAUTHORITY` 透传进容器，uxplay 在容器内开窗口并借宿主 avahi 注册 mDNS。
（若你更想本机原生装 uxplay：`sudo steamos-readonly disable` → 装 AUR 的 `yay` → `yay -S uxplay`
→ 重新锁回只读，并关闭 App 里的 distrobox 开关。）

### 系统更新会不会清掉？
- **AppImage 文件**在 `~/Applications`（家目录）→ 更新持久。
- **distrobox 容器**`uxplay-env` 在 `~/.local/share/containers`（家目录）→ 更新持久。
所以整套「AppImage + uxplay-env 容器」在 SteamOS 更新后都还在，无需重装。

---

## 四、旧 Decky 插件的关系

旧插件（`decky-airplay/`，0.2.21）保留不动。本仓库是全新独立项目 `airplay-deck/`，走「独立 App +
非 Steam 游戏」路线，从结构上解决游戏模式黑屏。两者并存、互不覆盖。

## 四之二、版本记录（每次迭代保留一份源码快照，不覆盖）

| 版本 | 主要变化 |
|------|----------|
| v0.2.0 | clashmi 多页 UI、自绘开关、版本号进文件名、App 图标 |
| v0.3.0 | 页面转场动画、SVG 菜单图标、iOS 圆角图标、podman 免授权容器模式、持久日志文件 |
| v0.4.0 | 真正的左右滑动转场、四语切换（简/繁/英/日）、保存反馈、状态实时化、帧率固定 30/60 档、游戏模式悬浮控制条、触摸尺寸放大、容器 X11 cookie 拷贝、不再触发密码框 |

快照目录：`versions/v0.2.0/`、`versions/v0.3.0/`、`versions/v0.4.0/` …，每个版本一份完整源码，互不覆盖。

## 五、设置项速查

| 设置 | 默认 | 说明 |
|------|------|------|
| 视频后端 | ximagesink | 纯 X11 不依赖 GL，Xwayland/gamescope 下最稳；auto 会选 GL 类可能黑屏 |
| 帧率 | 30 | **只有 30 / 60 两档**。自由填值会让部分 App（B站、相册）投屏卡死 |
| vsync | off | 不等待垂直同步、落后直接丢帧追新 |
| 显示模式 | auto | auto=游戏模式全屏 / 桌面窗口 |
| 分辨率 | auto | ximagesink + auto 锁 1280x800（Deck 原生，避免截断） |
| 通过容器运行 | 关 | 开=复用 uxplay-env 容器，免本机安装 uxplay |
| 界面语言 | 简体中文 | 简中 / 繁中 / English / 日本語，实时切换 |

## 六、排错

- **AppImage 双击无反应 / 启动报错**：SteamOS 镜像**自带 libfuse2**，AppImage 通常能直接跑，无需装任何系统包。
  所以「双击没反应」绝大多数情况不是 fuse 问题，而是 GUI 内部报错——请在 Deck 桌面模式**终端里直接跑**
  `./AirPlayDeck-x86_64.AppImage` 看完整 traceback。若确实报 FUSE 相关错误（极少见），在桌面模式执行
  `sudo steamos-readonly disable && sudo pacman -S fuse2 && sudo steamos-readonly enable`（一次性，落在只读系统分区，
  更新可能被清，但极少需要）。也可装 `AppImageLauncher` 做桌面集成。
- **iPhone 搜不到设备**：avahi 没起。App 启动会等 avahi 就绪；仍不行 `systemctl status avahi-daemon`。
- **只有声音没画面**：确认视频后端是 ximagesink（默认已是）；且是作为**非 Steam 游戏**启动，而非桌面终端直接跑。
- **找不到 uxplay**：勾上 App 里的「通过容器运行 uxplay」（复用 uxplay-env）。
- **反复弹出输入密码框**：v0.4.0 起 avahi 检测改为**纯只读**（`systemctl is-active` / `pgrep` / `dbus-send`），
  不再执行任何 `pkexec` / `sudo`，不会再触发 polkit。
- **改了设置后能连上但没画面**：先点「视频与渲染」里的**恢复推荐画质**（30 fps + ximagesink + 自动），
  保存后 App 会自动重启 uxplay 让新参数生效。
- **游戏模式点了开始就黑屏、回不到 App**：顶部会出现一条**悬浮控制条**（停止接收 / 设置）；
  实在不行按 Steam 键 →「退出游戏」，进程的 SIGTERM 处理器会顺带杀掉 uxplay。
- **B站 / 相册投屏卡死**：把帧率设为 **30**（默认），不要用自由帧率；仍不行再关掉「保持窗口」。
- **iOS 搜得到但连不上**：容器模式下宿主机的 XAUTHORITY 路径在容器里不存在，
  v0.4.0 会 `podman cp` 一份 cookie 到容器 `/tmp/.airplay_xauth` 再传 `XAUTHORITY`，按此逻辑仍失败请导出日志。

## 七、目录结构

```
airplay-deck/
├── airplay-deck.py                  # GUI 主程序（PyQt6）
├── build-appimage.sh                # 构建 AppImage（appimagetool + venv 打包 PyQt6，必须在 Linux 上跑）
├── .github/workflows/build-appimage.yml  # GitHub Actions 自动构建
├── core/                           # 纯 Python 核心（无 PyQt 依赖）
│   ├── settings.py / uxplay_args.py / session.py / avahi.py / launcher.py / i18n.py
├── resources/icon.svg              # iOS 圆角矩形图标
├── resources/icons/*.svg           # clashmi 风格菜单线框图标
├── versions/vX.Y.Z/                # 每个版本的完整源码快照（不覆盖）
├── requirements.txt
└── README.md
```
（注：`steam-deck-launch.sh` 是「直接跑源码」的备选入口，走 AppImage 路线不需要它。）
```
