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
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import (
    QObject, Qt, pyqtSignal, QStandardPaths,
    QPropertyAnimation, QParallelAnimationGroup, QAbstractAnimation,
    QEasingCurve, QPoint, QTimer, QThread,
)
from PyQt6.QtGui import QIcon, QPainter, QColor, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QTextEdit, QCheckBox,
    QSystemTrayIcon, QMenu, QMessageBox, QFrame, QScrollArea, QDialog,
)

from core import settings as cfg, launcher, session, uxplay_args
from core import i18n
from core import container as cman
from core import avahi

APP_VERSION = "0.6.6"
APP_TITLE = "AirPlay Deck"

# 改动这些设置后，若正在接收则自动重启 uxplay 让新参数生效
RUNTIME_KEYS = {
    "device_name", "append_hostname", "fps", "video_sink", "resolution",
    "display_mode", "decoder", "keep_window", "legacy_ports",
    "audio_sync",
    "extra", "uxplay_path", "use_distrobox", "distrobox_container",
    "distrobox_method",
}


# --------------------------------------------------------------------------- #
# 自绘开关（iOS 风格 pill toggle），用于主页一键启停
# --------------------------------------------------------------------------- #
class ToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

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

    progress = pyqtSignal(str, str)   # (level, message)
    finished = pyqtSignal(bool, str)  # (ok, summary)

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
    log_signal = pyqtSignal(str, str)
    status_signal = pyqtSignal(str)


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
        self._rebuilding = False
        self._was_connected = False  # 是否刚经历过一次「已连接」，用于断开后拉回主窗口

        if self._icon_path:
            self.setWindowIcon(QIcon(self._icon_path))

        self._build_pages()
        self.bridge.log_signal.connect(self._on_log)
        self.bridge.status_signal.connect(self._on_status)
        self._setup_tray()


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
            self.T("device_prefix") + self.settings.get("device_name", "SteamDeck")
        )
        self.device_hint.setObjectName("hint")
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
        lbl.setStyleSheet("font-size: 15px; color: #e6e7ea;")
        h.addWidget(lbl)
        h.addStretch(1)
        chev = QLabel("›")
        chev.setStyleSheet("color: #6c6e74; font-size: 28px;")
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
        form.addRow(self._form_label(""), self.f_append)

        self.f_autostart = QCheckBox(self.T("autostart"))
        self.f_autostart.setChecked(bool(self.settings.get("autostart")))
        form.addRow(self._form_label(""), self.f_autostart)

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

        card.layout().addLayout(form)
        body.addWidget(card)
        body.addWidget(self._make_save_button())
        body.addStretch(1)
        return page

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
        self.f_res.addItems(cfg.RESOLUTION_CHOICES)
        self.f_res.setCurrentText(self.settings.get("resolution", "auto"))
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
        self.f_res.setCurrentText("auto")
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

        # 容器运行方式：仅保留进入方式选择（容器模式固定开启）
        card = self._make_card(self.T("sec_uxplay"))
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

        card.layout().addLayout(form)
        body.addWidget(card)

        # 运行环境状态：与「一键检查」反馈合并展示
        env_card = self._make_card(self.T("env_card"))
        self.env_status = QLabel(self._env_status_text())
        self.env_status.setObjectName("hint")
        self.env_status.setWordWrap(True)
        env_card.layout().addWidget(self.env_status)
        body.addWidget(env_card)

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
        text.setObjectName("hint")
        text.setStyleSheet("color: #cfd1d5; font-size: 12px; line-height: 1.7;")
        cv.addWidget(text)
        body.addWidget(card)
        body.addStretch(1)
        return page

    # ---- QSS ---------------------------------------------------------------- #
    def _apply_qss(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #1a1b1e; color: #e6e7ea;
                font-family: 'Noto Sans CJK SC', 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
                font-size: 13px; }
            QLabel { background: transparent; color: #e6e7ea; }
            QLabel#appTitle { font-size: 19px; font-weight: 600;
                color: #e6e7ea; padding: 18px 0 4px 0; letter-spacing: 0.5px; }
            QLabel#sectionTitle { color: #b8bcc4; font-size: 12px; font-weight: 600;
                padding-bottom: 2px; letter-spacing: 0.3px; }
            QLabel#hint { color: #8b8d93; font-size: 11px; }
            QLabel#bigStatus { font-size: 17px; font-weight: 600; color: #e6e7ea; }
            QFrame { border: none; }
            QFrame#card { background: #25272b; border: 1px solid #2f3136; border-radius: 12px; }
            QFrame#sep { background: #2a2c30; max-height: 1px; min-height: 1px; border: none; }
            QWidget#page { background: #1a1b1e; }
            QWidget#subHeader { background: #1f2024; border-bottom: 1px solid #2a2c30; }
            QWidget#menuRow { background: transparent; }
            QWidget#menuRow:hover { background: #2a2c30; }
            QLabel#subTitle { font-size: 16px; font-weight: 600; color: #e6e7ea; }
            /* 表单左侧标签：与右侧输入框等高（40px）+ 垂直居中，保证文字齐平 */
            QLabel#formLabel { font-size: 13px; color: #c3c6cc;
                padding-right: 4px; background: transparent; }
            /* 返回键只有箭头：字号放大到与「返回」文字等高，padding 归零避免被裁切 */
            QPushButton#backBtn { background: transparent; border: none;
                color: #e6e7ea; font-size: 34px; font-weight: 400;
                padding: 0 0 4px 0; }
            QPushButton#backBtn:hover { color: #4f8cff; }

            QLineEdit, QComboBox, QTextEdit {
                background: #1a1b1e; color: #e6e7ea;
                border: 1px solid #3a3c41; border-radius: 8px;
                padding: 8px 10px; min-height: 22px;
                selection-background-color: #4f8cff; }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus { border-color: #4f8cff; }
            QComboBox::drop-down { border: none; width: 24px; }
            QComboBox QAbstractItemView { background: #25272b; color: #e6e7ea;
                selection-background-color: #4f8cff; border: 1px solid #3a3c41; border-radius: 6px;
                padding: 6px; }
            QTextEdit#log { background: #111214; color: #c9d1d9;
                font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 11px; }

            QCheckBox { color: #e6e7ea; spacing: 10px; min-height: 26px; }
            QCheckBox::indicator { width: 20px; height: 20px; border-radius: 5px;
                border: 1px solid #3a3c41; background: #1a1b1e; }
            QCheckBox::indicator:hover { border-color: #4f8cff; }
            QCheckBox::indicator:checked { background: #4f8cff; border-color: #4f8cff; }

            QPushButton { background: #2f3136; color: #e6e7ea;
                border: 1px solid #3a3c41; border-radius: 8px;
                padding: 9px 16px; font-size: 13px; min-height: 26px; }
            QPushButton:hover { background: #3a3c41; border-color: #4a4c51; }
            QPushButton:pressed { background: #25272b; }
            QPushButton:disabled { background: #232427; color: #5a5c61; border-color: #2a2c2f; }
            QPushButton#primary { background: #4f8cff; color: white; border: none;
                padding: 10px 18px; font-size: 14px; font-weight: 600; border-radius: 10px; }
            QPushButton#primary:hover { background: #6aa0ff; }
            QPushButton#primary[ok="1"] { background: #2ea043; }
            QPushButton#danger { color: #ef6464; border-color: #5a2c2c; }
            QPushButton#danger:hover { background: #3a2222; }

            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background: transparent; width: 8px; }
            QScrollBar::handle:vertical { background: #3a3c41; border-radius: 4px; min-height: 34px; }
            QScrollBar::handle:vertical:hover { background: #4a4c51; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

    def _load_ui_from_settings(self) -> None:
        s = self.settings
        self.f_name.setText(s.get("device_name", "SteamDeck"))
        self.f_append.setChecked(bool(s.get("append_hostname")))
        self.f_autostart.setChecked(bool(s.get("autostart")))
        i = self.f_fps.findData(int(s.get("fps", 30) or 30))
        self.f_fps.setCurrentIndex(max(0, i))
        self.f_sink.setCurrentText(s.get("video_sink", "ximagesink"))
        self.f_res.setCurrentText(s.get("resolution", "auto"))
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
        try:
            self.device_hint.setText(
                self.T("device_prefix") + str(s.get("device_name", "SteamDeck"))
            )
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
        self.launcher = launcher.Launcher(
            self.settings,
            log_cb=lambda lvl, msg: self.bridge.log_signal.emit(lvl, msg),
            status_cb=lambda st: self.bridge.status_signal.emit(st),
        )
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

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---- 回调槽 ------------------------------------------------------------- #
    def _on_log(self, level: str, msg: str):
        color = {
            "info": "#8b949e", "warn": "#d29922", "error": "#f85149",
            "cmd": "#58a6ff", "exit": "#8b949e", "uxplay": "#c9d1d9",
        }.get(level, "#c9d1d9")
        try:
            self.log.append(f'<span style="color:{color}">[{level}] {msg}</span>')
        except Exception:
            return
        self._log_buffer.append(f"[{level}] {msg}")
        if len(self._log_buffer) > 500:
            self._log_buffer = self._log_buffer[-500:]
        # 落盘持久日志：游戏模式窗口会被隐藏，回桌面后仍能从文件读到当时输出
        try:
            cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(cfg.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{level}] {msg}\n")
        except Exception:
            pass
        if self.log.document().blockCount() > 500:
            self.log.clear()

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
        s["fps"] = int(self.f_fps.currentData() or 30)
        s["video_sink"] = self.f_sink.currentText()
        s["resolution"] = self.f_res.currentText()
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
        return s

    def _save(self, silent: bool = False):
        old = dict(self.settings)
        self.settings = self._collect()
        try:
            cfg.save_settings(self.settings)
        except Exception as e:
            if not silent:
                self._flash_saved(str(e))
            self._log("error", str(e))
            return
        try:
            self.device_hint.setText(
                self.T("device_prefix") + str(self.settings.get("device_name", "SteamDeck"))
            )
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
    app.setApplicationName(APP_TITLE)
    # 强制 Fusion 风格：避免 macOS native style 把 QCheckBox 渲染成开关
    # 破坏表单对齐（Linux KDE Breeze 下也建议走 Fusion 以保证跨设备一致）。
    try:
        from PyQt6.QtWidgets import QStyleFactory
        if "Fusion" in QStyleFactory.keys():
            app.setStyle("Fusion")
    except Exception:
        pass
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
