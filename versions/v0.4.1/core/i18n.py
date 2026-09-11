"""极简 i18n：四语词条表 + 取值函数。

词条以「简体中文为源语言」，其他语种手工对照。取值时若目标语种缺失，
回退到简体中文，再回退到 key 本身（保证界面永远不会因为缺词条而空白）。
"""

from typing import Dict

LANGS: list[tuple[str, str]] = [
    ("zh_CN", "简体中文"),
    ("zh_TW", "繁體中文"),
    ("en", "English"),
    ("ja", "日本語"),
]
DEFAULT_LANG = "zh_CN"


def lang_code_list() -> list[str]:
    return [c for c, _ in LANGS]


def normalize(code: str) -> str:
    code = (code or "").strip()
    return code if code in lang_code_list() else DEFAULT_LANG


# --------------------------------------------------------------------------- #
# 词条表
# --------------------------------------------------------------------------- #
S: Dict[str, Dict[str, str]] = {
    # ---- 主菜单 ---- #
    "menu_device": {"zh_CN": "设备设置", "zh_TW": "設備設定", "en": "Device", "ja": "デバイス"},
    "menu_video": {"zh_CN": "视频与渲染", "zh_TW": "影像與渲染", "en": "Video", "ja": "映像"},
    "menu_check": {"zh_CN": "检查与日志", "zh_TW": "檢查與日誌", "en": "Diagnostics", "ja": "診断"},
    "menu_about": {"zh_CN": "关于", "zh_TW": "關於", "en": "About", "ja": "情報"},

    # ---- 状态 ---- #
    "status_idle": {"zh_CN": "未连接", "zh_TW": "未連線", "en": "Not connected", "ja": "未接続"},
    "status_waiting": {"zh_CN": "等待设备连接…", "zh_TW": "等待裝置連線…",
                       "en": "Waiting for device…", "ja": "接続待機中…"},
    "status_connected": {"zh_CN": "已连接，正在接收投屏", "zh_TW": "已連線，正在接收投影",
                         "en": "Connected, receiving", "ja": "接続中、受信中"},
    "status_error": {"zh_CN": "出错", "zh_TW": "發生錯誤", "en": "Error", "ja": "エラー"},
    "device_prefix": {"zh_CN": "设备名：", "zh_TW": "裝置名稱：",
                      "en": "Name: ", "ja": "デバイス名："},

    # ---- 通用 ---- #
    "back": {"zh_CN": "返回", "zh_TW": "返回", "en": "Back", "ja": "戻る"},
    "save": {"zh_CN": "保存设置", "zh_TW": "儲存設定", "en": "Save", "ja": "保存"},
    "saved": {"zh_CN": "已保存 ✓", "zh_TW": "已儲存 ✓", "en": "Saved ✓", "ja": "保存しました ✓"},
    "saved_restart": {"zh_CN": "已保存，正在重启接收…", "zh_TW": "已儲存，正在重新啟動接收…",
                      "en": "Saved, restarting…", "ja": "保存しました，再起動中…"},

    # ---- 设备设置 ---- #
    "name_label": {"zh_CN": "设备名称", "zh_TW": "裝置名稱", "en": "Device name", "ja": "デバイス名"},
    "name_ph": {"zh_CN": "iPhone 上显示的名字", "zh_TW": "iPhone 上顯示的名稱",
                "en": "Shown on iPhone", "ja": "iPhone に表示される名前"},
    "append_host": {"zh_CN": "在名称后追加主机名", "zh_TW": "在名稱後附加主機名稱",
                    "en": "Append hostname", "ja": "ホスト名を追加"},
    "autostart": {"zh_CN": "启动 App 时自动开始接收", "zh_TW": "啟動 App 時自動開始接收",
                  "en": "Start receiving on launch", "ja": "起動時に自動受信"},
    "language": {"zh_CN": "界面语言", "zh_TW": "介面語言", "en": "Language", "ja": "言語"},

    # ---- 视频与渲染 ---- #
    "fps": {"zh_CN": "帧率", "zh_TW": "幀率", "en": "Frame rate", "ja": "フレームレート"},
    "video_sink": {"zh_CN": "视频后端", "zh_TW": "影像後端", "en": "Video sink", "ja": "映像シンク"},
    "resolution": {"zh_CN": "渲染分辨率", "zh_TW": "渲染解析度", "en": "Resolution", "ja": "解像度"},
    "display_mode": {"zh_CN": "显示模式", "zh_TW": "顯示模式", "en": "Display mode", "ja": "表示モード"},
    "decoder": {"zh_CN": "解码器", "zh_TW": "解碼器", "en": "Decoder", "ja": "デコーダ"},
    "reset_label": {"zh_CN": "无响应重置（秒）", "zh_TW": "無回應重置（秒）",
                    "en": "Reset if unresponsive (s)", "ja": "無応答リセット（秒）"},
    "reset_hint": {"zh_CN": "无响应超过「数值 × 3」秒后自动重置",
                   "zh_TW": "無回應超過「數值 × 3」秒後自動重置",
                   "en": "Resets after value × 3 seconds of silence",
                   "ja": "「値 × 3」秒応答がなければ自動リセット"},
    "keep_window": {"zh_CN": "断开后保持窗口不关闭", "zh_TW": "斷線後保持視窗不關閉",
                    "en": "Keep window after disconnect", "ja": "切断後も窓を閉じない"},
    "legacy_ports": {"zh_CN": "使用旧端口", "zh_TW": "使用舊連接埠",
                     "en": "Legacy ports", "ja": "旧ポート"},
    "extra": {"zh_CN": "额外参数", "zh_TW": "額外參數", "en": "Extra args", "ja": "追加引数"},
    "extra_ph": {"zh_CN": "追加到命令行的原始参数", "zh_TW": "附加到命令列的原始參數",
                 "en": "Raw args appended to command", "ja": "コマンドに追加する引数"},
    "tip_video": {"zh_CN": "30 fps + ximagesink 是当前最低延迟组合。",
                  "zh_TW": "30 fps + ximagesink 是目前最低延遲組合。",
                  "en": "30 fps + ximagesink is the lowest-latency combo.",
                  "ja": "30 fps + ximagesink が最も低遅延です。"},

    "restore_rec": {"zh_CN": "恢复推荐画质", "zh_TW": "恢復推薦畫質",
                    "en": "Restore recommended", "ja": "推奨設定に戻す"},
    "none_opt": {"zh_CN": "关闭", "zh_TW": "關閉", "en": "Off", "ja": "オフ"},
    "dm_auto": {"zh_CN": "自动", "zh_TW": "自動", "en": "Auto", "ja": "自動"},
    "dm_full": {"zh_CN": "全屏", "zh_TW": "全螢幕", "en": "Fullscreen", "ja": "全画面"},
    "dm_win": {"zh_CN": "窗口", "zh_TW": "視窗", "en": "Window", "ja": "ウィンドウ"},
    "dec_auto": {"zh_CN": "自动", "zh_TW": "自動", "en": "Auto", "ja": "自動"},
    "dec_sw": {"zh_CN": "软件解码", "zh_TW": "軟體解碼", "en": "Software", "ja": "ソフトウェア"},
    "dec_vaapi": {"zh_CN": "VAAPI 硬件解码", "zh_TW": "VAAPI 硬體解碼",
                  "en": "VAAPI (hardware)", "ja": "VAAPI（ハードウェア）"},
    "dec_v4l2": {"zh_CN": "V4L2 硬件解码", "zh_TW": "V4L2 硬體解碼",
                 "en": "V4L2 (hardware)", "ja": "V4L2（ハードウェア）"},

    # ---- uxplay 与容器 ---- #
    "sec_uxplay": {"zh_CN": "uxplay 与容器", "zh_TW": "uxplay 與容器",
                   "en": "uxplay & container", "ja": "uxplay とコンテナ"},
    "ux_path": {"zh_CN": "uxplay 路径", "zh_TW": "uxplay 路徑",
                "en": "uxplay path", "ja": "uxplay のパス"},
    "ux_path_ph": {"zh_CN": "留空 = 用容器 / PATH 里的 uxplay", "zh_TW": "留空 = 使用容器 / PATH 內的 uxplay",
                   "en": "Blank = container / PATH uxplay", "ja": "空欄 = コンテナ / PATH の uxplay"},
    "use_dbox": {"zh_CN": "通过容器运行 uxplay", "zh_TW": "透過容器執行 uxplay",
                 "en": "Run uxplay in container", "ja": "コンテナで uxplay を実行"},
    "dbox_name": {"zh_CN": "容器名", "zh_TW": "容器名稱", "en": "Container", "ja": "コンテナ名"},
    "dbox_method": {"zh_CN": "进入容器方式", "zh_TW": "進入容器方式",
                    "en": "Container method", "ja": "コンテナ実行方式"},
    "method_enter": {"zh_CN": "distrobox enter（默认）", "zh_TW": "distrobox enter（預設）",
                     "en": "distrobox enter (default)", "ja": "distrobox enter（既定）"},
    "method_podman": {"zh_CN": "podman 直接执行（免授权）", "zh_TW": "podman 直接執行（免授權）",
                      "en": "podman exec (no auth)", "ja": "podman 直接実行（認証不要）"},
    "tip_dbox": {"zh_CN": "游戏模式会自动改用 podman 免授权方式。",
                 "zh_TW": "遊戲模式會自動改用 podman 免授權方式。",
                 "en": "Game mode auto-uses the podman method.",
                 "ja": "ゲームモードでは自動的に podman 方式を使用します。"},

    "btn_check": {"zh_CN": "检查 uxplay", "zh_TW": "檢查 uxplay",
                  "en": "Check uxplay", "ja": "uxplay を確認"},
    "btn_test": {"zh_CN": "测试容器", "zh_TW": "測試容器", "en": "Test container", "ja": "コンテナをテスト"},
    "btn_export": {"zh_CN": "导出日志", "zh_TW": "匯出日誌", "en": "Export log", "ja": "ログを書き出し"},
    "btn_reset": {"zh_CN": "重置选项", "zh_TW": "重置選項", "en": "Reset", "ja": "リセット"},
    "log_title": {"zh_CN": "运行日志", "zh_TW": "執行日誌", "en": "Log", "ja": "ログ"},

    # ---- 浮动控制条（游戏模式） ---- #
    "float_stop": {"zh_CN": "停止接收", "zh_TW": "停止接收", "en": "Stop", "ja": "停止"},
    "float_open": {"zh_CN": "设置", "zh_TW": "設定", "en": "Settings", "ja": "設定"},

    # ---- 弹窗 ---- #
    "reset_title": {"zh_CN": "重置选项", "zh_TW": "重置選項", "en": "Reset", "ja": "リセット"},
    "reset_body": {"zh_CN": "确定将所有设置恢复为默认值？", "zh_TW": "確定將所有設定恢復為預設值？",
                   "en": "Restore all settings to defaults?", "ja": "すべての設定を初期化しますか？"},
    "export_title": {"zh_CN": "导出完成", "zh_TW": "匯出完成", "en": "Export done", "ja": "書き出し完了"},
    "export_body": {"zh_CN": "诊断日志已保存到：", "zh_TW": "診斷日誌已儲存至：",
                    "en": "Diagnostic log saved to:", "ja": "ログを保存しました："},
    "export_fail": {"zh_CN": "导出失败", "zh_TW": "匯出失敗", "en": "Export failed", "ja": "書き出し失敗"},
    "export_empty": {"zh_CN": "日志为空", "zh_TW": "日誌為空", "en": "Log is empty", "ja": "ログは空です"},

    # ---- 托盘 ---- #
    "tray_show": {"zh_CN": "显示窗口", "zh_TW": "顯示視窗", "en": "Show window", "ja": "ウィンドウを表示"},
    "tray_stop": {"zh_CN": "停止接收", "zh_TW": "停止接收", "en": "Stop", "ja": "停止"},
    "tray_quit": {"zh_CN": "退出", "zh_TW": "結束", "en": "Quit", "ja": "終了"},

    # ---- 关于 ---- #
    "about_version": {"zh_CN": "版本", "zh_TW": "版本", "en": "Version", "ja": "バージョン"},
    "about_body": {
        "zh_CN": (
            "把 Steam Deck / Linux 桌面变成 AirPlay 接收端，包装 UxPlay 引擎。\n\n"
            "1. 在「检查与日志」确认容器可用。\n"
            "2. 回主页，打开开关开始接收。\n"
            "3. iPhone / iPad / Mac 控制中心 → 屏幕镜像 → 选本机名称。\n"
            "4. 游戏模式：Steam 添加非 Steam 游戏 → 选本 AppImage。\n\n"
            "游戏模式顶部会有一条悬浮控制条，可随时停止或回到设置。"
        ),
        "zh_TW": (
            "把 Steam Deck / Linux 桌面變成 AirPlay 接收端，包裝 UxPlay 引擎。\n\n"
            "1. 在「檢查與日誌」確認容器可用。\n"
            "2. 回主頁，打開開關開始接收。\n"
            "3. iPhone / iPad / Mac 控制中心 → 螢幕鏡像 → 選本機名稱。\n"
            "4. 遊戲模式：Steam 加入非 Steam 遊戲 → 選本 AppImage。\n\n"
            "遊戲模式頂部會有一條懸浮控制條，可隨時停止或回到設定。"
        ),
        "en": (
            "Turn your Steam Deck / Linux desktop into an AirPlay receiver, powered by UxPlay.\n\n"
            "1. Verify the container in Diagnostics.\n"
            "2. Back on Home, flip the switch to start.\n"
            "3. iPhone / iPad / Mac → Control Center → Screen Mirroring → pick this device.\n"
            "4. Game mode: add this AppImage as a Non-Steam Game.\n\n"
            "A floating bar appears at the top in game mode for Stop / Settings."
        ),
        "ja": (
            "Steam Deck / Linux を AirPlay 受信機にします（UxPlay 使用）。\n\n"
            "1. 「診断」でコンテナを確認。\n"
            "2. ホームに戻り、スイッチをオン。\n"
            "3. iPhone / iPad / Mac のコントロールセンター → 画面ミラーリング → 本機を選択。\n"
            "4. ゲームモード：非 Steam ゲームとして本 AppImage を追加。\n\n"
            "ゲームモードでは上部に操作バーが表示されます。"
        ),
    },

    # ---- 日志文案 ---- #
    "log_started": {"zh_CN": "AirPlay Deck v{ver} 已启动", "zh_TW": "AirPlay Deck v{ver} 已啟動",
                    "en": "AirPlay Deck v{ver} started", "ja": "AirPlay Deck v{ver} を起動しました"},
    "log_autostart": {"zh_CN": "已开启开机自启，正在自动启动…", "zh_TW": "已開啟自動啟動，正在自動啟動…",
                      "en": "Autostart enabled, starting…", "ja": "自動起動が有効，開始します…"},
    "log_saved": {"zh_CN": "设置已保存", "zh_TW": "設定已儲存", "en": "Settings saved",
                  "ja": "設定を保存しました"},
    "log_reset_done": {"zh_CN": "已重置为默认设置", "zh_TW": "已重置為預設設定",
                       "en": "Restored defaults", "ja": "初期設定に戻しました"},
}


def t(key: str, lang: str = DEFAULT_LANG, **kw) -> str:
    d = S.get(key)
    if not d:
        return key
    s = d.get(lang) or d.get(DEFAULT_LANG) or key
    try:
        return s.format(**kw) if kw else s
    except Exception:
        return s
