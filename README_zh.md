# AirPlay Deck

> English version: [README.md](README.md)

把 Steam Deck 变成 AirPlay 接收端，让 iPhone、iPad一键镜像投屏。其他的 Linux 系统请自行尝试，不保证一定可用。

本程序基于成熟的 [UxPlay](https://github.com/FDH2/UxPlay) 引擎（自编译 1.73.7），在AI 辅助下完成，提供图形界面、一键启动与可调设置，开箱即用。

> **免责声明：** 本项目是实验性、AI 辅助的个人 hobby 项目，与 Apple 公司无任何隶属或背书关系，AirPlay 是 Apple 的商标。它基于第三方引擎 [UxPlay](https://github.com/FDH2/UxPlay)（GPL-3.0）封装，仅供个人使用。功能可能变动、可能出 bug，使用风险自负。欢迎提意见和一起完善。

## 功能特性

- 一键镜像：打开即用，在 iPhone / iPad 控制中心「屏幕镜像」里选择本机即可连接
- 桌面模式稳定可用（推荐）；游戏模式有实验性修复（X11 鉴权、强制 `-fs`、长片投屏保活：更强的 systemd-inhibit + 宿主 xset 周期性防熄屏），但**尚未完整支持**。
- 首次启动自动编译安装运行环境（distrobox 容器），无需手动装包
- 音频、分辨率、帧率、显示模式均可调
- 图形界面支持 8 种语言（简体中文、繁體中文、English、日本語、한국어、Français、Deutsch、Español），并跟随系统自动切换

## 安装（下载即用）

1. 前往 [Releases](https://github.com/mclhm8182/airplay-deck/releases) 下载 `AirPlayDeck-x86_64.AppImage`。
2. 下载完成后直接打开即可，无需安装、单文件运行。
3. 首次启动会引导你一键安装运行环境（约几分钟，需要联网拉取 Ubuntu 镜像并编译 UxPlay）。之后每次打开即用。

AppImage 放在home 目录，SteamOS 系统更新不会清除它；运行环境容器也在home 目录，同样持久。

## 使用

1. 打开 AirPlay Deck，点击「开始接收」。
2. 在 iPhone / iPad 的控制中心打开「屏幕镜像」，选择本机即可开始投屏。
3. 画面以默认以窗口形式呈现，可随时用任务栏或切回本程序。也可以自主设置为全屏模式。

**关于音频**

默认开启「直播式」音频同步：刷短视频、玩游戏时声音即时跟上，不会在切换内容时延迟几秒。如果你用来看电影、希望音画严格对齐，可在「视频与渲染」里打开「音频同步」。

> 游戏模式（SteamOS 全屏接管）仍为实验性：本分支改进了容器内 X11 鉴权，并针对长片投屏数分钟后「画面黑、声音还在」加强保活（systemd-inhibit `--what=idle:sleep --mode=block` + 宿主 xset 周期性 `s reset` / `dpms force on`，停止时再亮屏一次；仍属实验）。gamescope 下始终强制全屏（`-fs`）。桌面模式仍然是可靠路径。

## 截图

| 主界面 | 设备与设置 | 关于 |
| --- | --- | --- |
| ![主界面](screenshots/main.png) | ![设备与设置](screenshots/settings.png) | ![关于](screenshots/about.png) |


## 设置说明

| 设置 | 默认 | 说明 |
| --- | --- | --- |
| 视频后端 | ximagesink | 纯 X11 渲染，不依赖 OpenGL，在 Xwayland / gamescope 下最稳定 |
| 帧率 | 30 | 仅 30 / 60 两档；自由填值可能导致部分 App 投屏卡死 |
| 显示模式 | 窗口 | 窗口：可拖动小窗；全屏：无边框铺满；自动：程序自动选择 |
| 分辨率 | 自动 | 自动时向设备请求 1280×800（Deck 原生）流，画面铺满且不裁切 |
| 音频同步 | 关 | 关：直播式即时出声；开：基于时间戳的严格音画同步（适合看电影） |
| 投屏时保持屏幕常亮 | 开 | uxplay 运行期间阻止系统熄屏；游戏模式另加宿主 xset 保活（实验） |
| 通过容器运行 | 开 | 复用 uxplay-env 容器，首次启动自动安装（默认开启） |

## 常见问题

**AppImage 双击没反应**
多数情况是程序内部报错，不是 FUSE 问题。请在桌面模式的终端里直接运行 `./AirPlayDeck-x86_64.AppImage` 查看完整报错。

**手机搜不到设备**
通常是 avahi（mDNS）没有正常启动。App 启动时会等待 avahi 就绪；若长时间搜不到，可在终端确认宿主 `systemctl status avahi-daemon`。容器内的 mDNS 由 App 自动维护，一般不需要手动处理。

**有声音没画面，或只有画面没声音**
- 没画面：确认视频后端是 ximagesink（默认即是），且本程序是从桌面环境打开（双击 AppImage 或桌面启动器），而不是在无法访问图形界面的纯命令行终端（如 SSH）里运行。
- 没声音：SteamOS 的声音走 PipeWire，App 会自动把容器接入宿主的 PulseAudio 兼容接口，通常无需设置。可在日志里搜索 `音频已接入宿主机 PulseAudio` 确认。

**画面卡顿、不流畅**
先看日志是否出现 `raop_rtp resend failed` 或 `since last client feedback request`——这两行一起出现说明是 Wi-Fi 丢包，不是性能问题。按优先级排查：手机与 Deck 都连 5GHz 并靠近路由器；关闭路由器的省电模式和手机的低电量模式；视频后端保持 ximagesink；网络不稳时把帧率调回 30；分辨率保持自动。

**安装运行环境失败**
先在「检查与日志」页点「安装 / 重建运行环境」。若因网络拉不到 UxPlay 源码（国内网络常见），可在能联网的环境下载 [v1.73.7 源码包](https://github.com/FDH2/UxPlay/archive/refs/tags/v1.73.7.tar.gz)，存为 Deck home 目录下的 `~/uxplay-1.73.7.tar.gz` 再重试，脚本会优先使用本地包。

**日志出现 `no element "ximagesink"`，或一直连上却没画面**
说明容器里缺 `gstreamer1.0-x` 包。在终端运行一次即可修复（无需重装 AppImage）：
```bash
podman exec -u 0 uxplay-env bash -lc "apt-get update && apt-get install -y gstreamer1.0-x gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-tools && rm -rf /home/*/.cache/gstreamer-1.0 /root/.cache/gstreamer-1.0 && gst-inspect-1.0 ximagesink"
```

**游戏模式：能连上/有声音但没画面，或日志出现 Authorization required**
SteamOS 游戏模式下 Xwayland 属于 Deck 用户（uid 1000），容器默认可能是 root。近期版本会以宿主 uid/gid 跑 X 探针和 uxplay，并在有 cookie 时避免回退到 `unset XAUTHORITY`。若仍失败，请在「检查与日志」导出日志（关注宿主 libX11 实测与容器内 uid），并改用桌面模式。

**游戏模式：长片投屏几分钟后画面变黑、声音还在，电源键也无法恢复**
gamescope 可能独立于 logind 熄屏。v0.6.11 起在「投屏时保持屏幕常亮」开启时，uxplay 一启动即启用更强的 systemd-inhibit，并在宿主 DISPLAY 上用 xset 周期性保活（停止接收时再强制亮屏一次）。若主机没有 `xset`，会降级为仅 inhibit（仍可能黑屏）。此问题仍属实验性修复；若复现请导出日志并改用桌面模式。

## 从源码构建（可选）

预编译包已通过 GitHub Actions 自动生成，绝大多数用户直接下载即可。如果你需要自己构建：

- **在 Deck 的 distrobox 容器里构建**：把仓库源码放进 `~/airplay-deck/`，进入 `ubuntu:22.04` 容器后运行 `./build-appimage.sh`。
- **用 GitHub Actions 构建**：把仓库推到 GitHub，推送 `main` 分支会自动构建（产物在 Actions Artifacts，供你验证）；打好版本标签 `vX.Y.Z` 并推送后才会发布到 Releases。

构建必须在 x86_64 Linux 上进行（AppImage 工具链仅支持 Linux，无法在 macOS 或 ARM 设备原生打包）。

## 说明

- 本项目在 AI 辅助下完成（vibe coding）。代码与文档均开源，欢迎提交 issue 和 PR。
- 投屏引擎基于 [UxPlay](https://github.com/FDH2/UxPlay)（GPL-3.0 许可）等开源项目，在此致谢。

## 许可证

本程序自身以 MIT 许可证发布。需说明：随附的投屏引擎 [UxPlay](https://github.com/FDH2/UxPlay) 为 GPL-3.0，PySide6（LGPL v3）、GStreamer（LGPL）等运行依赖亦采用相应开源许可证。