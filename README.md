# AirPlay Deck —— 独立桌面 App（替代 Decky 插件路线）

把 Steam Deck / 任意 Linux 桌面变成 AirPlay 接收端，让 iPhone / iPad / Mac 一键镜像投屏。
本程序包装成熟的 **UxPlay** 引擎，提供图形化一键启动、友好设置，并支持通过「添加为非 Steam 游戏」
在 SteamOS 游戏模式下全屏出画面。

> 为什么不做成 Decky 插件？插件是跑在图形会话之外的后台服务，拿不到 gamescope 在游戏模式下
> 发放的 `DISPLAY` / `XAUTHORITY`，导致「只有声音没有画面」的结构性死结。独立 App 作为非 Steam
> 游戏运行时，本身活在用户的图形会话里，这些环境变量天然正确。

---

## 〇、先说结论与硬约束

**目标形态：一个自包含的 `AirPlayDeck-<版本号>-x86_64.AppImage` 单文件**（如 `AirPlayDeck-0.6.0-x86_64.AppImage`），放到 Deck 的 `~/Applications`，
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

> ⚠️ **常见坑（你这次就踩了）**：脚本开头有 SteamOS 主机守卫。如果**没先进容器**就直接跑
> `./build-appimage.sh`，会立刻报「检测到当前是 SteamOS 主机环境，不能在这里构建」并退出——这**不是脚本 bug**，
> 是故意拦的（主机没 pip、根目录只读，装不了 PyQt6）。报错 `no such container airplay-builder`
> 说明你从没建过 `airplay-builder` 容器，所以 `distrobox enter` 失败、脚本落到主机上跑了。

**最简一键部署命令（首次自动建容器，之后复用；整段粘贴到 Deck 桌面终端）：**

```bash
distrobox create -i docker.io/library/ubuntu:22.04 -n airplay-builder 2>/dev/null || true; \
distrobox enter airplay-builder -- bash -lc \
  "sudo apt update && sudo apt install -y python3-pip curl libfuse2 squashfs-tools file patchelf && cd ~/airplay-deck && chmod +x build-appimage.sh && ./build-appimage.sh"
```

> 若提示 `build-appimage.sh: No such file or directory`，说明 `~/airplay-deck/` 里还没有源码（见上方【前提】），
> 先把仓库全部文件传进 Deck 的 `~/airplay-deck/`，再跑上面这条。

分步等价写法（便于看懂）：

```bash
# 1) 建 Ubuntu 构建容器（只需一次；已建过可跳过）
distrobox create -i docker.io/library/ubuntu:22.04 -n airplay-builder
distrobox enter airplay-builder

# 2) 容器内（标准 Ubuntu，apt/pip 都正常，无任何 SteamOS 限制）：
sudo apt update
sudo apt install -y python3-pip curl libfuse2 squashfs-tools file patchelf
cd ~/airplay-deck            # 家目录共享，源码直接可见
chmod +x build-appimage.sh
./build-appimage.sh

# 3) 产物 AirPlayDeck-<版本号>-x86_64.AppImage（当前 0.6.0）已生成在 ~/airplay-deck/
exit                         # 退出容器
```

> 若 `docker.io/library/ubuntu:22.04` 拉取慢，换 `ubuntu:22.04` 或 `debian:12` 亦可。

---

## 二、构建 AppImage 的路径 2：GitHub Actions（零本地 Linux，自动出产物）

不需要任何本地 Linux / Docker / macOS 构建。把本仓库推到 GitHub，Actions 在标准 Ubuntu runner 上
自动跑 `build-appimage.sh` 并产出可下载的 AppImage。

**推上去就会自动构建**（workflow 里配了 `on: push: branches: [main, master]`），不用手动点。

### 一次性准备

1. 在 GitHub 新建一个仓库（如 `airplay-deck`）。
2. 把本目录所有文件（含 `.github/workflows/build-appimage.yml`）推上去：
   ```bash
   cd airplay-deck
   git init -b main
   git add -A && git commit -m "airplay-deck v0.6.0"
   git remote add origin git@github.com:<你的账号>/airplay-deck.git   # 或 https://github.com/<你的账号>/airplay-deck.git
   git push -u origin main
   ```
   > ⚠️ **用 HTTPS + Personal Access Token 推送时，token 必须勾选 `workflow` 权限**，
   > 否则含 `.github/workflows/` 的推送会被 GitHub 拒绝（报 `refusing to allow a Personal Access Token
   > to create or update workflow`）。用 SSH 或 `gh auth login` 则无此问题。

