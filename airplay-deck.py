#!/usr/bin/env python3
# ============================================================================
#  AirPlay Deck —— 独立桌面 App（替代 Decky 插件路线）
#
#  把 Steam Deck / 任意 Linux 桌面变成 AirPlay 接收端，包装成熟的 UxPlay 引擎，
#  提供图形化一键启动、友好设置、与 Steam 集成（添加为非 Steam 游戏即可在游戏模式出画面）。
#
#  为什么不做成 Decky 插件：插件是跑在图形会话之外的后台服务，拿不到 gamescope
#  在游戏模式下发放的 DISPLAY/XAUTHORITY，导致「只有声音没有画面」的结构性死结。
#  独立 App 作为非 Steam 游戏运行时，本身活在用户的图形会话里，这些环境变量天然正确。
# ============================================================================

import os
import sys
import socket
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import (
    QObject, Qt, Signal, QStandardPaths, QUrl, QLockFile,
    QPropertyAnimation, QParallelAnimationGroup, QAbstractAnimation,
    QEasingCurve, QPoint, QTimer, QThread,
)
from PySide6.QtGui import QIcon, QPainter, QColor, QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QTextEdit, QCheckBox,
    QSystemTrayIcon, QMenu, QMessageBox, QFrame, QScrollArea, QDialog, QSizePolicy,
)

from core import settings as cfg, launcher, session, uxplay_args, steam_shortcut
from core import i18n
from core import container as cman
from core import avahi

APP_VERSION = "0.7.5"
APP_TITLE = "AirPlay Deck"

# 界面主题配色（深色 / 浅色两套）。_apply_qss 按当前主题取一套填进样式表模板，
# 不把颜色写死在样式里——否则加主题要整段复制，改一处还得同步两处。
QSS_PALETTE = {
    "dark": {
        "bg": "#1a1b1e", "panel": "#25272b", "check_bg": "#3a3c42", "check_border": "#6a6d74", "border": "#2f3136",
        "text": "#e6e7ea", "text_mid": "#c3c6cc", "text_dim": "#8b8d93",
        "section": "#b8bcc4", "subheader": "#1f2024", "sep": "#2a2c30",
        "input_bg": "#1a1b1e", "input_border": "#3a3c41",
        "accent": "#4f8cff", "accent_hover": "#6aa0ff",
        "btn_bg": "#2f3136", "btn_hover": "#3a3c41", "btn_pressed": "#25272b",
        "btn_border": "#3a3c41", "btn_border_hover": "#4a4c51",
        "dis_bg": "#232427", "dis_text": "#5a5c61", "dis_border": "#2a2c2f",
        "log_bg": "#111214", "log_text": "#c9d1d9",
        "ok": "#2ea043", "danger": "#ef6464", "danger_border": "#5a2c2c",
        "danger_hover": "#3a2222",
        "scroll": "#3a3c41", "scroll_hover": "#4a4c51",
        "link": "#58a6ff", "link_hover": "#79c0ff",
        "row_hover": "#2a2c30", "menu_sel_text": "#ffffff",
    },
    "light": {
        "bg": "#f4f5f7", "panel": "#ffffff", "check_bg": "#e8eaed", "check_border": "#b0b4bb", "border": "#dfe1e5",
        "text": "#1c1e21", "text_mid": "#4a4d52", "text_dim": "#6b6f76",
        "section": "#4a4d52", "subheader": "#eceef1", "sep": "#dfe1e5",
        "input_bg": "#ffffff", "input_border": "#c7cad0",
        "accent": "#1f6feb", "accent_hover": "#388bfd",
        "btn_bg": "#ffffff", "btn_hover": "#f0f1f3", "btn_pressed": "#e6e8eb",
        "btn_border": "#c7cad0", "btn_border_hover": "#a8acb3",
        "dis_bg": "#f0f1f3", "dis_text": "#a0a3a8", "dis_border": "#dfe1e5",
        "log_bg": "#ffffff", "log_text": "#2b2f36",
        "ok": "#1a7f37", "danger": "#cf222e", "danger_border": "#e0b4b4",
        "danger_hover": "#fbe9e9",
        "scroll": "#c7cad0", "scroll_hover": "#a8acb3",
        "link": "#0969da", "link_hover": "#0550ae",
        "row_hover": "#e8eaed", "menu_sel_text": "#ffffff",
    },
}

# 改动这些设置后，若正在接收则自动重启 uxplay 让新参数生效
RUNTIME_KEYS = {
    "device_name", "append_hostname", "fps", "video_sink", "resolution",
    "display_mode", "decoder", "keep_window", "legacy_ports",
    "audio_sync",
    "pin_enabled", "pin_code",
    "extra", "uxplay_path", "use_distrobox", "distrobox_container",
    "distrobox_method",
}


# --------------------------------------------------------------------------- #
# 设备广播名计算
#
# UxPlay 的 -nh 关闭「追加主机名」；开启时广播名格式为「设备名@主机名」
# （uxplay.cpp 的 append_hostname()：name.append("@"); name.append(nodename)）。
# 主页只显示设备名，主机名只在 iPhone 的屏幕镜像列表里出现——这也是用户勾选
# 后「看起来没变化」的原因。这里集中算完整广播名，方便 App 内直接预览。
def full_device_name(device_name: str, append_hostname: bool) -> str:
    device_name = (device_name or "SteamDeck").strip() or "SteamDeck"
    if append_hostname:
        try:
            host = (socket.gethostname() or "").strip()
        except Exception:
            host = ""
        if host:
            return f"{device_name}@{host}"
    return device_name


