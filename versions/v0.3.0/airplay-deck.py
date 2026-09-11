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
    QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import QIcon, QPainter, QColor, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QPushButton, QLineEdit, QComboBox, QSpinBox, QCheckBox, QTextEdit,
    QSystemTrayIcon, QMenu, QMessageBox, QFrame, QScrollArea, QStackedWidget,
    QGraphicsOpacityEffect,
)

from core import settings as cfg, launcher, session, uxplay_args

APP_VERSION = "0.3.0"
APP_TITLE = "AirPlay Deck"


# --------------------------------------------------------------------------- #
# 自绘开关（iOS 风格 pill toggle），用于主页一键启停
# --------------------------------------------------------------------------- #
class ToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False
        self.setFixedSize(46, 26)
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
        # 轨道
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#4f8cff") if self._on else QColor("#3a3c41"))
        p.drawRoundedRect(rect, 13, 13)
        # 滑块
        d = 20
        x = 3 if not self._on else rect.width() - d - 3
        y = (rect.height() - d) // 2
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(x, y, d, d)


# --------------------------------------------------------------------------- #
# 桥接：把后台线程的日志/状态信号转发到 UI 线程
# --------------------------------------------------------------------------- #
class Bridge(QObject):
    log_signal = pyqtSignal(str, str)
    status_signal = pyqtSignal(str)