### 拿产物

3. 推完在仓库 → **Actions** → `Build AirPlay Deck AppImage`，等约 2–4 分钟跑完。
4. 在该次运行的 **Artifacts** 里下载 `AirPlayDeck-AppImage`（解压后里面是
   `AirPlayDeck-<版本号>-x86_64.AppImage`，当前即 `AirPlayDeck-0.6.0-x86_64.AppImage`）。

> 想在网页上手动重跑：Actions → 选左边 `Build AirPlay Deck AppImage` → **Run workflow**。

> **workflow 为什么这样写**：宿主固定 `ubuntu-24.04`（长期支持），但整个构建跑在
> `container: ubuntu:22.04` 里。因为 AppImage 打包的是**构建机上的** python 解释器，
> 它链接构建机的 glibc——在 glibc 2.39 的 24.04 上构建，拿到较老的 SteamOS 会报
> `GLIBC_2.39 not found`；用 22.04 容器（glibc 2.35）则向前兼容，产出与下面路径 1 的
> distrobox 容器完全一致。不直接写 `runs-on: ubuntu-22.04` 是因为该 runner 标签
> 自 2026-09-17 起进入弃用期（有 24 小时临时不可用窗口），2027-04-17 彻底下线，
> 而 Docker 镜像 `ubuntu:22.04` 不受此时间表影响。

> **为什么这条路径值得留着**：AppImage 内嵌的是 **x86_64** 的 Python + PyQt6，只能在
> x86_64 Linux 上打包。**macOS 无法构建**（AppImage 工具链只有 Linux 版）——尤其苹果芯片
> （M 系列，arm64）即使装了 Docker，默认也是 arm64 虚拟机，仍需专门开 x86_64 环境。
> 所以「本地 Mac 直接编译打包」不成立，走这里的云端构建最省事。

---

## 三、在 Steam Deck 上使用 AppImage（运行时无任何安装）

1. 把构建产物 `AirPlayDeck-<版本号>-x86_64.AppImage`（当前 `AirPlayDeck-0.6.0-x86_64.AppImage`，文件名带版本号）放到 Deck 的 `~/Applications/`（如不存在就建一个；嫌版本号碍事可重命名为 `AirPlayDeck-x86_64.AppImage`）。
2. 终端执行：`chmod +x ~/Applications/AirPlayDeck-0.6.0-x86_64.AppImage`
3. Steam → 库 → 添加非 Steam 游戏 → 浏览，选中这个 `.AppImage`。
4. 右键该条目 → 属性 → 名称改成「AirPlay Deck」。
5. 第一次运行前，确保 uxplay 可用（见下）；启动后：
   - **游戏模式**：点「开始接收」窗口自动隐藏，UxPlay 全屏接管；iPhone 控制中心 → 屏幕镜像 → 选设备名。
     按 **Steam 键 → 退出游戏** 停止。
   - **桌面模式**：默认「自动」接管（隐藏本 App 窗口、顶部留一条悬浮控制条），UxPlay 以**正常窗口**铺满 1280×800 屏幕——全画面、不裁切、非无边框全屏；若想保留可拖动小窗口，把「显示模式」设「窗口」即可。

### uxplay 从哪来？（AppImage 不含 uxplay，但会首次自动编译安装 **UxPlay 1.73.7**）
App 默认开启「**通过 distrobox 容器运行 uxplay**」（容器名 `uxplay-env`）。第一次打开 App 时，
若检测到本机还没有这个容器，会**自动弹窗引导你一键安装**：在后台创建 `uxplay-env` 容器，先 apt 装好
编译工具链（build-essential / cmake / 各 -dev 库）与 GStreamer 全套插件，再**从源码编译 UxPlay 1.73.7**
装到容器内 `/usr/local/bin/uxplay`（首次需联网拉取 Ubuntu 镜像 + 编译，约 400–800 MB 下载、编译 1–3 分钟，
请耐心等进度条走完）。