# --------------------------------------------------------------------------- #
# 自绘开关（iOS 风格 pill toggle），用于主页一键启停
# --------------------------------------------------------------------------- #
class ToggleSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False
        self.setFixedSize(52, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._on

    def setChecked(self, on: bool) -> None:
        """程序设置（不发射信号），用于把开关状态同步到 launcher 实际状态。"""
        self._on = bool(on)
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._on = not self._on
            self.update()
            self.toggled.emit(self._on)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#4f8cff") if self._on else QColor("#3a3c41"))
        p.drawRoundedRect(rect, 15, 15)
        d = 24
        x = 3 if not self._on else rect.width() - d - 3
        y = (rect.height() - d) // 2
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(x, y, d, d)


# --------------------------------------------------------------------------- #
# 页面栈：自己管理显示/隐藏 + 左右滑动转场（不依赖 QStackedWidget，
# 避免布局在动画期间反复重置 geometry 导致动效被吃掉）
# --------------------------------------------------------------------------- #
class PageStack(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pages: list[QWidget] = []
        self._idx = 0
        self._group: QParallelAnimationGroup | None = None

    def addWidget(self, w: QWidget):
        w.setParent(self)
        w.setGeometry(0, 0, self.width(), self.height())
        if self._pages:
            w.hide()
            w.lower()
        else:
            w.show()
            w.raise_()
        self._pages.append(w)
        return len(self._pages) - 1

    def count(self) -> int:
        return len(self._pages)

    def currentIndex(self) -> int:
        return self._idx

    def currentWidget(self) -> QWidget | None:
        return self._pages[self._idx] if self._pages else None

    def widget(self, i: int) -> QWidget | None:
        return self._pages[i] if 0 <= i < len(self._pages) else None

    def setCurrentIndex(self, i: int, animate: bool = True):
        if not (0 <= i < len(self._pages)) or i == self._idx:
            return
        cur = self._pages[self._idx]
        nxt = self._pages[i]
        direction = 1 if i > self._idx else -1
        self._idx = i

        if self._group is not None:
            try:
                self._group.stop()
            except Exception:
                pass
            self._group = None

        if not animate or self.width() <= 0:
            cur.hide()
            nxt.setGeometry(0, 0, self.width(), self.height())
            nxt.show()
            nxt.raise_()
            return

        w = self.width()
        h = self.height()
        nxt.setGeometry(0, 0, w, h)
        nxt.show()
        nxt.raise_()

        group = QParallelAnimationGroup(self)
        for widget, sx, ex in ((cur, 0, -direction * w), (nxt, direction * w, 0)):
            a = QPropertyAnimation(widget, b"pos", self)
            a.setDuration(230)
            a.setStartValue(QPoint(sx, 0))
            a.setEndValue(QPoint(ex, 0))
            a.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(a)
        group.finished.connect(lambda: self._after_slide(cur))
        self._group = group
        group.start()

    def _after_slide(self, old: QWidget):
        self._group = None
        old.hide()
        # 动画期间 resizeEvent 被跳过，这里补一次几何，保证当前页尺寸正确
        for p in self._pages:
            p.setGeometry(0, 0, self.width(), self.height())
            if p is not self.currentWidget():
                p.hide()

    def _animating(self) -> bool:
        return bool(self._group and self._group.state() == QAbstractAnimation.State.Running)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._animating():
            return  # 动画期间别动 geometry，否则位移会被覆盖
        for p in self._pages:
            p.setGeometry(0, 0, self.width(), self.height())


# --------------------------------------------------------------------------- #
# 游戏模式悬浮控制条：uxplay 全屏接管后，这是唯一能回到 App 的入口
# --------------------------------------------------------------------------- #
# 进度对话框：安装 / 卸载在后台线程跑，这里只显示流式日志与最终状态
# --------------------------------------------------------------------------- #
class ProgressDialog(QDialog):
    def __init__(self, parent, title: str, intro: str):
        super().__init__(parent)
        self._t = getattr(parent, "T", lambda k, **kw: k)
        self.setWindowTitle(title)
        self.setMinimumSize(460, 440)
        # 复用主窗口的深色 QSS，保证对话框风格一致
        if parent is not None:
            self.setStyleSheet(parent.styleSheet())
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)
        intro_lbl = QLabel(intro)
        intro_lbl.setWordWrap(True)
        intro_lbl.setObjectName("hint")
        v.addWidget(intro_lbl)
        self.area = QTextEdit()
        self.area.setObjectName("log")
        self.area.setReadOnly(True)
        self.area.setMinimumHeight(220)
        v.addWidget(self.area, 1)
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("hint")
        self.status_lbl.setWordWrap(True)
        v.addWidget(self.status_lbl)
        bh = QHBoxLayout()
        bh.addStretch(1)
        self.btn_close = QPushButton(self._t("back"))
        self.btn_close.setMinimumHeight(44)
        self.btn_close.setEnabled(False)
        self.btn_close.clicked.connect(self.accept)
        bh.addWidget(self.btn_close)
        v.addLayout(bh)

    def append(self, level: str, msg: str):
        color = {
            "info": "#8b949e", "warn": "#d29922", "error": "#f85149",
            "cmd": "#58a6ff", "exit": "#8b949e", "uxplay": "#c9d1d9",
        }.get(level, "#c9d1d9")
        try:
            self.area.append(f'<span style="color:{color}">[{level}] {msg}</span>')
        except Exception:
            pass

    def finish(self, ok: bool, msg: str):
        self.status_lbl.setText(("✓ " if ok else "✗ ") + msg)
        self.btn_close.setEnabled(True)
        self.btn_close.setText(self._t("back"))


class ContainerWorker(QThread):
    """后台线程：安装 / 卸载运行环境，逐行把日志通过信号抛回 UI 线程。"""

    progress = Signal(str, str)   # (level, message)
    finished = Signal(bool, str)  # (ok, summary)

    def __init__(self, mode: str, container: str, parent=None):
        super().__init__(parent)
        self.mode = mode              # "install" | "uninstall"
        self.container = container

    def run(self):
        if self.mode == "install":
            ok, msg = cman.create_and_install(
                self.container,
                log_fn=lambda l, m: self.progress.emit(l, m),
            )
        else:
            ok, msg = cman.uninstall(
                self.container,
                log_fn=lambda l, m: self.progress.emit(l, m),
            )
        self.finished.emit(ok, msg)


# --------------------------------------------------------------------------- #
# 桥接：把后台线程的日志/状态信号转发到 UI 线程
# --------------------------------------------------------------------------- #
class Bridge(QObject):
    log_signal = Signal(str, str)
    status_signal = Signal(str)


# --------------------------------------------------------------------------- #
# 主窗口（clashmi 风格：主页 + 多子页，点击菜单滑动切换）
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = cfg.load_settings()
        self.lang = i18n.resolve_lang(self.settings.get("language", "auto"))

        self.setWindowTitle(f"{APP_TITLE}  v{APP_VERSION}")
        self.resize(470, 760)
        self.setMinimumSize(430, 620)

        self.launcher: launcher.Launcher | None = None
        self.bridge = Bridge()
        self._log_buffer: list[str] = []
        self._icon_path = self._pick_icon()
        self._save_btns: list[QPushButton] = []
        self._lang_combo: QComboBox | None = None
        self._theme_combo: QComboBox | None = None
        self._rebuilding = False
        self._was_connected = False  # 是否刚经历过一次「已连接」，用于断开后拉回主窗口

        if self._icon_path:
            self.setWindowIcon(QIcon(self._icon_path))

        self._build_pages()
        self.bridge.log_signal.connect(self._on_log)
        self.bridge.status_signal.connect(self._on_status)
        try:
            app = QApplication.instance()
            if app is not None:
                app.applicationStateChanged.connect(self._on_app_state_changed)
        except Exception:
            pass
        self._append_file_log("info", f"log file: {cfg.LOG_FILE}")
        self._setup_tray()
        # 主题为「自动」时跟随系统深浅色：KDE/GNOME 切换深浅色时立即重刷样式。
        # 老版本 Qt 没有 colorSchemeChanged，连不上就算了（自动模式会回退深色）。
        try:
            QApplication.styleHints().colorSchemeChanged.connect(self._on_system_theme_changed)
        except Exception:
            pass

        self._log("info", i18n.t("log_started", self.lang, ver=APP_VERSION))
        self._refresh_status()

        # 方案 B：首次启动自动提示安装投屏运行环境（容器化，免本机原生装 uxplay）。
        # 仅弹一次（env_bootstrap_seen 标记），之后用户可在「检查与日志」手动安装/卸载。
        if self.settings.get("use_distrobox") and not self.settings.get("env_bootstrap_seen"):
            QTimer.singleShot(400, self._maybe_first_bootstrap)

        if self.settings.get("autostart"):
            self._log("info", self.T("log_autostart"))
            self._start()

    # ---- 小工具 ------------------------------------------------------------ #
    def T(self, key: str, **kw) -> str:
        return i18n.t(key, self.lang, **kw)

    def _pick_icon(self) -> str:
        """优先用 iOS squircle 矢量图标 icon.svg，回退位图 icon.png。"""
        res = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
        for name in ("icon.svg", "icon.png"):
            p = os.path.join(res, name)
            if os.path.exists(p):
                return p
        return ""

    def _icons_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "icons")

    def _menu_icon(self, name: str, size: int = 24) -> QPixmap:
        p = os.path.join(self._icons_dir(), f"{name}.svg")
        if os.path.exists(p):
            return QIcon(p).pixmap(size, size)
        return QIcon().pixmap(size, size)

    # ---- 页面构建 ---------------------------------------------------------- #
    def _build_pages(self):
        self._apply_qss()
        self._save_btns = []
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.stack = PageStack()
        outer.addWidget(self.stack)

        self._page_main = self._build_main_page()      # 0
        self._page_device = self._build_device_page()  # 1
        self._page_video = self._build_video_page()    # 2
        self._page_check = self._build_check_page()    # 3
        self._page_about = self._build_about_page()    # 4
        for p in (self._page_main, self._page_device, self._page_video,
                  self._page_check, self._page_about):
            self.stack.addWidget(p)

    def _rebuild_ui(self):
        """切换语言后整体重建界面（保留日志内容）。"""
        if self._rebuilding:
            return
        self._rebuilding = True
        try:
            idx = self.stack.currentIndex()
            html = ""
            try:
                html = self.log.toHtml()
            except Exception:
                pass
            running = self.launcher is not None
            self._build_pages()
            try:
                if html:
                    self.log.setHtml(html)
            except Exception:
                pass
            self.stack.setCurrentIndex(idx, animate=False)
            self._refresh_status()
            if running:
                self.toggle.setChecked(True)
        finally:
            self._rebuilding = False

    def _goto(self, idx: int):
        self.stack.setCurrentIndex(idx)

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        # 两侧留白，与子页卡片边距保持一致（之前卡片直接贴到窗口边缘）
        v.setContentsMargins(14, 0, 14, 0)
        v.setSpacing(0)

        title = QLabel(APP_TITLE)
        title.setObjectName("appTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)
        v.addSpacing(12)

        # 状态卡
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(18, 16, 18, 16)
        cv.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(12)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color: #888; font-size: 20px;")
        self.status_label = QLabel(self.T("status_idle"))
        self.status_label.setObjectName("bigStatus")
        row.addWidget(self.dot)
        row.addWidget(self.status_label)
        row.addStretch(1)
        self.toggle = ToggleSwitch()
        self.toggle.toggled.connect(self._on_toggle_switch)
        row.addWidget(self.toggle)
        cv.addLayout(row)
        self.device_hint = QLabel(
            self.T("device_prefix")
            + full_device_name(
                self.settings.get("device_name", "SteamDeck"),
                bool(self.settings.get("append_hostname")),
            )
        )
        self.device_hint.setObjectName("deviceHint")
        cv.addWidget(self.device_hint)
        v.addWidget(card)
        v.addSpacing(12)

        # 菜单
        menu = QFrame()
        menu.setObjectName("card")
        mv = QVBoxLayout(menu)
        mv.setContentsMargins(0, 4, 0, 4)
        mv.setSpacing(0)
        items = [
            ("device", "menu_device", 1),
            ("video", "menu_video", 2),
            ("check", "menu_check", 3),
            ("about", "menu_about", 4),
        ]
        for i, (icon_name, key, idx) in enumerate(items):
            row_w = self._make_menu_row(icon_name, self.T(key))
            row_w.mousePressEvent = (
                lambda e, ix=idx: self._goto(ix)
                if e.button() == Qt.MouseButton.LeftButton else None
            )
            mv.addWidget(row_w)
            if i < len(items) - 1:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setObjectName("sep")
                sep.setFixedHeight(1)
                mv.addWidget(sep)
        v.addWidget(menu)
        v.addSpacing(12)
        self.home_pin_label = QLabel("")
        self.home_pin_label.setObjectName("bigStatus")
        self.home_pin_label.setWordWrap(True)
        self.home_pin_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.home_pin_label)
        self._refresh_home_pin()
        self.btn_add_steam = QPushButton(self.T("btn_add_steam"))
        self.btn_add_steam.setObjectName("primary")
        self.btn_add_steam.setMinimumHeight(44)
        self.btn_add_steam.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add_steam.clicked.connect(self._add_to_steam)
        v.addWidget(self.btn_add_steam)
        v.addStretch(1)
        return page

    def _make_menu_row(self, icon_name: str, text: str) -> QWidget:
        w = QWidget()
        w.setObjectName("menuRow")
        w.setMinimumHeight(60)
        w.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(w)
        h.setContentsMargins(18, 10, 16, 10)
        h.setSpacing(14)
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(26, 26)
        icon_lbl.setPixmap(self._menu_icon(icon_name, 26))
        h.addWidget(icon_lbl)
        lbl = QLabel(text)
        lbl.setObjectName("menuRowText")
        h.addWidget(lbl)
        h.addStretch(1)
        chev = QLabel("›")
        chev.setObjectName("menuRowChev")
        h.addWidget(chev)
        return w

    def _make_subpage(self, title_text: str):
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QWidget()
        header.setObjectName("subHeader")
        h = QHBoxLayout(header)
        h.setContentsMargins(6, 6, 6, 6)
        h.setSpacing(0)

        # 返回键：只保留箭头（之前箭头 +「返回」文字被按钮裁掉了一半）。
        # 用纯文本 QPushButton，字号放大到与原来「返回」文字等高，并给足固定尺寸。
        back = QPushButton("‹")
        back.setObjectName("backBtn")
        back.setFixedSize(56, 46)
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(lambda: self._goto(0))
        h.addWidget(back)

        t = QLabel(title_text)
        t.setObjectName("subTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(t, 1)

        # 右侧放一个与返回键等宽的占位，标题才能真正居中（而不是偏右）
        spacer = QWidget()
        spacer.setFixedWidth(56)
        h.addWidget(spacer)
        v.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        v.addWidget(scroll, 1)
        body = QWidget()
        scroll.setWidget(body)
        body_v = QVBoxLayout(body)
        body_v.setContentsMargins(14, 12, 14, 16)
        body_v.setSpacing(14)
        return page, body_v

    def _make_card(self, title: str | None = None) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(10)
        if title:
            t = QLabel(title)
            t.setObjectName("sectionTitle")
            v.addWidget(t)
        return card

    def _form_label(self, text: str) -> QLabel:
        """表单左侧标签：固定与右侧控件等高（40px）并垂直居中，
        否则标签文字会比输入框里的文字偏高，看着不齐。"""
        lbl = QLabel(text)
        lbl.setObjectName("formLabel")
        lbl.setMinimumHeight(40)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return lbl

    def _make_save_button(self) -> QPushButton:
        btn = QPushButton(self.T("save"))
        btn.setObjectName("primary")
        btn.setMinimumHeight(46)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self._save)
        self._save_btns.append(btn)
        return btn

    def _flash_saved(self, msg: str | None = None):
        """保存反馈：按钮变绿并显示「已保存 ✓」，1.4 秒后复原。"""
        text = msg or self.T("saved")
        for b in self._save_btns:
            b.setText(text)
            b.setProperty("ok", "1")
            b.style().unpolish(b)
            b.style().polish(b)
        QTimer.singleShot(1400, self._restore_save_btns)

    def _restore_save_btns(self):
        for b in self._save_btns:
            b.setText(self.T("save"))
            b.setProperty("ok", "")
            b.style().unpolish(b)
            b.style().polish(b)

    # ---- 设备设置 ---------------------------------------------------------- #
    def _build_device_page(self):
        page, body = self._make_subpage(self.T("menu_device"))
        card = self._make_card()
        form = QFormLayout()
        form.setSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.f_name = QLineEdit(self.settings.get("device_name", "SteamDeck"))
        self.f_name.setPlaceholderText(self.T("name_ph"))
        form.addRow(self._form_label(self.T("name_label")), self.f_name)

        self.f_append = QCheckBox(self.T("append_host"))
        self.f_append.setChecked(bool(self.settings.get("append_hostname")))

        # 实时预览：勾选「追加主机名」后，iOS 屏幕镜像列表里实际出现的完整广播名。
        # 与上方复选框放进同一个竖向容器，提示紧贴复选框、并与上下行间距保持一致。
        self.bc_preview = QLabel()
        # 走 QSS 的 QLabel#hint（随主题变色），不写死颜色
        self.bc_preview.setObjectName("hint")
        _append_box = QWidget()
        _append_v = QVBoxLayout(_append_box)
        _append_v.setContentsMargins(0, 0, 0, 0)
        _append_v.setSpacing(4)
        _append_v.addWidget(self.f_append)
        _append_v.addWidget(self.bc_preview)
        form.addRow(self._form_label(""), _append_box)
        self.f_name.textChanged.connect(self._update_broadcast_preview)
        self.f_append.toggled.connect(self._update_broadcast_preview)
        # 建页面时立刻填一次预览：否则要等用户输入或勾选才出现，刚打开时是一行空白
        self._update_broadcast_preview()

        self.f_autostart = QCheckBox(self.T("autostart"))
        self.f_autostart.setChecked(bool(self.settings.get("autostart")))
        form.addRow(self._form_label(""), self.f_autostart)

        # 可选 PIN：与上方「追加主机名 / 开机自启」同款排版（左侧空标签 + 勾选）
        self.f_pin_enable = QCheckBox(self.T("pin_enable"))
        self.f_pin_enable.setChecked(bool(self.settings.get("pin_enabled")))
        self._pin_code = "".join(ch for ch in str(self.settings.get("pin_code") or "") if ch.isdigit())[:4]
        self.f_pin_label = QLabel()
        self.f_pin_label.setObjectName("hint")
        self.f_pin_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.btn_pin_regen = QPushButton(self.T("pin_regen"))
        self.btn_pin_regen.setMinimumHeight(32)
        self.btn_pin_regen.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pin_regen.clicked.connect(self._regen_pin)
        self.f_pin_hint = QLabel(self.T("pin_hint"))
        self.f_pin_hint.setObjectName("hint")
        self.f_pin_hint.setWordWrap(True)
        _pin_box = QWidget()
        _pin_v = QVBoxLayout(_pin_box)
        _pin_v.setContentsMargins(0, 0, 0, 0)
        _pin_v.setSpacing(4)
        _pin_v.addWidget(self.f_pin_enable)
        _pin_row = QWidget()
        _pin_h = QHBoxLayout(_pin_row)
        _pin_h.setContentsMargins(0, 0, 0, 0)
        _pin_h.setSpacing(8)
        _pin_h.addWidget(self.f_pin_label, 1)
        _pin_h.addWidget(self.btn_pin_regen, 0)
        _pin_v.addWidget(_pin_row)
        _pin_v.addWidget(self.f_pin_hint)
        form.addRow(self._form_label(""), _pin_box)
        self.f_pin_enable.toggled.connect(self._on_pin_enable_toggled)
        self._refresh_pin_widgets()

        self._lang_combo = QComboBox()
        self._lang_combo.addItem(self.T("lang_auto"), "auto")
        for code, label in i18n.LANGS:
            self._lang_combo.addItem(label, code)
        stored = self.settings.get("language", "auto")
        idx = self._lang_combo.findData(stored)
        if idx < 0:
            idx = self._lang_combo.findData(self.lang)
        self._lang_combo.setCurrentIndex(max(0, idx))
        self._lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        form.addRow(self._form_label(self.T("language")), self._lang_combo)

        # 界面主题：auto = 跟随系统深浅色，也可手动锁定深色 / 浅色；切换即时生效
        self._theme_combo = QComboBox()
        for _v in cfg.THEME_CHOICES:
            self._theme_combo.addItem(self.T("theme_" + _v), _v)
        _ti = self._theme_combo.findData(self.settings.get("theme", "auto"))
        self._theme_combo.setCurrentIndex(max(0, _ti))
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        form.addRow(self._form_label(self.T("theme")), self._theme_combo)

        card.layout().addLayout(form)
        body.addWidget(card)
        body.addWidget(self._make_save_button())
        body.addStretch(1)
        return page


    @staticmethod
    def _random_pin() -> str:
        """UxPlay 固定 PIN 合法范围 [0001:9999]；用 1000–9999，避开 0000/前导零。"""
        import random
        return f"{random.randint(1000, 9999)}"

    def _refresh_pin_widgets(self) -> None:
        on = bool(getattr(self, "f_pin_enable", None) and self.f_pin_enable.isChecked())
        pin_ok = on and len(getattr(self, "_pin_code", "") or "") == 4
        if getattr(self, "f_pin_label", None) is not None:
            if pin_ok:
                self.f_pin_label.setText(self.T("pin_current", pin=self._pin_code))
                self.f_pin_label.show()
            else:
                self.f_pin_label.setText("")
                self.f_pin_label.hide()
        if getattr(self, "btn_pin_regen", None) is not None:
            self.btn_pin_regen.setEnabled(pin_ok)
            self.btn_pin_regen.setVisible(pin_ok)
        if getattr(self, "f_pin_hint", None) is not None:
            self.f_pin_hint.setVisible(on)
        try:
            self._refresh_home_pin()
        except Exception:
            pass

    def _refresh_home_pin(self) -> None:
        lbl = getattr(self, "home_pin_label", None)
        if lbl is None:
            return
        on = bool(self.settings.get("pin_enabled")) if getattr(self, "f_pin_enable", None) is None else self.f_pin_enable.isChecked()
        pin = "".join(ch for ch in str(getattr(self, "_pin_code", "") or self.settings.get("pin_code") or "") if ch.isdigit())[:4]
        if on and len(pin) == 4:
            lbl.setText(self.T("home_pin", pin=pin))
            lbl.show()
        else:
            lbl.setText("")
            lbl.hide()

    def _on_pin_enable_toggled(self, checked: bool) -> None:
        if checked:
            if len(getattr(self, "_pin_code", "") or "") != 4 or self._pin_code == "0000":
                self._pin_code = self._random_pin()
        self._refresh_pin_widgets()
        try:
            self._refresh_home_pin()
        except Exception:
            pass
        # 立刻落盘；若正在接收会因 RUNTIME_KEYS 变化自动重启
        self._save()

    def _regen_pin(self) -> None:
        if not self.f_pin_enable.isChecked():
            return
        self._pin_code = self._random_pin()
        self._refresh_pin_widgets()
        try:
            self._refresh_home_pin()
        except Exception:
            pass
        self._save()  # 落盘并在接收中时重启，保证 UxPlay -pin 与界面一致

    def _on_lang_changed(self, _idx: int):
        if self._rebuilding or self._lang_combo is None:
            return
        code = self._lang_combo.currentData() or "auto"
        effective = i18n.resolve_lang(code)
        if code == self.settings.get("language", "auto") and effective == self.lang:
            return
        self.lang = effective
        self.settings["language"] = code
        try:
            cfg.save_settings(self.settings)
        except Exception:
            pass
        self._rebuild_ui()
        self._flash_saved()

    def _on_theme_changed(self, _idx: int):
        if self._rebuilding or self._theme_combo is None:
            return
        val = self._theme_combo.currentData() or "auto"
        if val == self.settings.get("theme", "auto"):
            return
        self.settings["theme"] = val
        try:
            cfg.save_settings(self.settings)
        except Exception:
            pass
        # 纯外观设置：不涉及 uxplay 参数，不必重启接收，直接重刷样式即时生效
        self._apply_qss()
        self._flash_saved()

    def _on_system_theme_changed(self, *_a):
        """系统深浅色变了：只有「自动」模式才跟着变，手动锁定了就不动。"""
        if (self.settings.get("theme") or "auto").strip().lower() != "auto":
            return
        self._apply_qss()

    # ---- 视频与渲染 -------------------------------------------------------- #
    def _build_video_page(self):
        page, body = self._make_subpage(self.T("menu_video"))
        card = self._make_card()
        form = QFormLayout()
        form.setSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # 帧率：固定 30 / 60 两档（自由填值会让部分 App 的投屏会话卡死）
        self.f_fps = QComboBox()
        for v in cfg.FPS_CHOICES:
            self.f_fps.addItem(f"{v} fps", v)
        cur = int(self.settings.get("fps", 30) or 30)
        i = self.f_fps.findData(cur)
        self.f_fps.setCurrentIndex(max(0, i))
        form.addRow(self._form_label(self.T("fps")), self.f_fps)

        self.f_sink = QComboBox()
        self.f_sink.addItems(cfg.VIDEO_SINKS)
        self.f_sink.setCurrentText(self.settings.get("video_sink", "ximagesink"))
        form.addRow(self._form_label(self.T("video_sink")), self.f_sink)

        self.f_res = QComboBox()
        for _v in cfg.RESOLUTION_CHOICES:
            # auto 显示为「自动」（随语言变化），其余分辨率原样显示；
            # 用 userData 存原始值，保证设置读写与旧版本兼容。
            self.f_res.addItem(self.T("res_auto") if _v == "auto" else _v, _v)
        i = self.f_res.findData(self.settings.get("resolution", "auto"))
        self.f_res.setCurrentIndex(max(0, i))
        form.addRow(self._form_label(self.T("resolution")), self.f_res)

        self.f_mode = QComboBox()
        for value, key in (("auto", "dm_auto"), ("fullscreen", "dm_full"), ("window", "dm_win")):
            self.f_mode.addItem(self.T(key), value)
        i = self.f_mode.findData(self.settings.get("display_mode", "auto"))
        self.f_mode.setCurrentIndex(max(0, i))
        form.addRow(self._form_label(self.T("display_mode")), self.f_mode)

        self.f_decoder = QComboBox()
        for value, key in (("auto", "dec_auto"), ("sw", "dec_sw"),
                           ("vaapi", "dec_vaapi"), ("v4l2", "dec_v4l2")):
            self.f_decoder.addItem(self.T(key), value)
        i = self.f_decoder.findData(self.settings.get("decoder", "auto"))
        self.f_decoder.setCurrentIndex(max(0, i))
        form.addRow(self._form_label(self.T("decoder")), self.f_decoder)

        self.f_keep = QCheckBox(self.T("keep_window"))
        self.f_keep.setChecked(bool(self.settings.get("keep_window")))
        form.addRow(self._form_label(""), self.f_keep)

        self.f_legacy = QCheckBox(self.T("legacy_ports"))
        self.f_legacy.setChecked(bool(self.settings.get("legacy_ports")))
        form.addRow(self._form_label(""), self.f_legacy)

        self.f_anti_sleep = QCheckBox(self.T("anti_sleep"))
        self.f_anti_sleep.setChecked(bool(self.settings.get("anti_sleep", True)))
        form.addRow(self._form_label(""), self.f_anti_sleep)

        self.f_audio_sync = QCheckBox(self.T("audio_sync"))
        self.f_audio_sync.setChecked((self.settings.get("audio_sync") or "off") != "off")
        self.f_audio_sync.setToolTip(self.T("audio_sync_hint"))
        form.addRow(self._form_label(""), self.f_audio_sync)

        self.f_extra = QLineEdit(self.settings.get("extra", ""))
        self.f_extra.setPlaceholderText(self.T("extra_ph"))
        form.addRow(self._form_label(self.T("extra")), self.f_extra)

        card.layout().addLayout(form)
        body.addWidget(card)

        row = QWidget()
        rh = QHBoxLayout(row)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.setSpacing(10)
        btn_rec = QPushButton(self.T("restore_rec"))
        btn_rec.setMinimumHeight(44)
        btn_rec.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_rec.clicked.connect(self._restore_recommended)
        rh.addWidget(btn_rec, 1)
        body.addWidget(row)

        body.addWidget(self._make_save_button())
        body.addStretch(1)
        return page

    def _restore_recommended(self):
        """一键回到最稳组合：30 fps + ximagesink + 自动分辨率/解码/显示模式。
        改设置后一旦出现「能连上但没画面」，用这个立刻回退。"""
        i = self.f_fps.findData(30)
        self.f_fps.setCurrentIndex(max(0, i))
        self.f_sink.setCurrentText("ximagesink")
        i = self.f_res.findData("auto")
        self.f_res.setCurrentIndex(max(0, i))
        i = self.f_mode.findData("auto")
        self.f_mode.setCurrentIndex(max(0, i))
        i = self.f_decoder.findData("auto")
        self.f_decoder.setCurrentIndex(max(0, i))
        self._log("info", "已恢复推荐画质：30 fps + ximagesink + 自动")
        self._save()

    # ---- 检查与日志 -------------------------------------------------------- #
    def _build_check_page(self):
        page, body = self._make_subpage(self.T("menu_check"))

        # 一键检查运行环境：置顶，结果合并到下方状态框
        check_actions = QWidget()
        check_ah = QHBoxLayout(check_actions)
        check_ah.setContentsMargins(0, 0, 0, 0)
        check_ah.setSpacing(10)
        self.btn_check_env = QPushButton(self.T("btn_check_env"))
        self.btn_check_env.setMinimumHeight(44)
        self.btn_check_env.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_check_env.clicked.connect(self._check_env)
        check_ah.addWidget(self.btn_check_env, 1)
        body.addWidget(check_actions)

        # 容器运行方式：仅保留进入方式选择（容器模式固定开启），不再用「uxplay 与容器」标题
        form = QFormLayout()
        form.setSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.f_dbox_method = QComboBox()
        self.f_dbox_method.addItem(self.T("method_enter"), "enter")
        self.f_dbox_method.addItem(self.T("method_podman"), "podman")
        cur = (self.settings.get("distrobox_method") or "enter").strip()
        i = self.f_dbox_method.findData(cur)
        self.f_dbox_method.setCurrentIndex(max(0, i))
        form.addRow(self._form_label(self.T("dbox_method")), self.f_dbox_method)

        body.addLayout(form)

        # 运行环境状态：默认空，仅在点「一键检查运行环境」后显示结果（不再常驻「已就绪」）
        self.env_status = QLabel("")
        self.env_status.setObjectName("hint")
        self.env_status.setWordWrap(True)
        body.addWidget(self.env_status)

        env_actions = QWidget()
        env_ah = QHBoxLayout(env_actions)
        env_ah.setContentsMargins(0, 0, 0, 0)
        env_ah.setSpacing(10)
        self.btn_install_env = QPushButton(self.T("btn_install_env"))
        self.btn_install_env.setMinimumHeight(44)
        self.btn_install_env.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_install_env.clicked.connect(self._install_env)
        self.btn_uninstall = QPushButton(self.T("btn_uninstall"))
        self.btn_uninstall.setObjectName("danger")
        self.btn_uninstall.setMinimumHeight(44)
        self.btn_uninstall.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_uninstall.clicked.connect(self._uninstall_env)
        env_ah.addWidget(self.btn_install_env, 1)
        env_ah.addWidget(self.btn_uninstall, 1)
        body.addWidget(env_actions)

        env_tip = QLabel(self.T("tip_env"))
        env_tip.setObjectName("hint")
        env_tip.setWordWrap(True)
        body.addWidget(env_tip)

        body.addWidget(self._make_save_button())

        log_card = self._make_card(self.T("log_title"))
        self.log = QTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(220)
        log_card.layout().addWidget(self.log)
        body.addWidget(log_card, 1)

        actions2 = QWidget()
        ah2 = QHBoxLayout(actions2)
        ah2.setContentsMargins(0, 0, 0, 0)
        ah2.setSpacing(10)
        self.log_path_lbl = QLabel(self.T("log_path_hint", path=str(cfg.LOG_FILE)))
        self.log_path_lbl.setObjectName("hint")
        self.log_path_lbl.setWordWrap(True)
        self.log_path_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.log_path_lbl)
        self.btn_export = QPushButton(self.T("btn_export"))
        self.btn_export.setMinimumHeight(44)
        self.btn_export.clicked.connect(self._export_log)
        self.btn_reset = QPushButton(self.T("btn_reset"))
        self.btn_reset.setObjectName("danger")
        self.btn_reset.setMinimumHeight(44)
        self.btn_reset.clicked.connect(self._reset_settings)
        ah2.addWidget(self.btn_export, 1)
        ah2.addWidget(self.btn_reset, 1)
        body.addWidget(actions2)
        return page

    # ---- 关于 -------------------------------------------------------------- #
    def _build_about_page(self):
        page, body = self._make_subpage(self.T("menu_about"))
        card = self._make_card()
        cv = card.layout()
        v_label = QLabel(f"{self.T('about_version')}  v{APP_VERSION}")
        v_label.setObjectName("bigStatus")
        cv.addWidget(v_label)
        text = QLabel(self.T("about_body"))
        text.setWordWrap(True)
        # 走 QSS 的 QLabel#aboutBody（随主题变色），不再写死深色文字
        text.setObjectName("aboutBody")
        cv.addWidget(text)
        # GitHub 仓库跳转：用 QPushButton（图标 + 链接文字），点击必定触发。
        # 之前用 QLabel 超链接 + linkActivated 在部分桌面环境下不会触发，故改用按钮。
        gh_row = QWidget()
        # 关键：全局 QSS 的 `QWidget { background: #1a1b1e }`（近黑）会作用到这个普通容器，
        # 不显式设透明，就会在卡片灰底（#25272b）上露出一块黑底，和周边不协调。
        gh_row.setStyleSheet("background: transparent;")
        # 不让这行在竖向布局里被拉伸成整行宽（否则图标与链接右侧会出现一长条空白）
        gh_row.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred))
        gh_h = QHBoxLayout(gh_row)
        gh_h.setContentsMargins(0, 0, 0, 0)
        gh_h.setSpacing(6)
        _gh_url = "https://github.com/mclhm8182/airplay-deck"
        _gh_btn = QPushButton()
        _gh_btn.setObjectName("ghLink")
        _gh_svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" height="16" width="16" viewBox="0 0 16 16">'
            '<path fill="#cfd1d5" d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17'
            '-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88'
            '-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0'
            '-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15'
            ' 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83'
            '-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49'
            ' 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>'
        )
        try:
            from PySide6.QtSvg import QSvgRenderer
            _r = QSvgRenderer(bytearray(_gh_svg.encode("utf-8")))
            _pm = QPixmap(16, 16)
            _pm.fill(Qt.GlobalColor.transparent)
            _p = QPainter(_pm)
            _r.render(_p)
            _p.end()
            _gh_btn.setIcon(QIcon(_pm))
        except Exception:
            pass
        _gh_btn.setText("github.com/mclhm8182/airplay-deck")
        _gh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _gh_btn.setToolTip(_gh_url + "\n" + self.T("open_hint"))
        # 样式交给 _apply_qss 里的 QPushButton#ghLink 规则（随主题变色），
        # 不再在这里写死颜色，否则切到浅色主题会残留深色链接。
        _gh_btn.clicked.connect(lambda: self._open_url(_gh_url))
        # 右键兜底：直接复制链接并给一次可见提示
        _gh_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        _gh_btn.customContextMenuRequested.connect(lambda _pos: self._copy_url(_gh_url))
        gh_h.addWidget(_gh_btn)
        # 显式「复制」按钮：Steam Deck 用手柄/触屏时右键很不方便，给一个一定能用的入口——
        # 浏览器起不来（例如游戏模式）时，至少能把地址复制走。
        _gh_copy = QPushButton(self.T("copy_link"))
        _gh_copy.setObjectName("ghCopy")
        _gh_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        _gh_copy.setToolTip(self.T("copy_link_tip"))
        _gh_copy.clicked.connect(lambda: self._copy_url(_gh_url))
        gh_h.addWidget(_gh_copy)
        cv.addWidget(gh_row)
        body.addWidget(card)
        body.addStretch(1)
        return page

    # ---- 外链：打开 / 复制 ------------------------------------------------- #
    def _open_url(self, url: str) -> None:
        """打开外链，带多级兜底。

        QDesktopServices 在没有浏览器 / 没有 URL 处理器的环境（例如 Steam 游戏模式）
        会**静默失败**（点了没反应），所以失败后再依次尝试 xdg-open / gio / kde-open；
        全都起不来就把链接复制到剪贴板并提示，保证用户至少能拿到地址。
        """
        try:
            if QDesktopServices.openUrl(QUrl(url)):
                return
        except Exception:
            pass
        # 用 Popen 而不是 run(等待)：不阻塞界面；命令不存在会直接抛错，继续下一个。
        for prog, args in (("xdg-open", (url,)), ("gio", ("open", url)),
                           ("kde-open", (url,)), ("gnome-open", (url,))):
            try:
                subprocess.Popen([prog, *args], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return
            except Exception:
                continue
        self._copy_url(url)

    def _copy_url(self, url: str) -> None:
        """复制链接到剪贴板，并给一次可见反馈。"""
        try:
            QApplication.clipboard().setText(url)
        except Exception:
            return
        try:
            self._flash_saved(self.T("link_copied"))
        except Exception:
            pass

    # ---- QSS ---------------------------------------------------------------- #
    def _resolve_theme(self) -> str:
        """auto = 跟随系统深浅色；系统不给答案时回退深色（Steam Deck 本来就是深色）。"""
        val = (self.settings.get("theme") or "auto").strip().lower()
        if val in ("dark", "light"):
            return val
        try:
            cs = QApplication.styleHints().colorScheme()
            if cs == Qt.ColorScheme.Light:
                return "light"
            if cs == Qt.ColorScheme.Dark:
                return "dark"
        except Exception:
            pass
        return "dark"

    def _apply_qss(self) -> None:
        p = dict(QSS_PALETTE[self._resolve_theme()])
        p.setdefault("check_bg", p.get("panel", "#333"))
        p.setdefault("check_border", p.get("input_border", "#666"))
        self.setStyleSheet("""
            QMainWindow, QWidget { background: %(bg)s; color: %(text)s;
                font-family: 'Noto Sans CJK SC', 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
                font-size: 13px; }
            QLabel { background: transparent; color: %(text)s; }
            QLabel#appTitle { font-size: 19px; font-weight: 600;
                color: %(text)s; padding: 18px 0 4px 0; letter-spacing: 0.5px; }
            QLabel#sectionTitle { color: %(section)s; font-size: 12px; font-weight: 600;
                padding-bottom: 2px; letter-spacing: 0.3px; }
            QLabel#hint { color: %(text_dim)s; font-size: 11px; }
            QLabel#deviceHint { color: %(text)s; font-size: 13px; background: transparent; }
            QLabel#aboutBody { color: %(text_mid)s; font-size: 12px; line-height: 1.7; }
            QLabel#bigStatus { font-size: 17px; font-weight: 600; color: %(text)s; }
            QFrame { border: none; }
            QFrame#card { background: %(panel)s; border: 1px solid %(border)s; border-radius: 12px; }
            QFrame#sep { background: %(sep)s; max-height: 1px; min-height: 1px; border: none; }
            QWidget#page { background: %(bg)s; }
            QWidget#subHeader { background: %(subheader)s; border-bottom: 1px solid %(sep)s; }
            QWidget#menuRow { background: transparent; }
            QWidget#menuRow:hover { background: %(row_hover)s; }
            QWidget#menuRow QLabel#menuRowText { font-size: 15px; color: %(text)s; background: transparent; }
            QWidget#menuRow QLabel#menuRowChev { color: %(text_dim)s; font-size: 28px; background: transparent; }
            QLabel#subTitle { font-size: 16px; font-weight: 600; color: %(text)s; }
            /* 表单左侧标签：与右侧输入框等高（40px）+ 垂直居中，保证文字齐平 */
            QLabel#formLabel { font-size: 13px; color: %(text_mid)s;
                padding-right: 4px; background: transparent; }
            /* 返回键只有箭头：字号放大到与「返回」文字等高，padding 归零避免被裁切 */
            QPushButton#backBtn { background: transparent; border: none;
                color: %(text)s; font-size: 34px; font-weight: 400;
                padding: 0 0 4px 0; }
            QPushButton#backBtn:hover { color: %(accent)s; }

            QLineEdit, QComboBox, QTextEdit {
                background: %(input_bg)s; color: %(text)s;
                border: 1px solid %(input_border)s; border-radius: 8px;
                padding: 8px 10px; min-height: 22px;
                selection-background-color: %(accent)s; }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus { border-color: %(accent)s; }
            QComboBox::drop-down { border: none; width: 24px; }
            QComboBox QAbstractItemView { background: %(panel)s; color: %(text)s;
                selection-background-color: %(accent)s; border: 1px solid %(input_border)s;
                border-radius: 6px; padding: 6px; }
            QTextEdit#log { background: %(log_bg)s; color: %(log_text)s;
                font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 11px; }

            QCheckBox { color: %(text)s; spacing: 10px; min-height: 26px; }
            QCheckBox::indicator { width: 20px; height: 20px; border-radius: 5px;
                border: 1px solid %(check_border)s; background: %(check_bg)s; }
            QCheckBox::indicator:hover { border-color: %(accent)s; }
            QCheckBox::indicator:checked { background: %(accent)s; border-color: %(accent)s; }

            QPushButton { background: %(btn_bg)s; color: %(text)s;
                border: 1px solid %(btn_border)s; border-radius: 8px;
                padding: 9px 16px; font-size: 13px; min-height: 26px; }
            QPushButton:hover { background: %(btn_hover)s; border-color: %(btn_border_hover)s; }
            QPushButton:pressed { background: %(btn_pressed)s; }
            QPushButton:disabled { background: %(dis_bg)s; color: %(dis_text)s;
                border-color: %(dis_border)s; }
            QPushButton#primary { background: %(accent)s; color: white; border: none;
                padding: 10px 18px; font-size: 14px; font-weight: 600; border-radius: 10px; }
            QPushButton#primary:hover { background: %(accent_hover)s; }
            QPushButton#primary[ok="1"] { background: %(ok)s; }
            QPushButton#danger { color: %(danger)s; border-color: %(danger_border)s; }
            QPushButton#danger:hover { background: %(danger_hover)s; }
            /* 关于页仓库链接：底色透明，跟着卡片走（否则会在卡片上糊出一块异色底） */
            QPushButton#ghLink { background: transparent; border: none;
                color: %(link)s; font-size: 12px; padding: 2px 4px;
                text-align: left; spacing: 6px; }
            QPushButton#ghLink:hover { color: %(link_hover)s; text-decoration: underline; }
            QPushButton#ghCopy { background: transparent; border: 1px solid %(btn_border)s;
                color: %(text_dim)s; font-size: 11px; padding: 2px 8px; border-radius: 6px; }
            QPushButton#ghCopy:hover { color: %(link_hover)s; border-color: %(accent)s; }

            QMenu { background: %(panel)s; color: %(text)s;
                border: 1px solid %(border)s; border-radius: 8px; padding: 4px; }
            QMenu::item { padding: 6px 20px; border-radius: 5px; }
            QMenu::item:selected { background: %(accent)s; color: %(menu_sel_text)s; }

            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background: transparent; width: 8px; }
            QScrollBar::handle:vertical { background: %(scroll)s; border-radius: 4px;
                min-height: 34px; }
            QScrollBar::handle:vertical:hover { background: %(scroll_hover)s; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """ % p)

    def _update_broadcast_preview(self) -> None:
        """设置页实时预览：iOS 屏幕镜像列表里实际出现的完整广播名。"""
        name = self.f_name.text().strip() or "SteamDeck"
        full = full_device_name(name, self.f_append.isChecked())
        try:
            self.bc_preview.setText(self.T("append_host_preview").replace("{name}", full))
        except Exception:
            self.bc_preview.setText(full)

    def _load_ui_from_settings(self) -> None:
        s = self.settings
        self.f_name.setText(s.get("device_name", "SteamDeck"))
        self.f_append.setChecked(bool(s.get("append_hostname")))
        self.f_autostart.setChecked(bool(s.get("autostart")))
        if getattr(self, "f_pin_enable", None) is not None:
            self._pin_code = "".join(ch for ch in str(s.get("pin_code") or "") if ch.isdigit())[:4]
            self.f_pin_enable.blockSignals(True)
            self.f_pin_enable.setChecked(bool(s.get("pin_enabled")))
            self.f_pin_enable.blockSignals(False)
            if self.f_pin_enable.isChecked() and len(self._pin_code) != 4:
                self._pin_code = self._random_pin()
            self._refresh_pin_widgets()
        i = self.f_fps.findData(int(s.get("fps", 30) or 30))
        self.f_fps.setCurrentIndex(max(0, i))
        self.f_sink.setCurrentText(s.get("video_sink", "ximagesink"))
        i = self.f_res.findData(s.get("resolution", "auto"))
        self.f_res.setCurrentIndex(max(0, i))
        i = self.f_mode.findData(s.get("display_mode", "auto"))
        self.f_mode.setCurrentIndex(max(0, i))
        i = self.f_decoder.findData(s.get("decoder", "auto"))
        self.f_decoder.setCurrentIndex(max(0, i))
        self.f_keep.setChecked(bool(s.get("keep_window")))
        self.f_legacy.setChecked(bool(s.get("legacy_ports")))
        self.f_anti_sleep.setChecked(bool(s.get("anti_sleep", True)))
        self.f_audio_sync.setChecked((s.get("audio_sync") or "off") != "off")
        self.f_extra.setText(s.get("extra", ""))
        i = self.f_dbox_method.findData((s.get("distrobox_method") or "enter").strip())
        self.f_dbox_method.setCurrentIndex(max(0, i))
        if self._lang_combo is not None:
            i = self._lang_combo.findData(self.lang)
            self._lang_combo.setCurrentIndex(max(0, i))
        if self._theme_combo is not None:
            i = self._theme_combo.findData(s.get("theme", "auto"))
            self._theme_combo.setCurrentIndex(max(0, i))
        try:
            self.device_hint.setText(
                self.T("device_prefix")
                + full_device_name(
                    s.get("device_name", "SteamDeck"),
                    bool(s.get("append_hostname")),
                )
            )
        except Exception:
            pass
        try:
            self._update_broadcast_preview()
        except Exception:
            pass

    # ---- 托盘 --------------------------------------------------------------- #
    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self)
        if self._icon_path:
            self.tray.setIcon(QIcon(self._icon_path))
        menu = QMenu()
        act_show = menu.addAction(self.T("tray_show"))
        act_show.triggered.connect(self._show_window)
        act_stop = menu.addAction(self.T("tray_stop"))
        act_stop.triggered.connect(lambda: self._stop() if self.launcher else None)
        act_quit = menu.addAction(self.T("tray_quit"))
        act_quit.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_window()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
        )
        self.tray.show()

    # ---- 启停 --------------------------------------------------------------- #
    def _on_toggle_switch(self, on: bool):
        if on:
            self._start()
        else:
            self._stop()
        try:
            from core import keepalive as _ka
            _ka.restore_display_defaults(log=lambda lvl, msg: self._log(lvl, msg))
        except Exception:
            pass

    def _will_takeover(self) -> bool:
        """本次接收是否隐藏主窗口（旧版游戏模式用，已废止，恒为 False）。

        历史：游戏模式下 gamescope 全屏呈现 uxplay 窗口、看不到任务栏，曾靠「隐藏主窗口 +
        顶部置顶悬浮控制条」给用户留返回入口。用户 2026-09-11 明确说**不要那条悬浮条**：

            「游戏模式下我觉得也不需要那个悬浮条，如果能正常投屏直接投屏画面就好，
              投屏结束就退出回到 app 主界面」

        所以现在两种模式一视同仁：主窗口不隐藏、不出悬浮条；uxplay 的全屏窗口盖在上面
        就是画面本体；投屏一结束（客户端断开 / 出错）自动把主窗口拉回前台，见 `_on_status`。
        """
        return False

    def _start(self):
        if not self._ensure_env_ready():
            return
        self._save(silent=True)  # 启动前先把当前表单落盘
        def _log_from_worker(lvl, msg):
            MainWindow._append_file_log(lvl, msg)
            self.bridge.log_signal.emit(lvl, msg)
        self.launcher = launcher.Launcher(
            self.settings,
            log_cb=_log_from_worker,
            status_cb=lambda st: self.bridge.status_signal.emit(st),
        )
        try:
            self._log_pin_diagnostics("start_receive")
        except Exception:
            pass
        self.launcher.start()
        self.toggle.setChecked(True)
        # 不再隐藏主窗口、不再显示悬浮控制条（见 _will_takeover 的说明）。
        # uxplay 的全屏窗口会盖在主窗口上显示画面；断开后由 _on_status 把主窗口拉回前台。
        self._was_connected = False

    def _stop(self):
        if self.launcher:
            self.launcher.stop()
            self.launcher = None
        self.toggle.setChecked(False)

    def _restart(self):
        """设置变更后重启 uxplay 让新参数立刻生效。"""
        was_on = self.launcher is not None
        if was_on:
            self.launcher.stop()
            self.launcher = None
        self.toggle.setChecked(True)
        self._start()

    def _on_app_state_changed(self, state):
        """Game Mode 熄屏再亮后，强制刷新，避免整窗黑死。"""
        try:
            if state != Qt.ApplicationState.ApplicationActive:
                return
        except Exception:
            return
        self._append_file_log("info", "app became active — forcing UI refresh")
        try:
            from core import keepalive as _ka
            _ka.one_shot_wake(log=lambda lvl, m: self._append_file_log(lvl, m))
        except Exception:
            pass
        QTimer.singleShot(50, self._recover_ui_after_wake)

    def _recover_ui_after_wake(self):
        try:
            self.show()
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self.repaint()
            try:
                self._apply_qss()
            except Exception:
                pass
            self.update()
            self._append_file_log("info", "UI refresh after wake done")
        except Exception as e:
            self._append_file_log("error", f"UI refresh after wake failed: {e}")

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---- 回调槽 ------------------------------------------------------------- #
    def _on_log(self, level: str, msg: str):
        # 先落盘再刷新 UI：界面黑屏/控件异常时也不能丢掉日志
        self._append_file_log(level, msg)
        try:
            self._log_buffer.append(f"[{level}] {msg}")
            if len(self._log_buffer) > 500:
                self._log_buffer = self._log_buffer[-500:]
        except Exception:
            pass
        color = {
            "info": "#8b949e", "warn": "#d29922", "error": "#f85149",
            "cmd": "#58a6ff", "exit": "#8b949e", "uxplay": "#c9d1d9",
        }.get(level, "#c9d1d9")
        try:
            self.log.append(f'<span style="color:{color}">[{level}] {msg}</span>')
            if self.log.document().blockCount() > 500:
                self.log.clear()
        except Exception:
            pass

    @staticmethod
    def _append_file_log(level: str, msg: str) -> None:
        """黑屏/强杀前尽可能把日志刷到磁盘。"""
        try:
            from datetime import datetime
            cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = "%s [%s] %s" % (ts, level, msg) + chr(10)
            with open(cfg.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
        except Exception:
            pass

    def _log(self, level: str, msg: str):
        self._on_log(level, msg)

    def _on_status(self, status: str):
        key, color = {
            "idle": ("status_idle", "#888888"),
            "waiting": ("status_waiting", "#d29922"),
            "connected": ("status_connected", "#3fb950"),
            "error": ("status_error", "#f85149"),
        }.get(status, ("status_idle", "#888888"))
        self.status_label.setText(self.T(key))
        self.dot.setStyleSheet(f"color: {color}; font-size: 20px;")
        self.toggle.setChecked(status in ("waiting", "connected"))

        # 「投屏结束就回到 app 主界面」（用户 2026-09-11 要求）。
        # uxplay 的全屏窗口消失后，界面会留在黑屏/桌面，所以这里主动把主窗口拉到前台。
        if status == "connected":
            self._was_connected = True
        elif self._was_connected and status in ("waiting", "idle", "error"):
            self._was_connected = False
            self._show_window()
            self._log("info", "投屏已结束，已切回主界面")

    def _refresh_status(self):
        self.status_label.setText(self.T("status_idle"))
        self.dot.setStyleSheet("color: #888; font-size: 20px;")
        self.toggle.setChecked(False)

    # ---- 设置 --------------------------------------------------------------- #
    def _collect(self) -> dict:
        s = dict(self.settings)
        s["device_name"] = self.f_name.text().strip() or "SteamDeck"
        s["append_hostname"] = self.f_append.isChecked()
        s["autostart"] = self.f_autostart.isChecked()
        if getattr(self, "f_pin_enable", None) is not None:
            s["pin_enabled"] = self.f_pin_enable.isChecked()
            if s["pin_enabled"]:
                pin = "".join(ch for ch in str(getattr(self, "_pin_code", "") or "") if ch.isdigit())[:4]
                if len(pin) != 4:
                    pin = self._random_pin()
                    self._pin_code = pin
                s["pin_code"] = pin
            else:
                s["pin_code"] = "".join(ch for ch in str(getattr(self, "_pin_code", "") or "") if ch.isdigit())[:4]
        s["fps"] = int(self.f_fps.currentData() or 30)
        s["video_sink"] = self.f_sink.currentText()
        s["resolution"] = self.f_res.currentData() or "auto"
        s["display_mode"] = self.f_mode.currentData() or "auto"
        s["decoder"] = self.f_decoder.currentData() or "auto"
        s["keep_window"] = self.f_keep.isChecked()
        s["legacy_ports"] = self.f_legacy.isChecked()
        s["anti_sleep"] = self.f_anti_sleep.isChecked()
        s["audio_sync"] = "auto" if self.f_audio_sync.isChecked() else "off"
        s["extra"] = self.f_extra.text().strip()
        s["distrobox_method"] = self.f_dbox_method.currentData() or "enter"
        if self._lang_combo is not None:
            d = self._lang_combo.currentData()
            s["language"] = "auto" if d == "auto" else i18n.normalize(d or self.lang)
        if self._theme_combo is not None:
            s["theme"] = self._theme_combo.currentData() or "auto"
        return s

    def _save(self, silent: bool = False):
        old = dict(self.settings)
        collected = self._collect()
        if collected.get("pin_enabled"):
            pin = "".join(ch for ch in str(collected.get("pin_code") or "") if ch.isdigit())
            if len(pin) != 4:
                pin = self._random_pin()
                self._pin_code = pin
                collected["pin_code"] = pin
                try:
                    self._refresh_pin_widgets()
                except Exception:
                    pass
            else:
                collected["pin_code"] = pin
        self.settings = collected
        try:
            cfg.save_settings(self.settings)
        except Exception as e:
            if not silent:
                self._flash_saved(str(e))
            self._log("error", str(e))
            return
        try:
            if self.settings.get("pin_enabled"):
                self._log_pin_diagnostics("save")
        except Exception:
            pass
        try:
            self.device_hint.setText(
                self.T("device_prefix")
                + full_device_name(
                    self.settings.get("device_name", "SteamDeck"),
                    bool(self.settings.get("append_hostname")),
                )
            )
        except Exception:
            pass

        try:
            self._refresh_home_pin()
        except Exception:
            pass

        if silent:
            return

        changed = [k for k in RUNTIME_KEYS if old.get(k) != self.settings.get(k)]
        if changed and self.launcher is not None:
            self._log("info", self.T("log_saved") + f" ({', '.join(sorted(changed))})")
            self._flash_saved(self.T("saved_restart"))
            QTimer.singleShot(400, self._restart)
        else:
            self._log("info", self.T("log_saved"))
            self._flash_saved()


    def _add_to_steam(self):
        """主页一键：把当前 AppImage 写入 Steam 非 Steam 游戏库。"""
        try:
            ok, code = steam_shortcut.add_to_steam(APP_TITLE)
        except Exception as e:
            QMessageBox.warning(
                self,
                self.T("btn_add_steam"),
                self.T("steam_add_fail", reason=str(e)),
            )
            self._log("error", f"add_to_steam: {e}")
            return
        if ok and code in ("added", "steamos_ok"):
            QMessageBox.information(self, self.T("btn_add_steam"), self.T("steam_add_ok"))
            self._log("info", f"add_to_steam: {code}")
            return
        if ok and code == "updated":
            QMessageBox.information(self, self.T("btn_add_steam"), self.T("steam_add_updated"))
            self._log("info", "add_to_steam: updated")
            return
        if ok and code == "already":
            QMessageBox.information(self, self.T("btn_add_steam"), self.T("steam_add_already"))
            self._log("info", "add_to_steam: already")
            return
        reason_map = {
            "steam_not_found": self.T("steam_reason_not_found"),
            "no_userdata": self.T("steam_reason_not_found"),
            "steamos_failed": self.T("steam_reason_not_found"),
        }
        if code.startswith("no_target"):
            reason = self.T("steam_reason_no_target")
        else:
            reason = reason_map.get(code, code)
        QMessageBox.warning(
            self,
            self.T("btn_add_steam"),
            self.T("steam_add_fail", reason=reason),
        )
        self._log("error", f"add_to_steam failed: {code}")

    def _check_env(self):
        """一键检查运行环境：uxplay 可用性 + 容器就绪状态，结果合并到状态框。"""
        container = (self.settings.get("distrobox_container") or "uxplay-env").strip()
        binpath = (self.settings.get("uxplay_path") or "").strip() or "uxplay"
        where = f"容器 {container}"
        cmd = ["distrobox", "enter", container, "--", binpath, "-v"]

        # 1) uxplay 可用性
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            self._log("error", "找不到 distrobox 命令。请先安装 distrobox，再使用本程序。")
            if hasattr(self, "env_status"):
                self.env_status.setText(self._env_status_text())
            return
        except Exception as e:
            self._log("error", f"检查 uxplay 失败: {e}")
            if hasattr(self, "env_status"):
                self.env_status.setText(self._env_status_text())
            return
        if r.returncode != 0:
            err = ""
            for s in (r.stderr, r.stdout):
                if s and s.strip():
                    err = s.strip().splitlines()[-1]
            tail = f"：{err}" if err else ""
            self._log("error",
                      f"在 {where} 内检查 uxplay 失败（退出码 {r.returncode}）{tail}。"
                      f"请确认该容器已装 uxplay（`distrobox enter {container} -- which uxplay`）。")
            if hasattr(self, "env_status"):
                self.env_status.setText(self._env_status_text())
            return
        ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else "未知版本"
        self._log("info", f"uxplay 可用（{where}）：{ver}")

        # 2) 容器就绪状态
        if cman.is_ready(container):
            self._log("info", f"运行环境已就绪（容器 {container}）")
        else:
            self._log("warn", f"容器 {container} 未就绪，请先在上方「安装 / 重建运行环境」。")
        # 3) 容器内能否连上宿主 mDNS（搜不到设备基本都出在这一步）
        if cman.container_exists(container):
            if avahi.container_avahi_ok(container):
                self._log("info", "容器内可连上宿主机 mDNS 服务（avahi），设备应能被搜到")
            else:
                self._log("warn", "容器内连不上宿主机 mDNS（avahi）—— 手机/电脑会搜不到设备。"
                                  "「导出日志」里已附带完整诊断，发我即可定位。")
            if avahi.ensure_avahi():
                self._log("info", "宿主机 avahi-daemon 正在运行")
            else:
                self._log("warn", "未能确认宿主机 avahi-daemon 在运行")
        if hasattr(self, "env_status"):
            self.env_status.setText(self._env_status_text())

    def _export_log(self):
        try:
            import json as _json
            from datetime import datetime as _dt
            downloads = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DownloadLocation
            )
            if not downloads:
                downloads = os.path.expanduser("~/Downloads")
            os.makedirs(downloads, exist_ok=True)
            ts = _dt.now().strftime("%Y%m%d-%H%M%S")
            path = os.path.join(downloads, f"airplay-deck-debug-{ts}.txt")
            gm = session.is_gamemode()
            try:
                args_preview = " ".join(uxplay_args.build_args(self.settings, gm))
            except Exception as e:
                args_preview = f"<参数预览失败: {e}>"
            lines = [
                f"AirPlay Deck v{APP_VERSION}  诊断导出  {ts}",
                f"OS: {sys.platform}  游戏模式: {gm}  桌面模式: {not gm}",
                f"DISPLAY={os.environ.get('DISPLAY','')}  "
                f"XAUTHORITY={os.environ.get('XAUTHORITY') or '<unset>'}",
                f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY') or '<unset>'}  "
                f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE') or '<unset>'}  "
                f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR') or '<unset>'}",
                f"LD_PRELOAD={os.environ.get('LD_PRELOAD') or '<unset>'}",
                "",
                "=== Settings ===",
                _json.dumps(self.settings, ensure_ascii=False, indent=2),
                "",
                "=== uxplay 实际参数预览 ===",
                args_preview,
                "",
            ]
            # 容器内 mDNS / D-Bus 实况：这是「搜不到设备」类问题的关键证据，
            # 导出日志时自动带上，省得再让用户单独敲命令。
            try:
                _c = (self.settings.get("distrobox_container") or "uxplay-env").strip()
                lines.append("=== 容器内 mDNS / D-Bus 诊断 ===")
                lines.append(avahi.container_mdns_report(_c))
                lines.append("")
            except Exception as e:
                lines.append(f"=== 容器内 mDNS / D-Bus 诊断 ===（失败：{e}）")
                lines.append("")
            lines.append("=== Recent log (最多 300 条，最新在末尾) ===")
            lines.extend(self._log_buffer[-300:])
            try:
                if cfg.LOG_FILE.exists():
                    with open(cfg.LOG_FILE, "r", encoding="utf-8", errors="replace") as lf:
                        tail = lf.readlines()[-200:]
                    if tail:
                        lines.append("")
                        lines.append(f"=== 持久日志文件尾部 ({cfg.LOG_FILE}) ===")
                        lines.extend(t.rstrip("\n") for t in tail)
            except Exception:
                pass
            lines.append("")
            lines.append(f"=== Saved to: {path} ===")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self._log("info", f"诊断日志已导出：{path}")
            QMessageBox.information(
                self, self.T("export_title"),
                self.T("export_body") + f"\n{path}",
            )
        except Exception as e:
            self._log("error", f"导出日志失败: {e}")
            QMessageBox.warning(self, self.T("export_fail"), str(e))

    def _reset_settings(self):
        ret = QMessageBox.question(
            self, self.T("reset_title"), self.T("reset_body"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        lang = self.lang
        self.settings = dict(cfg.DEFAULT_SETTINGS)
        self.settings["language"] = lang
        try:
            cfg.save_settings(self.settings)
        except Exception as e:
            self._log("error", f"保存默认设置失败: {e}")
            return
        self._load_ui_from_settings()
        # 重置可能把主题从「深色」改回「自动」——下拉回填时值没变不会触发重刷，
        # 这里显式刷一次，保证界面外观和设置一致。
        self._apply_qss()
        self._log("info", self.T("log_reset_done"))
        self._flash_saved(self.T("log_reset_done"))

    # ---- 运行环境：首启自动安装 / 一键卸载 --------------------------------- #
    def _env_status_text(self) -> str:
        c = (self.settings.get("distrobox_container") or "uxplay-env").strip()
        if self.settings.get("use_distrobox") and cman.is_ready(c):
            return self.T("env_ready").format(c=c)
        return self.T("env_missing")

    def _maybe_first_bootstrap(self):
        """首次启动且运行环境未就绪时，自动弹一次安装引导（env_bootstrap_seen 去重）。"""
        if not self.settings.get("use_distrobox"):
            return
        c = (self.settings.get("distrobox_container") or "uxplay-env").strip()
        if cman.is_ready(c):
            return
        self.settings["env_bootstrap_seen"] = True
        try:
            cfg.save_settings(self.settings)
        except Exception:
            pass
        self._show_bootstrap_dialog(first_launch=True)

    def _show_bootstrap_dialog(self, first_launch: bool = False):
        c = (self.settings.get("distrobox_container") or "uxplay-env").strip()
        dlg = QDialog(self)
        dlg.setWindowTitle(self.T("bootstrap_title"))
        dlg.setMinimumSize(440, 220)
        dlg.setStyleSheet(self.styleSheet())
        v = QVBoxLayout(dlg)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(12)
        intro = QLabel(self.T("bootstrap_body").format(c=c))
        intro.setWordWrap(True)
        intro.setObjectName("hint")
        v.addWidget(intro)
        v.addStretch(1)
        bh = QHBoxLayout()
        bh.setSpacing(10)
        later = QPushButton(self.T("bootstrap_later"))
        later.setMinimumHeight(44)
        later.setCursor(Qt.CursorShape.PointingHandCursor)
        later.clicked.connect(dlg.reject)
        install = QPushButton(self.T("bootstrap_install"))
        install.setObjectName("primary")
        install.setMinimumHeight(44)
        install.setCursor(Qt.CursorShape.PointingHandCursor)
        install.clicked.connect(lambda: self._begin_bootstrap_install(dlg))
        bh.addWidget(later, 1)
        bh.addWidget(install, 1)
        v.addLayout(bh)
        dlg.exec()

    def _begin_bootstrap_install(self, dlg):
        dlg.accept()
        self._install_env()

    def _ensure_env_ready(self) -> bool:
        """开始接收前确认运行环境就绪；未就绪则弹引导，避免点开始却黑屏报错。"""
        if not self.settings.get("use_distrobox"):
            return True
        c = (self.settings.get("distrobox_container") or "uxplay-env").strip()
        if cman.is_ready(c):
            return True
        self._log("warn", self.T("env_missing") + "，请先安装运行环境")
        QTimer.singleShot(0, lambda: self._show_bootstrap_dialog(first_launch=False))
        return False

    def _install_env(self):
        c = (self.settings.get("distrobox_container") or "uxplay-env").strip()
        dlg = ProgressDialog(
            self, self.T("btn_install_env"),
            self.T("bootstrap_body").format(c=c),
        )
        worker = ContainerWorker("install", c, self)

        def _on_progress(level, msg):
            dlg.append(level, msg)
            self._on_log(level, msg)

        def _on_done(ok, msg):
            self._on_install_done(ok, msg, dlg, worker)

        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_done)
        dlg.show()
        worker.start()

    def _on_install_done(self, ok, msg, dlg, worker):
        if ok:
            self.settings["use_distrobox"] = True
            self.settings["env_bootstrap_seen"] = True
            try:
                cfg.save_settings(self.settings)
            except Exception:
                pass
            dlg.finish(True, self.T("bootstrap_done"))
            self._log("info", self.T("bootstrap_done"))
        else:
            dlg.finish(False, self.T("bootstrap_failed").format(e=msg))
            self._log("error", self.T("bootstrap_failed").format(e=msg))
        if hasattr(self, "env_status"):
            self.env_status.setText(self._env_status_text())
        try:
            worker.deleteLater()
        except Exception:
            pass

    def _uninstall_env(self):
        c = (self.settings.get("distrobox_container") or "uxplay-env").strip()
        ret = QMessageBox.question(
            self, self.T("uninstall_title"),
            self.T("uninstall_body").format(c=c),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        # 先停掉正在跑的接收，避免容器被占用删不掉
        self._stop()
        dlg = ProgressDialog(
            self, self.T("uninstall_title"),
            self.T("uninstall_body").format(c=c),
        )
        worker = ContainerWorker("uninstall", c, self)

        def _on_progress(level, msg):
            dlg.append(level, msg)
            self._on_log(level, msg)

        def _on_done(ok, msg):
            self._on_uninstall_done(ok, msg, dlg, worker)

        worker.progress.connect(_on_progress)
        worker.finished.connect(_on_done)
        dlg.show()
        worker.start()

    def _on_uninstall_done(self, ok, msg, dlg, worker):
        if ok:
            dlg.finish(True, self.T("uninstall_done").format(detail=msg))
            self._log("info", self.T("uninstall_done").format(detail=msg))
        else:
            dlg.finish(False, self.T("uninstall_failed").format(e=msg))
            self._log("error", self.T("uninstall_failed").format(e=msg))
        if hasattr(self, "env_status"):
            self.env_status.setText(self._env_status_text())
        try:
            worker.deleteLater()
        except Exception:
            pass

    # ---- 退出 --------------------------------------------------------------- #
    def _quit(self):
        self._stop()
        try:
            from core import keepalive as _ka
            _ka.restore_display_defaults(log=lambda lvl, msg: self._log(lvl, msg))
        except Exception:
            pass
        QApplication.quit()

    def closeEvent(self, event):
        if getattr(self, "tray", None) and self.tray.isVisible():
            self.hide()
            event.ignore()
        else:
            self._quit()


# --------------------------------------------------------------------------- #
def main():
    cfg.ensure_dirs()
    app = QApplication(sys.argv)
    # 单实例：避免双开导致两个 uxplay 抢 PIN 配对（日志里成对出现）
    _lock_dir = os.path.join(os.path.expanduser("~"), ".local", "state", "airplay-deck")
    os.makedirs(_lock_dir, exist_ok=True)
    _lock = QLockFile(os.path.join(_lock_dir, "airplay-deck.lock"))
    _lock.setStaleLockTime(30_000)
    if not _lock.tryLock(100):
        print("AirPlay Deck 已在运行（单实例）", flush=True)
        return 1

    app.setApplicationName(APP_TITLE)
    # 强制 Fusion 风格：避免 macOS native style 把 QCheckBox 渲染成开关
    # 破坏表单对齐（Linux KDE Breeze 下也建议走 Fusion 以保证跨设备一致）。
    try:
        from PySide6.QtWidgets import QStyleFactory
        if "Fusion" in QStyleFactory.keys():
            app.setStyle("Fusion")
    except Exception:
        pass
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