# --------------------------------------------------------------------------- #
# 主窗口（clashmi 风格：主页 + 多子页，点击菜单切换）
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE}  v{APP_VERSION}")
        self.resize(460, 720)
        self.setMinimumSize(420, 600)

        self.settings = cfg.load_settings()
        self.launcher: launcher.Launcher | None = None
        self.bridge = Bridge()
        self._log_buffer: list[str] = []
        self._icon_path = self._pick_icon()

        if self._icon_path:
            self.setWindowIcon(QIcon(self._icon_path))

        self._build_pages()
        self.bridge.log_signal.connect(self._on_log)
        self.bridge.status_signal.connect(self._on_status)
        self._setup_tray()

        self._log("info", f"AirPlay Deck v{APP_VERSION} 已启动")
        self._refresh_status()

        if self.settings.get("autostart"):
            self._log("info", "已开启开机自启，正在自动启动…")
            self._start()

    def _pick_icon(self) -> str:
        """优先用 iOS squircle 矢量图标 icon.svg，回退位图 icon.png。"""
        res = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")
        for name in ("icon.svg", "icon.png"):
            p = os.path.join(res, name)
            if os.path.exists(p):
                return p
        return ""

    # ---- 页面构建 ---------------------------------------------------------- #
    def _build_pages(self):
        self._apply_qss()
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        # 注意：顺序即 _goto() 的索引。uxplay/容器设置已并入「检查与日志」页。
        self._page_main = self._build_main_page()    # 0
        self._page_device = self._build_device_page()  # 1
        self._page_video = self._build_video_page()    # 2
        self._page_check = self._build_check_page()    # 3
        self._page_about = self._build_about_page()    # 4
        for p in (self._page_main, self._page_device, self._page_video,
                  self._page_check, self._page_about):
            self.stack.addWidget(p)

    def _goto(self, idx: int):
        """切换页面并播放淡入动画（进出/返回都有）。"""
        if idx == self.stack.currentIndex():
            return
        self.stack.setCurrentIndex(idx)
        self._fade_in_current()

    def _fade_in_current(self):
        w = self.stack.currentWidget()
        if w is None:
            return
        # 清掉上一次可能残留的 effect，避免叠加
        try:
            w.setGraphicsEffect(None)
        except Exception:
            pass
        eff = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity")
        anim.setDuration(190)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # 结束后移除 effect，恢复原生渲染（省资源）
        anim.finished.connect(lambda: w.setGraphicsEffect(None))
        self._anim = anim  # 保持引用，防止被 GC 导致动画中断
        anim.start()

    def _icons_dir(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "icons")

    def _menu_icon(self, name: str, size: int = 22) -> QPixmap:  # noqa: F821
        p = os.path.join(self._icons_dir(), f"{name}.svg")
        if os.path.exists(p):
            return QIcon(p).pixmap(size, size)
        return QIcon().pixmap(size, size)

    def _build_main_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # 标题
        title = QLabel(APP_TITLE)
        title.setObjectName("appTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)
        v.addSpacing(10)

        # 状态卡（点 + 文字 + 开关）
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(18, 14, 18, 14)
        cv.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color: #888; font-size: 18px;")
        self.status_label = QLabel("未连接")
        self.status_label.setObjectName("bigStatus")
        row.addWidget(self.dot)
        row.addWidget(self.status_label)
        row.addStretch(1)
        self.toggle = ToggleSwitch()
        self.toggle.toggled.connect(self._on_toggle_switch)
        row.addWidget(self.toggle)
        cv.addLayout(row)
        sub = QLabel(f"设备名：{self.settings.get('device_name', 'SteamDeck')}")
        sub.setObjectName("hint")
        cv.addWidget(sub)
        v.addWidget(card)
        v.addSpacing(10)

        # 菜单列表（5 个子页入口）
        menu = QFrame()
        menu.setObjectName("card")
        mv = QVBoxLayout(menu)
        mv.setContentsMargins(0, 4, 0, 4)
        mv.setSpacing(0)
        items = [
            ("device", "设备设置", 1),
            ("video", "视频与渲染", 2),
            ("check", "检查与日志", 3),
            ("about", "关于", 4),
        ]
        for i, (icon_name, text, idx) in enumerate(items):
            row_w = self._make_menu_row(icon_name, text)
            # 覆盖 mousePressEvent：点击跳到对应子页
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
        """clashmi 风格菜单行：左侧线框图标 + 标题 + 右侧 › 箭头。"""
        w = QWidget()
        w.setObjectName("menuRow")
        w.setMinimumHeight(54)
        w.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(w)
        h.setContentsMargins(18, 8, 16, 8)
        h.setSpacing(14)
        icon_lbl = QLabel()
        icon_lbl.setFixedSize(24, 24)
        icon_lbl.setPixmap(self._menu_icon(icon_name, 24))
        h.addWidget(icon_lbl)
        lbl = QLabel(text)
        lbl.setStyleSheet("font-size: 14px; color: #e6e7ea;")
        h.addWidget(lbl)
        h.addStretch(1)
        chev = QLabel("›")
        chev.setStyleSheet("color: #6c6e74; font-size: 26px;")
        h.addWidget(chev)
        return w

    def _make_subpage(self, title_text: str):
        """返回一个 (page, body_layout) 元组，子页构造器往 body_layout 加内容。"""
        page = QWidget()
        page.setObjectName("page")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        # 顶部条：返回 + 标题
        header = QWidget()
        header.setObjectName("subHeader")
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 8, 8, 8)
        h.setSpacing(8)
        back = QPushButton("←  返回")
        back.setObjectName("backBtn")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(lambda: self._goto(0))
        h.addWidget(back)
        t = QLabel(title_text)
        t.setObjectName("subTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(t, 1)
        h.addSpacing(60)  # 视觉上让标题居中（抵消左侧返回按钮宽度）
        v.addWidget(header)
        # 滚动主体
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        v.addWidget(scroll, 1)
        body = QWidget()
        scroll.setWidget(body)
        body_v = QVBoxLayout(body)
        body_v.setContentsMargins(16, 12, 16, 16)
        body_v.setSpacing(12)
        return page, body_v

    def _build_device_page(self):
        page, body = self._make_subpage("设备设置")
        card = self._make_card("设备")
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.f_name = QLineEdit(self.settings.get("device_name", "SteamDeck"))
        form.addRow("设备名称（iPhone 上显示）", self.f_name)

        self.f_append = QCheckBox("在名称后追加主机名")
        self.f_append.setChecked(bool(self.settings.get("append_hostname")))
        form.addRow("", self.f_append)

        self.f_autostart = QCheckBox("启动 App 时自动开始接收")
        self.f_autostart.setChecked(bool(self.settings.get("autostart")))
        form.addRow("", self.f_autostart)

        card.layout().addLayout(form)
        body.addWidget(card)
        body.addWidget(self._make_save_button())
        body.addStretch(1)
        return page

    def _build_video_page(self):
        page, body = self._make_subpage("视频与渲染")
        card = self._make_card("视频与渲染")
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.f_fps = QSpinBox(); self.f_fps.setRange(1, 120)
        self.f_fps.setValue(int(self.settings.get("fps", 30)))
        form.addRow("帧率上限 (fps)", self.f_fps)

        self.f_sink = QComboBox(); self.f_sink.addItems(cfg.VIDEO_SINKS)
        self.f_sink.setCurrentText(self.settings.get("video_sink", "ximagesink"))
        form.addRow("视频后端", self.f_sink)

        self.f_res = QComboBox(); self.f_res.addItems(cfg.RESOLUTION_CHOICES)
        self.f_res.setCurrentText(self.settings.get("resolution", "auto"))
        form.addRow("渲染分辨率", self.f_res)

        self.f_mode = QComboBox(); self.f_mode.addItems(cfg.DISPLAY_MODE_CHOICES)
        self.f_mode.setCurrentText(self.settings.get("display_mode", "auto"))
        form.addRow("显示模式", self.f_mode)

        self.f_decoder = QComboBox(); self.f_decoder.addItems(cfg.DECODER_CHOICES)
        self.f_decoder.setCurrentText(self.settings.get("decoder", "auto"))
        form.addRow("解码器", self.f_decoder)

        self.f_reset = QSpinBox(); self.f_reset.setRange(0, 600)
        self.f_reset.setValue(int(self.settings.get("reset", 15)))
        form.addRow("无响应重置（秒×3）", self.f_reset)

        self.f_keep = QCheckBox("保持窗口（断开后不关闭）")
        self.f_keep.setChecked(bool(self.settings.get("keep_window")))
        form.addRow("", self.f_keep)

        self.f_legacy = QCheckBox("使用旧端口")
        self.f_legacy.setChecked(bool(self.settings.get("legacy_ports")))
        form.addRow("", self.f_legacy)

        self.f_extra = QLineEdit(self.settings.get("extra", ""))
        self.f_extra.setPlaceholderText("追加到命令行的原始参数")
        form.addRow("额外参数", self.f_extra)

        tip = QLabel("帧率与渲染分辨率相互独立。30 fps + ximagesink + vsync off 是当前最低延迟组合。")
        tip.setObjectName("hint"); tip.setWordWrap(True)
        form.addRow("", tip)

        card.layout().addLayout(form)
        body.addWidget(card)
        body.addWidget(self._make_save_button())
        body.addStretch(1)
        return page

    def _build_check_page(self):
        """「检查与日志」页：uxplay/容器设置 + 检查/测试 + 运行日志 + 导出/重置。"""
        page, body = self._make_subpage("检查与日志")

        # ---- uxplay 与容器设置 ----
        card = self._make_card("uxplay 与容器")
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.f_uxpath = QLineEdit(self.settings.get("uxplay_path", ""))
        self.f_uxpath.setPlaceholderText("留空 = 用容器/PATH 里的 uxplay")
        form.addRow("uxplay 路径", self.f_uxpath)

        self.f_distrobox = QCheckBox("通过容器运行 uxplay（复用已部署的 uxplay-env）")
        self.f_distrobox.setChecked(bool(self.settings.get("use_distrobox")))
        form.addRow("", self.f_distrobox)

        self.f_dbox_name = QLineEdit(self.settings.get("distrobox_container", "uxplay-env"))
        form.addRow("容器名", self.f_dbox_name)

        self.f_dbox_method = QComboBox()
        self.f_dbox_method.addItems([
            "distrobox enter（默认）",
            "podman 直接执行（免授权）",
        ])
        cur = (self.settings.get("distrobox_method") or "enter").strip()
        self.f_dbox_method.setCurrentIndex(1 if cur == "podman" else 0)
        form.addRow("进入容器方式", self.f_dbox_method)

        warn = QLabel(
            "⚠ 游戏模式下无法弹出授权对话框。若游戏模式投屏没画面，"
            "请把「进入容器方式」改成「podman 直接执行（免授权）」、保存后重试。"
        )
        warn.setObjectName("warn"); warn.setWordWrap(True)
        form.addRow("", warn)

        card.layout().addLayout(form)
        body.addWidget(card)

        # 检查 / 测试
        actions = QWidget()
        ah = QHBoxLayout(actions)
        ah.setContentsMargins(0, 0, 0, 0)
        ah.setSpacing(8)
        self.btn_check = QPushButton("检查 uxplay")
        self.btn_check.clicked.connect(self._check_uxplay)
        self.btn_test = QPushButton("测试容器")
        self.btn_test.clicked.connect(self._test_container)
        ah.addWidget(self.btn_check)
        ah.addWidget(self.btn_test)
        ah.addStretch(1)
        body.addWidget(actions)

        body.addWidget(self._make_save_button())

        # ---- 运行日志 ----
        log_card = self._make_card("运行日志")
        self.log = QTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(200)
        log_card.layout().addWidget(self.log)
        body.addWidget(log_card, 1)

        # 导出 / 重置
        actions2 = QWidget()
        ah2 = QHBoxLayout(actions2)
        ah2.setContentsMargins(0, 0, 0, 0)
        ah2.setSpacing(8)
        self.btn_export = QPushButton("导出日志")
        self.btn_export.clicked.connect(self._export_log)
        self.btn_reset = QPushButton("重置选项")
        self.btn_reset.setObjectName("danger")
        self.btn_reset.clicked.connect(self._reset_settings)
        ah2.addWidget(self.btn_export)
        ah2.addWidget(self.btn_reset)
        ah2.addStretch(1)
        body.addWidget(actions2)
        return page

    def _build_about_page(self):
        page, body = self._make_subpage("关于")
        card = self._make_card("AirPlay Deck")
        cv = card.layout()
        v_label = QLabel(f"版本  v{APP_VERSION}")
        v_label.setObjectName("bigStatus")
        cv.addWidget(v_label)
        text = QLabel(
            "把 Steam Deck / 任意 Linux 桌面变成 AirPlay 接收端，包装 UxPlay 引擎。\n\n"
            "使用步骤：\n"
            "  1. 桌面模式打开本 App，进「检查与日志」确认勾选「通过容器运行 uxplay」。\n"
            "  2. 点「测试容器」完成容器导出（首次需图形授权）。\n"
            "  3. 回到主页，点右上角开关开始接收。\n"
            "  4. iPhone / iPad / Mac 控制中心 → 屏幕镜像 → 选本机名称。\n"
            "  5. 游戏模式全屏：Steam → 添加非 Steam 游戏 → 选本 AppImage → 启动。\n"
            "     若游戏模式没画面，把「进入容器方式」改为「podman 直接执行（免授权）」并保存重试。\n\n"
            "AirPlay 投屏本身固有 ~100–300 ms 延迟（网络 + 编码缓冲），"
            "已默认 30 fps + ximagesink + vsync off，是当前最稳最低延迟组合。"
        )
        text.setWordWrap(True)
        text.setObjectName("hint")
        text.setStyleSheet("color: #cfd1d5; font-size: 12px; line-height: 1.6;")
        cv.addWidget(text)
        body.addWidget(card)
        body.addStretch(1)
        return page

    def _make_card(self, title: str) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("sectionTitle")
        v.addWidget(t)
        return card

    def _make_save_button(self) -> QPushButton:
        btn = QPushButton("保存设置")
        btn.setObjectName("primary")
        btn.setDefault(True)
        btn.clicked.connect(self._save)
        return btn

    # ---- QSS ---------------------------------------------------------------- #
    def _apply_qss(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #1a1b1e; color: #e6e7ea;
                font-family: 'Noto Sans CJK SC', 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
                font-size: 13px; }
            QLabel { background: transparent; color: #e6e7ea; }
            QLabel#appTitle { font-size: 18px; font-weight: 600;
                color: #e6e7ea; padding: 16px 0 4px 0; letter-spacing: 0.5px; }
            QLabel#sectionTitle { color: #b8bcc4; font-size: 12px; font-weight: 600;
                padding-bottom: 2px; letter-spacing: 0.3px; }
            QLabel#hint { color: #8b8d93; font-size: 11px; }
            QLabel#warn { color: #e0a93b; font-size: 11px; }
            QLabel#bigStatus { font-size: 16px; font-weight: 600; color: #e6e7ea; }
            QFrame { border: none; }
            QFrame#card { background: #25272b; border: 1px solid #2f3136; border-radius: 10px; }
            QFrame#sep { background: #2a2c30; max-height: 1px; min-height: 1px; border: none; }
            QWidget#page { background: #1a1b1e; }
            QWidget#subHeader { background: #1f2024; border-bottom: 1px solid #2a2c30; }
            QWidget#menuRow { background: transparent; }
            QWidget#menuRow:hover { background: #2a2c30; }
            QLabel#subTitle { font-size: 15px; font-weight: 600; color: #e6e7ea; }

            QLineEdit, QComboBox, QSpinBox, QTextEdit {
                background: #1a1b1e; color: #e6e7ea;
                border: 1px solid #3a3c41; border-radius: 6px;
                padding: 6px 8px; selection-background-color: #4f8cff; }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus { border-color: #4f8cff; }
            QComboBox::drop-down { border: none; width: 20px; }
            QComboBox QAbstractItemView { background: #25272b; color: #e6e7ea;
                selection-background-color: #4f8cff; border: 1px solid #3a3c41; border-radius: 4px;
                padding: 4px; }
            QSpinBox::up-button, QSpinBox::down-button { width: 16px; border: none; background: transparent; }
            QTextEdit#log { background: #111214; color: #c9d1d9;
                font-family: 'JetBrains Mono', 'Consolas', monospace; font-size: 11px; }

            QCheckBox { color: #e6e7ea; spacing: 8px; }
            QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
                border: 1px solid #3a3c41; background: #1a1b1e; }
            QCheckBox::indicator:hover { border-color: #4f8cff; }
            QCheckBox::indicator:checked { background: #4f8cff; border-color: #4f8cff; }

            QPushButton { background: #2f3136; color: #e6e7ea;
                border: 1px solid #3a3c41; border-radius: 6px;
                padding: 7px 14px; font-size: 13px; }
            QPushButton:hover { background: #3a3c41; border-color: #4a4c51; }
            QPushButton:pressed { background: #25272b; }
            QPushButton:disabled { background: #232427; color: #5a5c61; border-color: #2a2c2f; }
            QPushButton#primary { background: #4f8cff; color: white; border: none;
                padding: 9px 18px; font-size: 13px; font-weight: 600; border-radius: 8px; }
            QPushButton#primary:hover { background: #6aa0ff; }
            QPushButton#danger { color: #ef6464; border-color: #5a2c2c; }
            QPushButton#danger:hover { background: #3a2222; }
            QPushButton#backBtn { background: transparent; border: none; color: #e6e7ea;
                font-size: 21px; padding: 6px 12px; font-weight: 600; }
            QPushButton#backBtn:hover { color: #4f8cff; }

            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background: transparent; width: 8px; }
            QScrollBar::handle:vertical { background: #3a3c41; border-radius: 4px; min-height: 30px; }
            QScrollBar::handle:vertical:hover { background: #4a4c51; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QStackedWidget { background: #1a1b1e; }
        """)

    def _load_ui_from_settings(self) -> None:
        s = self.settings
        self.f_name.setText(s.get("device_name", "SteamDeck"))
        self.f_append.setChecked(bool(s.get("append_hostname")))
        self.f_autostart.setChecked(bool(s.get("autostart")))
        self.f_fps.setValue(int(s.get("fps", 30)))
        self.f_sink.setCurrentText(s.get("video_sink", "ximagesink"))
        self.f_res.setCurrentText(s.get("resolution", "auto"))
        self.f_mode.setCurrentText(s.get("display_mode", "auto"))
        self.f_decoder.setCurrentText(s.get("decoder", "auto"))
        self.f_reset.setValue(int(s.get("reset", 15)))
        self.f_keep.setChecked(bool(s.get("keep_window")))
        self.f_legacy.setChecked(bool(s.get("legacy_ports")))
        self.f_extra.setText(s.get("extra", ""))
        self.f_uxpath.setText(s.get("uxplay_path", ""))
        self.f_distrobox.setChecked(bool(s.get("use_distrobox")))
        self.f_dbox_name.setText(s.get("distrobox_container", "uxplay-env"))
        self.f_dbox_method.setCurrentIndex(
            1 if (s.get("distrobox_method") or "enter").strip() == "podman" else 0
        )

    # ---- 托盘 --------------------------------------------------------------- #
    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self)
        if self._icon_path:
            self.tray.setIcon(QIcon(self._icon_path))
        menu = QMenu()
        act_show = menu.addAction("显示窗口")
        act_show.triggered.connect(self._show_window)
        act_stop = menu.addAction("停止接收")
        act_stop.triggered.connect(lambda: self._stop() if self.launcher else None)
        act_quit = menu.addAction("退出")
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

    def _start(self):
        self._save()  # 启动前先把当前表单落盘
        self.launcher = launcher.Launcher(
            self.settings,
            log_cb=lambda lvl, msg: self.bridge.log_signal.emit(lvl, msg),
            status_cb=lambda st: self.bridge.status_signal.emit(st),
        )
        self.launcher.start()
        self.toggle.setChecked(True)  # 程序设置，不发信号
        # 游戏模式下隐藏主窗口，让 uxplay 全屏接管；桌面模式保留窗口
        if session.is_gamemode():
            self.hide()

    def _stop(self):
        if self.launcher:
            self.launcher.stop()
            self.launcher = None
        self.toggle.setChecked(False)

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
        self.log.append(f'<span style="color:{color}">[{level}] {msg}</span>')
        # 同时写入纯文本缓冲，供「导出日志」按钮一键落盘
        self._log_buffer.append(f"[{level}] {msg}")
        if len(self._log_buffer) > 500:
            self._log_buffer = self._log_buffer[-500:]
        # 同时落盘到持久日志文件（关键：游戏模式里窗口会隐藏，
        # 崩溃/失败后回桌面仍能从文件读到当时的输出，便于排查）
        try:
            cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(cfg.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{level}] {msg}\n")
        except Exception:
            pass
        # 限制文本框行数
        if self.log.document().blockCount() > 500:
            self.log.clear()

    def _log(self, level: str, msg: str):
        self._on_log(level, msg)

    def _on_status(self, status: str):
        text, color = {
            "idle": ("未连接", "#888888"),
            "waiting": ("等待设备连接…", "#d29922"),
            "connected": ("已连接，正在接收投屏", "#3fb950"),
            "error": ("出错", "#f85149"),
        }.get(status, ("未连接", "#888888"))
        self.status_label.setText(text)
        self.dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        # 同步主页开关：只有真正在等/已连时为开
        self.toggle.setChecked(status in ("waiting", "connected"))

    def _refresh_status(self):
        self.status_label.setText("未连接")
        self.dot.setStyleSheet("color: #888; font-size: 18px;")
        self.toggle.setChecked(False)

    # ---- 设置 --------------------------------------------------------------- #
    def _collect(self) -> dict:
        s = dict(self.settings)
        s["device_name"] = self.f_name.text().strip() or "SteamDeck"
        s["append_hostname"] = self.f_append.isChecked()
        s["autostart"] = self.f_autostart.isChecked()
        s["fps"] = self.f_fps.value()
        s["video_sink"] = self.f_sink.currentText()
        s["resolution"] = self.f_res.currentText()
        s["display_mode"] = self.f_mode.currentText()
        s["decoder"] = self.f_decoder.currentText()
        s["reset"] = self.f_reset.value()
        s["keep_window"] = self.f_keep.isChecked()
        s["legacy_ports"] = self.f_legacy.isChecked()
        s["extra"] = self.f_extra.text().strip()
        s["uxplay_path"] = self.f_uxpath.text().strip()
        s["use_distrobox"] = self.f_distrobox.isChecked()
        s["distrobox_container"] = self.f_dbox_name.text().strip() or "uxplay-env"
        s["distrobox_method"] = "podman" if self.f_dbox_method.currentIndex() == 1 else "enter"
        return s

    def _save(self):
        self.settings = self._collect()
        try:
            cfg.save_settings(self.settings)
            self._log("info", "设置已保存")
        except Exception as e:
            self._log("error", str(e))

    def _check_uxplay(self):
        binpath = (self.settings.get("uxplay_path") or "").strip() or "uxplay"
        use_dbox = bool(self.settings.get("use_distrobox"))
        container = (self.settings.get("distrobox_container") or "uxplay-env").strip()
        if use_dbox:
            cmd = ["distrobox", "enter", container, "--", binpath, "-v"]
            where = f"distrobox 容器 {container} 内"
        else:
            cmd = [binpath, "-v"]
            where = "系统 PATH"
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            if use_dbox:
                self._log("error",
                          "找不到 distrobox 命令。请先安装 distrobox，或在设置里取消勾选"
                          "「通过 distrobox 容器运行 uxplay」改用本机原生 uxplay。")
            else:
                self._log("error",
                          f"找不到 uxplay（{binpath}）。请先安装："
                          f"SteamOS → `yay -S uxplay`；Debian/Ubuntu → `sudo apt install uxplay`；"
                          f"Fedora → `sudo dnf install uxplay`；Arch → `yay -S uxplay`")
            return
        except Exception as e:
            self._log("error", f"检查 uxplay 失败: {e}")
            return
        if r.returncode != 0:
            err = ""
            for s in (r.stderr, r.stdout):
                if s and s.strip():
                    err = s.strip().splitlines()[-1]
            tail = f"：{err}" if err else ""
            if use_dbox:
                self._log("error",
                          f"在容器 {container} 内检查 uxplay 失败（退出码 {r.returncode}）{tail}。"
                          f"请确认该容器已装 uxplay（`distrobox enter {container} -- which uxplay`）。")
            else:
                self._log("error", f"uxplay 检查失败（退出码 {r.returncode}）{tail}。请先安装 uxplay。")
            return
        ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else "未知版本"
        self._log("info", f"uxplay 可用（{where}）：{ver}")

    def _test_container(self):
        if not self.settings.get("use_distrobox"):
            self._log("warn", "未启用 distrobox，跳过容器测试")
            return
        container = (self.settings.get("distrobox_container") or "uxplay-env").strip()
        self._log("info", f"测试 distrobox 容器 {container}（桌面模式会弹图形授权）…")
        try:
            r = subprocess.run(
                ["distrobox", "enter", container, "--",
                 "bash", "-c", "command -v uxplay && uxplay -v 2>&1 | head -1"],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            self._log("error", "找不到 distrobox 命令")
            return
        except Exception as e:
            self._log("error", f"测试容器失败: {e}")
            return
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        first = out.splitlines()[0] if out else ""
        if r.returncode == 0 and "uxplay" in out:
            self._log("info", f"容器 {container} 可用：{first}")
        else:
            self._log("error", f"容器 {container} 测试失败（rc={r.returncode}）：{first or '<无输出>'}")

    def _export_log(self):
        try:
            import json as _json
            from datetime import datetime as _dt
            downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
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
                f"XAUTHORITY={'<set>' if os.environ.get('XAUTHORITY') else '<unset>'}",
                f"XDG_CONFIG_HOME={os.environ.get('XDG_CONFIG_HOME','')}  "
                f"XDG_STATE_HOME={os.environ.get('XDG_STATE_HOME','')}",
                "",
                "=== Settings ===",
                _json.dumps(self.settings, ensure_ascii=False, indent=2),
                "",
                "=== uxplay 实际参数预览 ===",
                args_preview,
                "",
                "=== Recent log (最多 300 条，最新在末尾) ===",
            ]
            lines.extend(self._log_buffer[-300:])
            # 附带持久日志文件的尾部：游戏模式里窗口隐藏、进程随「退出游戏」被杀，
            # 回桌面再导出时内存缓冲是空的，只有文件里还留着当时的输出。
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
            QMessageBox.information(self, "导出完成", f"诊断日志已保存到：\n{path}")
        except Exception as e:
            self._log("error", f"导出日志失败: {e}")
            QMessageBox.warning(self, "导出失败", str(e))

    def _reset_settings(self):
        ret = QMessageBox.question(
            self, "重置选项",
            "确定将所有设置恢复为默认值？\n（设备名称、帧率、容器、显示模式等都会重置）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.settings = dict(cfg.DEFAULT_SETTINGS)
        try:
            cfg.save_settings(self.settings)
        except Exception as e:
            self._log("error", f"保存默认设置失败: {e}")
            return
        self._load_ui_from_settings()
        self._log("info", "已重置为默认设置")

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
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