> **为什么不用 apt 的 uxplay？** Ubuntu 22.04 仓库里只有 **1.46**（2022 年版）——选项集老旧
> （连 `-fs` 全屏都没有，我们为此踩过一串 `unknown option … stopping`），且含未修复的安全问题：
> 直到 **1.73.7** 才修掉 `lib/raop_handlers.h` 的栈缓冲区溢出 + 空指针解引用
> （安全公告 **GHSA-479c-ww7g-wgp8**），并适配 iOS 27 的 TEARDOWN 变更。所以改为源码编译 1.73.7。

> 🔌 **GitHub 下不动怎么办**：安装脚本会依次尝试 3 个官方下载地址，全失败会明确报错。此时可在任意能上网的
> 地方下载 `v1.73.7` 源码包，存成 **`~/uxplay-1.73.7.tar.gz`**（放到 Deck 家目录即可），再重试安装——
> 脚本会优先使用这个本地包，完全无需联网。

装好后即可投屏，无需任何本机原生安装。App 会把 gamescope 给的 `DISPLAY`/`XAUTHORITY` 透传进容器，
uxplay 在容器内开窗口并借宿主 avahi 注册 mDNS。
（若你更想本机原生装 uxplay：`sudo steamos-readonly disable` → 装 AUR 的 `yay` → `yay -S uxplay`
→ 重新锁回只读；如需改用原生 uxplay，把设置文件里的 `use_distrobox` 改为 `false` 并填好 `uxplay_path`。）
在「检查与日志」页还能随时**一键检查运行环境**、**一键安装 / 重建**运行环境，或**一键卸载环境**（删容器 + 镜像 + 应用数据）。

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
| v0.4.1 | 容器内 X 鉴权枚举实测（gst-launch 真连 X）、stop 杀干净容器内 uxplay、渲染器起不来无限重启修复、UI 对齐/标题居中/返回键修整 |
| v0.4.2 | 默认全屏接管（桌面也全屏 + 悬浮条）、界面语言自动跟随系统（不支持则默认英语） |
| v0.4.3 | 方案 B：首启自动安装 uxplay 运行环境（distrobox 容器 + GStreamer）、检查页新增一键安装/重建与「一键清爽卸载」 |
| v0.4.4 | 修复 0.4.3 投屏画面被截断（`resolution=auto` 现向客户端请求 1280x800 流，`auto` 显示模式在游戏模式无边框全屏、桌面模式把 uxplay 窗口拉到铺满 1280×800 屏幕——正常窗口、非无边框全屏，靠 `-s 1280x800` 保证全画面不裁切，避免窗口在 Deck 1280×800 上被剪）；新增「投屏时保持屏幕常亮」（连接后 systemd-inhibit 接管空闲/休眠，停止即释放）；修正容器「一键卸载」卡死（conmon 残留无法 `podman rm`）与重装时 apt 权限错误（改为容器内 root 直接 `apt-get install`）；安装脚本增加 apt 锁等待 + 重试，避免与 distrobox 初始化抢 `/var/lib/apt/lists/lock` 导致 `Could not get lock` 失败。**全程无需 sudo/密码**（rootless 容器，apt 以容器内 root 运行）；UI 精简：「检查与日志」页去掉普通用户无需改的 uxplay 路径 / 容器名输入框，并把「检查 uxplay」「测试容器」两个按钮合并为「一键检查运行环境」，删去视频页四处小字说明 |
| v0.5.1 | 修复「安装环境失败」与「投屏搜不到设备」复发：UxPlay 1.46 同样不识别 `-reset` 选项（旧默认 15 秒会让 uxplay 一启动就 `unknown option -reset` 退出、mDNS 不广播），已彻底移除该参数及对应设置项/界面控件/旧配置迁移；修复容器首次安装与 distrobox 初始化 apt 抢 `lock-frontend` 失败——裸 ubuntu 镜像无 pgrep 导致旧等待循环立即跳出，改为 apt 自带 `DPkg::Lock::Timeout=600` 排队等锁 + pgrep 可选等待，不再删除锁文件（避免双 apt 并发损坏 dpkg 库） |
| v0.5.2 | 对照 UxPlay 1.46 官方 man page 确认其**无 -fs 全屏选项**（与 -vsync/-reset 同被移除），删除 -fs 发射；`display_mode` 不再产生任何 uxplay 参数，三模式统一靠 `-s 1280x800` 的正常窗口铺满屏幕、全画面不裁切；默认 `display_mode` 改为「窗口」。新增容器安装后 ximagesink 渲染后端校验（缺失会触发 `renderer->sink` 断言崩溃）：自动 `--reinstall` 修复并二次校验，仍失败则明确报错提示修复命令 |
| v0.6.0 | **引擎大升级：从 apt 的 UxPlay 1.46 换成自编译 UxPlay 1.73.7**（跨 4 年）。原因：1.46 选项集老旧且含未修复漏洞。`core/container.py` 不再 apt 装 uxplay，改为装 `build-essential/cmake/libssl-dev/libplist-dev/libavahi-compat-libdnssd-dev/libgstreamer*-dev/libx11-dev` 后，从 GitHub 拉 v1.73.7 源码 `cmake && make && make install` 到 `/usr/local/bin`（依次尝试 3 个官方地址；GitHub 不可达时可手动把源码包放 `~/uxplay-1.73.7.tar.gz` 走离线分支）；`uxplay_installed()` 同时探测 `/usr/local/bin`。收益：**恢复 `-fs` 全屏**（`core/uxplay_args.py` 重新按 display_mode 发射：window 不发 / fullscreen 发 / auto 仅游戏模式发），并修掉 `raop_handlers.h` 栈溢出+空指针（GHSA-479c-ww7g-wgp8）、iOS 27 适配、多处 segfault |
| v0.5.4 | 修复「安装环境失败：`E: Could not get lock /var/cache/apt/archives/lock`」——这是**第三把** apt 锁（下载缓存锁），与之前修的 dpkg/lists 锁不同，`DPkg::Lock::Timeout` **管不到它**。上一轮安装被中断后容器里的 apt-get 会变孤儿一直抱着这把锁，之后每次安装都秒失败。修复：装前遍历 `/proc` 检测残留 apt/dpkg（不依赖 procps/pgrep，裸镜像没有），先等 60s，仍活着则判定孤儿强杀，再 `dpkg --configure -a` 修复半装状态。仍**不删除锁文件**（Debian 官方反对，可能损坏 dpkg 库） |
| v0.5.3 | 根治「装好环境还是不能投屏、日志刷 `Assertion 'renderer->sink' failed`」：`--reinstall` 只修系统 `.so`，但 GStreamer **注册表缓存**（`~/.cache/gstreamer-1.0/registry.x86_64.bin`）仍是旧的「插件不可用」记录，uxplay 读到过期缓存继续崩溃。修复点：(1) 容器安装/修复分支在 `--reinstall` 后**清除注册表缓存**（`/home/*/.cache/gstreamer-1.0`、`/root/.cache/gstreamer-1.0`——因 uxplay 经 podman 以宿主 `/home/deck` 运行、缓存落在那）；(2) 致命检测新增 `renderer->sink` / `video_renderer_init` / `assertion` 字样，渲染器起不来时**停止无限重启**并直给修复命令；(3) 校验失败报错信息补上清缓存命令。旧容器需跑一次救援命令才会好（见部署说明） |

快照目录：`versions/v0.2.0/`、`versions/v0.3.0/`、`versions/v0.4.0/` …，每个版本一份完整源码，互不覆盖。

## 五、设置项速查

| 设置 | 默认 | 说明 |
|------|------|------|
| 视频后端 | ximagesink | 纯 X11 不依赖 GL，Xwayland/gamescope 下最稳；auto 会选 GL 类可能黑屏 |
| 帧率 | 30 | **只有 30 / 60 两档**。自由填值会让部分 App（B站、相册）投屏卡死 |
| 显示模式 | window | 引擎为自编译 **UxPlay 1.73.7，支持 `-fs` 全屏**（需构建时带 X11，已装 `libx11-dev`）。window = 普通可拖动窗口（不传 `-fs`，靠 `-s 1280x800` 铺满、全画面不裁切）；fullscreen = 无边框全屏（传 `-fs`）；auto = 游戏模式无边框全屏（传 `-fs`）、桌面模式正常窗口铺满。默认「窗口」便于多任务 |
| 分辨率 | auto | auto 时向客户端请求 1280x800（Deck 原生）流，锚定窗口高度、避免被截；固定尺寸则尊重用户选择 |
| 投屏保活 | 开 | 连接设备后通过 systemd-inhibit 阻止系统空闲与熄屏；关闭则按系统设置正常休眠 |
| 通过容器运行 | 开 | 开=复用 uxplay-env 容器，免本机安装 uxplay；首次打开会自动引导安装 |
| 运行环境 | — | 「检查与日志」页：一键安装/重建 uxplay 容器，或一键清爽卸载（删容器+镜像+应用数据） |
| 界面语言 | 简体中文 | 简中 / 繁中 / English / 日本語，实时切换 |

## 六、排错

- **AppImage 双击无反应 / 启动报错**：SteamOS 镜像**自带 libfuse2**，AppImage 通常能直接跑，无需装任何系统包。
  所以「双击没反应」绝大多数情况不是 fuse 问题，而是 GUI 内部报错——请在 Deck 桌面模式**终端里直接跑**
  `./AirPlayDeck-0.6.0-x86_64.AppImage`（或你实际的版本号文件名）看完整 traceback。若确实报 FUSE 相关错误（极少见），在桌面模式执行
  `sudo steamos-readonly disable && sudo pacman -S fuse2 && sudo steamos-readonly enable`（一次性，落在只读系统分区，
  更新可能被清，但极少需要）。也可装 `AppImageLauncher` 做桌面集成。
- **iPhone 搜不到设备**：avahi 没起。App 启动会等 avahi 就绪；仍不行 `systemctl status avahi-daemon`。
- **只有声音没画面**：确认视频后端是 ximagesink（默认已是）；且是作为**非 Steam 游戏**启动，而非桌面终端直接跑。
- **日志刷 `Assertion 'renderer->sink' failed` / 退出码 134，始终搜得到连得上却没画面**：GStreamer 注册表缓存过期（apt 锁中断装坏插件后遗留）。在 Deck 桌面终端跑一次救援命令即可，无需重装 AppImage：
  ```bash
  podman exec -u 0 uxplay-env bash -lc "apt-get update && apt-get install -y --reinstall gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-tools && rm -rf /home/*/.cache/gstreamer-1.0 /root/.cache/gstreamer-1.0 && gst-inspect-1.0 ximagesink"
  ```
  看到 `ximagesink` 插件信息即修复成功，重开 App 点「开始接收」即可。v0.5.3 起，App 首次安装/重建环境会自动清掉该缓存，新容器不会再有此问题。
- **安装环境报 `E: Could not get lock /var/cache/apt/archives/lock`（被某个 pid 持有）**：上一轮安装被中断，容器里的 apt-get 变成孤儿抱着锁不放。
  `DPkg::Lock::Timeout` 管不到这把锁（它只覆盖 dpkg 锁）。在 Deck 桌面终端跑一次即可清掉孤儿并续装（免密码）：
  ```bash
  podman exec -u 0 uxplay-env bash -lc 'for p in /proc/[0-9]*; do c=$(tr "\0" " " < $p/cmdline 2>/dev/null); case "${c%% *}" in */apt-get|apt-get|*/apt|apt|*/dpkg|dpkg) echo "kill $p"; kill -9 ${p#/proc/} 2>/dev/null;; esac; done; dpkg --configure -a; apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y uxplay gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-vaapi libavahi-compat-libdnssd-dev avahi-utils pkg-config gstreamer1.0-tools'
  ```
  v0.5.4 起 App 会在安装前自动完成这套「等 60s → 强杀孤儿 → `dpkg --configure -a`」清理，无需手动。
  （**不要** `rm` 那些锁文件：Debian 官方明确反对，可能导致两个 apt 并发损坏 dpkg 库。）
- **安装失败于「下载 UxPlay 源码」（GitHub 不可达）**：v0.6.0 起安装会从 GitHub 拉 UxPlay 1.73.7 源码来编译，
  脚本依次尝试 3 个官方地址；若都失败（国内网络常见），可换网络，或用任意方式下好源码包放到 Deck 家目录
  `~/uxplay-1.73.7.tar.gz`，再在 App 里重新点「安装 / 重建运行环境」——脚本会优先用这个本地包，无需联网。
  官方源码包地址：`https://github.com/FDH2/UxPlay/archive/refs/tags/v1.73.7.tar.gz`。
- **容器内编译失败**：日志会打印 cmake / make 的末尾 30–40 行。多为磁盘或内存不足，或 apt 依赖没装全；
  可在「检查与日志」点「一键卸载环境」后重装（重建容器最稳）。
- **找不到 uxplay**：App 固定通过容器 `uxplay-env` 运行 uxplay，首次打开会自动引导安装该容器；若已跳过，去「检查与日志」点「安装 / 重建运行环境」。
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
