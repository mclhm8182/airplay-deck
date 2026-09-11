"""极简 i18n：八语词条表 + 取值函数。

词条以「简体中文为源语言」，其他语种手工对照。取值时若目标语种缺失，
回退到简体中文，再回退到 key 本身（保证界面永远不会因为缺词条而空白）。
"""

from typing import Dict

LANGS: list[tuple[str, str]] = [
    ("zh_CN", "简体中文"),
    ("zh_TW", "繁體中文"),
    ("en", "English"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("es", "Español"),
]
DEFAULT_LANG = "zh_CN"


def lang_code_list() -> list[str]:
    return [c for c, _ in LANGS]


def normalize(code: str) -> str:
    code = (code or "").strip()
    return code if code in lang_code_list() else DEFAULT_LANG


def detect_system_lang() -> str:
    """按系统区域推断受支持的语言；不在支持列表则回退英语。

    规则：zh + 地区 tw/hk/mo → 繁中，其余（cn/sg/无地区）→ 简中；
    ja → 日；ko → 韩；en → 英；fr/de/es → 对应；其它 → 英。
    """
    import os
    raw = (os.environ.get("LC_ALL")
           or os.environ.get("LC_MESSAGES")
           or os.environ.get("LANG")
           or "").strip().lower()
    parts = [p for p in raw.split(".")[0].replace("-", "_").split("_") if p]
    if not parts:
        return "en"
    primary = parts[0]
    region = parts[1] if len(parts) > 1 else ""
    if primary == "zh":
        return "zh_TW" if region in ("tw", "hk", "mo") else "zh_CN"
    if primary in ("ja", "ko", "en", "fr", "de", "es"):
        return primary
    return "en"


def resolve_lang(raw: str) -> str:
    """把设置里的语言值解析成实际生效的语言。

    "auto"（或缺失）→ 跟随系统检测；具体语言码 → 规范化；
    非法值 → 简中兜底（与 settings 默认值一致）。
    """
    if not raw or raw == "auto":
        return detect_system_lang()
    return normalize(raw)


# --------------------------------------------------------------------------- #
# 词条表
# --------------------------------------------------------------------------- #
S: Dict[str, Dict[str, str]] = {
    # ---- 主菜单 ---- #
    "menu_device": {"zh_CN": "设备设置", "zh_TW": "設備設定", "en": "Device", "ja": "デバイス",
                    "ko": "기기 설정", "fr": "Appareil", "de": "Gerät", "es": "Dispositivo"},
    "menu_video": {"zh_CN": "视频与渲染", "zh_TW": "影像與渲染", "en": "Video", "ja": "映像",
                   "ko": "영상 및 렌더링", "fr": "Vidéo et rendu", "de": "Video & Rendering", "es": "Vídeo y renderizado"},
    "menu_check": {"zh_CN": "检查与日志", "zh_TW": "檢查與日誌", "en": "Diagnostics", "ja": "診断",
                   "ko": "환경 확인 및 로그", "fr": "Diagnostic", "de": "Diagnose", "es": "Diagnóstico"},
    "menu_about": {"zh_CN": "关于", "zh_TW": "關於", "en": "About", "ja": "情報",
                   "ko": "정보", "fr": "À propos", "de": "Über", "es": "Acerca de"},

    # ---- 状态 ---- #
    "status_idle": {"zh_CN": "未连接", "zh_TW": "未連線", "en": "Not connected", "ja": "未接続",
                    "ko": "연결 안 됨", "fr": "Non connecté", "de": "Nicht verbunden", "es": "No conectado"},
    "status_waiting": {"zh_CN": "等待设备连接…", "zh_TW": "等待裝置連線…",
                       "en": "Waiting for device…", "ja": "接続待機中…",
                       "ko": "기기 연결 대기 중…", "fr": "En attente de l'appareil…",
                       "de": "Warte auf Gerät…", "es": "Esperando dispositivo…"},
    "status_connected": {"zh_CN": "已连接，正在接收投屏", "zh_TW": "已連線，正在接收投影",
                         "en": "Connected, receiving", "ja": "接続中、受信中",
                         "ko": "연결됨, 화면 수신 중", "fr": "Connecté, réception en cours",
                         "de": "Verbunden, empfange", "es": "Conectado, recibiendo"},
    "status_error": {"zh_CN": "出错", "zh_TW": "發生錯誤", "en": "Error", "ja": "エラー",
                     "ko": "오류", "fr": "Erreur", "de": "Fehler", "es": "Error"},
    "device_prefix": {"zh_CN": "设备名：", "zh_TW": "裝置名稱：",
                      "en": "Name: ", "ja": "デバイス名：",
                      "ko": "기기 이름: ", "fr": "Nom : ", "de": "Name: ", "es": "Nombre: "},

    # ---- 通用 ---- #
    "back": {"zh_CN": "返回", "zh_TW": "返回", "en": "Back", "ja": "戻る",
             "ko": "뒤로", "fr": "Retour", "de": "Zurück", "es": "Atrás"},
    "save": {"zh_CN": "保存设置", "zh_TW": "儲存設定", "en": "Save", "ja": "保存",
             "ko": "설정 저장", "fr": "Enregistrer", "de": "Speichern", "es": "Guardar"},
    "saved": {"zh_CN": "已保存 ✓", "zh_TW": "已儲存 ✓", "en": "Saved ✓", "ja": "保存しました ✓",
              "ko": "저장됨 ✓", "fr": "Enregistré ✓", "de": "Gespeichert ✓", "es": "Guardado ✓"},
    "saved_restart": {"zh_CN": "已保存，正在重启接收…", "zh_TW": "已儲存，正在重新啟動接收…",
                      "en": "Saved, restarting…", "ja": "保存しました，再起動中…",
                      "ko": "저장됨, 수신 재시작 중…", "fr": "Enregistré, redémarrage…",
                      "de": "Gespeichert, neu starten…", "es": "Guardado, reiniciando…"},

    # ---- 设备设置 ---- #
    "name_label": {"zh_CN": "设备名称", "zh_TW": "裝置名稱", "en": "Device name", "ja": "デバイス名",
                   "ko": "기기 이름", "fr": "Nom de l'appareil", "de": "Gerätename", "es": "Nombre del dispositivo"},
    "name_ph": {"zh_CN": "iPhone 上显示的名字", "zh_TW": "iPhone 上顯示的名稱",
                "en": "Shown on iPhone", "ja": "iPhone に表示される名前",
                "ko": "iPhone에 표시되는 이름", "fr": "Affiché sur iPhone", "de": "Auf iPhone angezeigt", "es": "Mostrado en iPhone"},
    "append_host": {"zh_CN": "在名称后追加主机名", "zh_TW": "在名稱後附加主機名稱",
                    "en": "Append hostname", "ja": "ホスト名を追加",
                    "ko": "이름 뒤에 호스트명 추가", "fr": "Ajouter le nom d'hôte", "de": "Hostname anhängen", "es": "Añadir nombre de host"},
    "append_host_preview": {"zh_CN": "iPhone 屏幕镜像中将显示为：{name}", "zh_TW": "iPhone 螢幕鏡射中將顯示為：{name}",
                            "en": "Will appear in iPhone Screen Mirroring as: {name}", "ja": "iPhone の画面共有に表示される名前：{name}",
                            "ko": "iPhone 화면 미러링에 표시되는 이름: {name}", "fr": "S'affichera dans Partage d'écran iPhone comme : {name}",
                            "de": "Erscheint in iPhone-Bildschirmfreigabe als: {name}", "es": "Aparecerá en Compartir pantalla de iPhone como: {name}"},
    "autostart": {"zh_CN": "启动 App 时自动开始接收", "zh_TW": "啟動 App 時自動開始接收",
                  "en": "Start receiving on launch", "ja": "起動時に自動受信",
                  "ko": "앱 시작 시 자동 수신 시작", "fr": "Démarrer la réception au lancement",
                  "de": "Beim Start automatisch empfangen", "es": "Iniciar recepción al arrancar"},
    "language": {"zh_CN": "界面语言", "zh_TW": "介面語言", "en": "Language", "ja": "言語",
                 "ko": "인터페이스 언어", "fr": "Langue", "de": "Sprache", "es": "Idioma"},
    "lang_auto": {"zh_CN": "自动（跟随系统）", "zh_TW": "自動（跟隨系統）",
                  "en": "Auto (follow system)", "ja": "自動（システムに従う）",
                  "ko": "자동(시스템 따름)", "fr": "Auto (système)", "de": "Auto (System)", "es": "Auto (sistema)"},

    # ---- 视频与渲染 ---- #
    "fps": {"zh_CN": "帧率", "zh_TW": "幀率", "en": "Frame rate", "ja": "フレームレート",
            "ko": "프레임 레이트", "fr": "Images par seconde", "de": "Bildrate", "es": "Cuadros por segundo"},
    "video_sink": {"zh_CN": "视频后端", "zh_TW": "影像後端", "en": "Video sink", "ja": "映像シンク",
                   "ko": "비디오 싱크", "fr": "Sortie vidéo", "de": "Video-Sink", "es": "Salida de vídeo"},
    "resolution": {"zh_CN": "渲染分辨率", "zh_TW": "渲染解析度", "en": "Resolution", "ja": "解像度",
                   "ko": "렌더링 해상도", "fr": "Résolution", "de": "Auflösung", "es": "Resolución"},
    "display_mode": {"zh_CN": "显示模式", "zh_TW": "顯示模式", "en": "Display mode", "ja": "表示モード",
                     "ko": "화면 모드", "fr": "Mode d'affichage", "de": "Anzeigemodus", "es": "Modo de pantalla"},
    "decoder": {"zh_CN": "解码器", "zh_TW": "解碼器", "en": "Decoder", "ja": "デコーダ",
                "ko": "디코더", "fr": "Décodeur", "de": "Decoder", "es": "Descodificador"},
    "keep_window": {"zh_CN": "断开后保持窗口不关闭", "zh_TW": "斷線後保持視窗不關閉",
                    "en": "Keep window after disconnect", "ja": "切断後も窓を閉じない",
                    "ko": "연결 끊긴 후 창 유지", "fr": "Garder la fenêtre après déconnexion",
                    "de": "Fenster nach Trennung behalten", "es": "Mantener ventana tras desconexión"},
    "legacy_ports": {"zh_CN": "使用旧端口", "zh_TW": "使用舊連接埠",
                     "en": "Legacy ports", "ja": "旧ポート",
                     "ko": "레거시 포트 사용", "fr": "Ports hérités", "de": "Alte Ports", "es": "Puertos antiguos"},
    "extra": {"zh_CN": "额外参数", "zh_TW": "額外參數", "en": "Extra args", "ja": "追加引数",
              "ko": "추가 인수", "fr": "Arguments supplémentaires", "de": "Zusätzliche Argumente", "es": "Argumentos extra"},
    "extra_ph": {"zh_CN": "追加到命令行的原始参数", "zh_TW": "附加到命令列的原始參數",
                 "en": "Raw args appended to command", "ja": "コマンドに追加する引数",
                 "ko": "명령행에 추가되는 원시 인수", "fr": "Arguments ajoutés à la commande",
                 "de": "Roh-Argumente an Befehl", "es": "Argumentos crudos añadidos"},
    "restore_rec": {"zh_CN": "恢复推荐画质", "zh_TW": "恢復推薦畫質",
                    "en": "Restore recommended", "ja": "推奨設定に戻す",
                    "ko": "권장 설정 복원", "fr": "Restaurer recommandé", "de": "Empfohlenes wiederherstellen", "es": "Restaurar recomendado"},
    "none_opt": {"zh_CN": "关闭", "zh_TW": "關閉", "en": "Off", "ja": "オフ",
                 "ko": "끄기", "fr": "Désactivé", "de": "Aus", "es": "Apagado"},
    "dm_auto": {"zh_CN": "自动", "zh_TW": "自動", "en": "Auto", "ja": "自動",
                "ko": "자동", "fr": "Auto", "de": "Auto", "es": "Auto"},
    "dm_full": {"zh_CN": "全屏", "zh_TW": "全螢幕", "en": "Fullscreen", "ja": "全画面",
                "ko": "전체화면", "fr": "Plein écran", "de": "Vollbild", "es": "Pantalla completa"},
    "dm_win": {"zh_CN": "窗口", "zh_TW": "視窗", "en": "Window", "ja": "ウィンドウ",
               "ko": "창", "fr": "Fenêtre", "de": "Fenster", "es": "Ventana"},
    "anti_sleep": {"zh_CN": "投屏时保持屏幕常亮", "zh_TW": "投影時保持螢幕常亮",
                   "en": "Keep screen awake while mirroring", "ja": "投屏中は画面を常時点灯",
                   "ko": "미러링 중 화면 켜짐 유지", "fr": "Garder l'écran allumé pendant la diffusion",
                   "de": "Bildschirm während Spiegelung aktiv", "es": "Mantener pantalla encendida al emitir"},
    "audio_sync": {"zh_CN": "音频同步", "zh_TW": "音訊同步", "en": "Audio sync", "ja": "音声同期",
                   "ko": "오디오 동기화", "fr": "Sync audio", "de": "Audio-Sync", "es": "Sincronización de audio"},
    "audio_sync_hint": {"zh_CN": "关闭后改为即时出声，修掉刷短视频时声音晚几秒才跟上的问题",
                        "zh_TW": "關閉後改為即時出聲，修掉刷短影音時聲音晚幾秒才跟上的問題",
                        "en": "Off = instant audio, fixes the delay when a new short video starts",
                        "ja": "オフで即時音声に。短い動画の切り替え時の遅延を解消",
                        "ko": "끄면 즉시 오디오. 짧은 동영상 전환 시 지연 해결",
                        "fr": "Désactivé = audio instantané, corrige le décalage au changement de vidéo",
                        "de": "Aus = sofortiger Ton, behebt Verzögerung beim Videowechsel",
                        "es": "Apagado = audio instantáneo, corrige el retraso al cambiar de vídeo"},
    "dec_auto": {"zh_CN": "自动", "zh_TW": "自動", "en": "Auto", "ja": "自動",
                 "ko": "자동", "fr": "Auto", "de": "Auto", "es": "Auto"},
    "dec_sw": {"zh_CN": "软件解码", "zh_TW": "軟體解碼", "en": "Software", "ja": "ソフトウェア",
               "ko": "소프트웨어", "fr": "Logiciel", "de": "Software", "es": "Software"},
    "dec_vaapi": {"zh_CN": "VAAPI 硬件解码", "zh_TW": "VAAPI 硬體解碼",
                  "en": "VAAPI (hardware)", "ja": "VAAPI（ハードウェア）",
                  "ko": "VAAPI (하드웨어)", "fr": "VAAPI (matériel)", "de": "VAAPI (Hardware)", "es": "VAAPI (hardware)"},
    "dec_v4l2": {"zh_CN": "V4L2 硬件解码", "zh_TW": "V4L2 硬體解碼",
                 "en": "V4L2 (hardware)", "ja": "V4L2（ハードウェア）",
                 "ko": "V4L2 (하드웨어)", "fr": "V4L2 (matériel)", "de": "V4L2 (Hardware)", "es": "V4L2 (hardware)"},

    # ---- uxplay 与容器 ---- #
    "sec_uxplay": {"zh_CN": "uxplay 与容器", "zh_TW": "uxplay 與容器",
                   "en": "uxplay & container", "ja": "uxplay とコンテナ",
                   "ko": "uxplay 및 컨테이너", "fr": "uxplay et conteneur", "de": "uxplay & Container", "es": "uxplay y contenedor"},
    "use_dbox": {"zh_CN": "通过容器运行 uxplay", "zh_TW": "透過容器執行 uxplay",
                 "en": "Run uxplay in container", "ja": "コンテナで uxplay を実行",
                 "ko": "컨테이너에서 uxplay 실행", "fr": "Exécuter uxplay dans un conteneur",
                 "de": "uxplay im Container ausführen", "es": "Ejecutar uxplay en contenedor"},
    "dbox_method": {"zh_CN": "进入容器方式", "zh_TW": "進入容器方式",
                    "en": "Container method", "ja": "コンテナ実行方式",
                    "ko": "컨테이너 진입 방식", "fr": "Méthode de conteneur", "de": "Container-Methode", "es": "Método de contenedor"},
    "method_enter": {"zh_CN": "distrobox enter（默认）", "zh_TW": "distrobox enter（預設）",
                     "en": "distrobox enter (default)", "ja": "distrobox enter（既定）",
                     "ko": "distrobox enter (기본)", "fr": "distrobox enter (par défaut)",
                     "de": "distrobox enter (Standard)", "es": "distrobox enter (predeterminado)"},
    "method_podman": {"zh_CN": "podman 直接执行（免授权）", "zh_TW": "podman 直接執行（免授權）",
                      "en": "podman exec (no auth)", "ja": "podman 直接実行（認証不要）",
                      "ko": "podman 직접 실행(권한 불필요)", "fr": "podman exec (sans auth)",
                      "de": "podman exec (ohne Auth)", "es": "podman exec (sin auth)"},
    "btn_check_env": {"zh_CN": "一键检查运行环境", "zh_TW": "一鍵檢查執行環境",
                      "en": "Check runtime", "ja": "実行環境を一括確認",
                      "ko": "실행 환경 한 번에 확인", "fr": "Vérifier l'environnement", "de": "Umgebung prüfen", "es": "Comprobar entorno"},
    "btn_export": {"zh_CN": "导出日志", "zh_TW": "匯出日誌", "en": "Export log", "ja": "ログを書き出し",
                   "ko": "로그 내보내기", "fr": "Exporter le journal", "de": "Log exportieren", "es": "Exportar registro"},
    "btn_reset": {"zh_CN": "重置选项", "zh_TW": "重置選項", "en": "Reset", "ja": "リセット",
                  "ko": "초기화", "fr": "Réinitialiser", "de": "Zurücksetzen", "es": "Restablecer"},
    "log_title": {"zh_CN": "运行日志", "zh_TW": "執行日誌", "en": "Log", "ja": "ログ",
                  "ko": "실행 로그", "fr": "Journal", "de": "Protokoll", "es": "Registro"},

    # ---- 运行环境（首启自动安装 / 一键卸载） ---- #
    "env_ready": {"zh_CN": "运行环境已就绪（容器 {c}）",
                  "zh_TW": "執行環境已就緒（容器 {c}）",
                  "en": "Runtime ready (container {c})",
                  "ja": "実行環境準備完了（コンテナ {c}）",
                  "ko": "실행 환경 준비 완료(컨테이너 {c})",
                  "fr": "Environnement prêt (conteneur {c})",
                  "de": "Laufzeit bereit (Container {c})",
                  "es": "Entorno listo (contenedor {c})"},
    "env_missing": {"zh_CN": "运行环境未安装", "zh_TW": "執行環境尚未安裝",
                    "en": "Runtime not installed", "ja": "実行環境が未インストール",
                    "ko": "실행 환경 미설치", "fr": "Environnement non installé",
                    "de": "Laufzeit nicht installiert", "es": "Entorno no instalado"},
    "env_card": {"zh_CN": "运行环境（一键安装 / 卸载）", "zh_TW": "執行環境（一鍵安裝 / 解除）",
                 "en": "Runtime (install / uninstall)", "ja": "実行環境（インストール / 削除）",
                 "ko": "실행 환경(설치 / 제거)", "fr": "Environnement (installer / désinstaller)",
                 "de": "Laufzeit (installieren / deinstallieren)", "es": "Entorno (instalar / desinstalar)"},
    "btn_install_env": {"zh_CN": "安装 / 重建运行环境", "zh_TW": "安裝 / 重建執行環境",
                        "en": "Install / rebuild runtime", "ja": "実行環境をインストール / 再構築",
                        "ko": "실행 환경 설치 / 재구성", "fr": "Installer / reconstruire l'environnement",
                        "de": "Laufzeit installieren / neu aufbauen", "es": "Instalar / reconstruir entorno"},
    "btn_uninstall": {"zh_CN": "一键卸载环境", "zh_TW": "一鍵解除安裝環境",
                      "en": "Uninstall runtime", "ja": "実行環境をアンインストール",
                      "ko": "실행 환경 제거", "fr": "Désinstaller l'environnement",
                      "de": "Laufzeit deinstallieren", "es": "Desinstalar entorno"},
    "tip_env": {"zh_CN": "首次打开会自动提示安装投屏运行环境（约 400–800 MB）。卸载会删除容器、镜像与应用数据，清爽如初；AppImage 自身不会被删除。",
                "zh_TW": "首次開啟會自動提示安裝投影執行環境（約 400–800 MB）。解除會刪除容器、映像與應用資料，清爽如初；AppImage 本身不會被刪除。",
                "en": "First launch auto-prompts to install the runtime (~400–800 MB). Uninstall removes container, image and app data; the AppImage itself is kept.",
                "ja": "初回起動時に実行環境のインストールを自動案内（約 400–800 MB）。アンインストールはコンテナ・イメージ・アプリデータを削除します。AppImage 本体は残ります。",
                "ko": "최초 실행 시 화면 미러링 실행 환경 설치를 자동으로 안내합니다(약 400–800 MB). 제거 시 컨테이너·이미지·앱 데이터를 삭제하며 AppImage 자체는 남습니다.",
                "fr": "Au premier lancement, invite à installer l'environnement (~400–800 Mo). La désinstallation supprime conteneur, image et données ; l'AppImage reste.",
                "de": "Beim ersten Start wird zur Installation aufgefordert (~400–800 MB). Deinstallation entfernt Container, Image und Daten; die AppImage bleibt.",
                "es": "Al primer inicio pide instalar el entorno (~400–800 MB). Desinstalar elimina contenedor, imagen y datos; la AppImage se conserva."},
    "bootstrap_title": {"zh_CN": "首次设置：安装投屏运行环境", "zh_TW": "首次設定：安裝投影執行環境",
                        "en": "First-time setup: install runtime", "ja": "初回設定：実行環境のインストール",
                        "ko": "최초 설정: 화면 미러링 실행 환경 설치", "fr": "Première configuration : installer l'environnement",
                        "de": "Ersteinrichtung: Laufzeit installieren", "es": "Primera configuración: instalar entorno"},
    "bootstrap_body": {"zh_CN": "本程序需要 uxplay 才能接收 AirPlay。将自动创建容器 {c} 并在其中安装 uxplay 与 GStreamer（首次需联网下载约 400–800 MB，请保持网络畅通）。现在开始吗？",
                       "zh_TW": "本程式需要 uxplay 才能接收 AirPlay。將自動建立容器 {c} 並在其中安裝 uxplay 與 GStreamer（首次需聯網下載約 400–800 MB，請保持網路暢通）。現在開始嗎？",
                       "en": "This app needs uxplay to receive AirPlay. It will auto-create container {c} and install uxplay + GStreamer inside (first run downloads ~400–800 MB; keep your network on). Start now?",
                       "ja": "AirPlay 受信には uxplay が必要です。コンテナ {c} を自動作成し、その中に uxplay と GStreamer をインストールします（初回は約 400–800 MB をダウンロード。ネット接続を維持してください）。今すぐ開始しますか？",
                       "ko": "이 앱은 AirPlay 수신을 위해 uxplay가 필요합니다. 컨테이너 {c}를 자동 생성하고 그 안에 uxplay와 GStreamer를 설치합니다(최초 실행 시 약 400–800 MB 다운로드, 네트워크 연결 필요). 지금 시작할까요?",
                       "fr": "Cette app a besoin d'uxplay pour recevoir AirPlay. Crée le conteneur {c} et installe uxplay + GStreamer (~400–800 Mo, connexion requise). Commencer ?",
                       "de": "Diese App braucht uxplay für AirPlay. Erstellt Container {c} und installiert uxplay + GStreamer (~400–800 MB, Netz nötig). Jetzt starten?",
                       "es": "Esta app necesita uxplay para recibir AirPlay. Crea el contenedor {c} e instala uxplay + GStreamer (~400–800 MB, conexión necesaria). ¿Empezar ahora?"},
    "bootstrap_install": {"zh_CN": "现在下载并安装", "zh_TW": "現在下載並安裝",
                          "en": "Download & install now", "ja": "今すぐダウンロードしてインストール",
                          "ko": "지금 다운로드 및 설치", "fr": "Télécharger et installer", "de": "Jetzt herunterladen & installieren", "es": "Descargar e instalar"},
    "bootstrap_later": {"zh_CN": "稍后再说", "zh_TW": "稍後再說", "en": "Later", "ja": "後で",
                        "ko": "나중에", "fr": "Plus tard", "de": "Später", "es": "Más tarde"},
    "bootstrap_done": {"zh_CN": "运行环境已安装完成，可以开始接收投屏了。",
                       "zh_TW": "執行環境已安裝完成，可以開始接收投影了。",
                       "en": "Runtime installed. You can start receiving now.",
                       "ja": "実行環境のインストールが完了しました。投屏を受信できます。",
                       "ko": "실행 환경 설치 완료. 이제 화면 수신을 시작할 수 있습니다.",
                       "fr": "Environnement installé. Vous pouvez recevoir maintenant.",
                       "de": "Laufzeit installiert. Empfang startbereit.",
                       "es": "Entorno instalado. Ya puedes recibir."},
    "bootstrap_failed": {"zh_CN": "安装未完成：{e}。可稍后在「检查与日志」里重试，或手动安装 uxplay。",
                         "zh_TW": "安裝未完成：{e}。可稍後在「檢查與日誌」裡重試，或手動安裝 uxplay。",
                         "en": "Install incomplete: {e}. Retry later in Diagnostics, or install uxplay manually.",
                         "ja": "インストール未完了：{e}。「診断」から再試行するか、uxplay を手動インストールしてください。",
                         "ko": "설치 미완료: {e}. 나중에 '환경 확인 및 로그'에서 다시 시도하거나 uxplay를 수동 설치하세요.",
                         "fr": "Installation incomplète : {e}. Réessayez dans Diagnostic ou installez uxplay manuellement.",
                         "de": "Installation unvollständig: {e}. Später in Diagnose erneut oder uxplay manuell installieren.",
                         "es": "Instalación incompleta: {e}. Reintenta en Diagnóstico o instala uxplay manualmente."},
    "uninstall_title": {"zh_CN": "一键卸载环境", "zh_TW": "一鍵解除安裝環境",
                        "en": "Uninstall runtime", "ja": "実行環境をアンインストール",
                        "ko": "실행 환경 제거", "fr": "Désinstaller l'environnement",
                        "de": "Laufzeit deinstallieren", "es": "Desinstalar entorno"},
    "uninstall_body": {"zh_CN": "将删除投屏运行环境容器 {c}、其镜像（若不被其它容器占用）以及本程序的所有设置与日志。此操作不可恢复，确定继续？",
                       "zh_TW": "將刪除投影執行環境容器 {c}、其映像（若不被其它容器佔用）以及本程式的所有設定與日誌。此操作不可復原，確定繼續？",
                       "en": "This removes the runtime container {c}, its image (if not used by other containers), and all settings/logs of this app. This cannot be undone. Continue?",
                       "ja": "実行環境コンテナ {c}、そのイメージ（他コンテナが使っていなければ）、および本アプリの設定とログをすべて削除します。元に戻せません。続行しますか？",
                       "ko": "화면 미러링 실행 환경 컨테이너 {c}, 이미지(다른 컨테이너가 사용하지 않으면), 그리고 이 앱의 모든 설정과 로그를 삭제합니다. 되돌릴 수 없습니다. 계속할까요?",
                       "fr": "Supprime le conteneur {c}, son image (si libre) et tous les réglages/logs. Irréversible. Continuer ?",
                       "de": "Entfernt Container {c}, sein Image (falls frei) und alle Einstellungen/Logs. Unwiderruflich. Fortfahren?",
                       "es": "Elimina el contenedor {c}, su imagen (si no se usa) y todos los ajustes/logs. Irreversible. ¿Continuar?"},
    "uninstall_done": {"zh_CN": "已卸载环境：{detail}", "zh_TW": "已解除安裝環境：{detail}",
                       "en": "Runtime removed: {detail}", "ja": "実行環境を削除しました：{detail}",
                       "ko": "실행 환경 제거됨: {detail}", "fr": "Environnement supprimé : {detail}",
                       "de": "Laufzeit entfernt: {detail}", "es": "Entorno eliminado: {detail}"},
    "uninstall_failed": {"zh_CN": "卸载出现问题：{e}", "zh_TW": "解除安裝出現問題：{e}",
                         "en": "Uninstall issue: {e}", "ja": "アンインストールで問題：{e}",
                         "ko": "제거 중 문제: {e}", "fr": "Problème de désinstallation : {e}",
                         "de": "Deinstallationsproblem: {e}", "es": "Problema al desinstalar: {e}"},

    # ---- 弹窗 ---- #
    "reset_title": {"zh_CN": "重置选项", "zh_TW": "重置選項", "en": "Reset", "ja": "リセット",
                    "ko": "초기화", "fr": "Réinitialiser", "de": "Zurücksetzen", "es": "Restablecer"},
    "reset_body": {"zh_CN": "确定将所有设置恢复为默认值？", "zh_TW": "確定將所有設定恢復為預設值？",
                   "en": "Restore all settings to defaults?", "ja": "すべての設定を初期化しますか？",
                   "ko": "모든 설정을 기본값으로 되돌릴까요?", "fr": "Restaurer les réglages par défaut ?",
                   "de": "Alle Einstellungen zurücksetzen?", "es": "¿Restaurar ajustes predeterminados?"},
    "export_title": {"zh_CN": "导出完成", "zh_TW": "匯出完成", "en": "Export done", "ja": "書き出し完了",
                     "ko": "내보내기 완료", "fr": "Exportation terminée", "de": "Export fertig", "es": "Exportación lista"},
    "export_body": {"zh_CN": "诊断日志已保存到：", "zh_TW": "診斷日誌已儲存至：",
                    "en": "Diagnostic log saved to:", "ja": "ログを保存しました：",
                    "ko": "진단 로그 저장됨:", "fr": "Journal de diagnostic enregistré :", "de": "Diagnose-Log gespeichert:", "es": "Registro de diagnóstico guardado:"},
    "export_fail": {"zh_CN": "导出失败", "zh_TW": "匯出失敗", "en": "Export failed", "ja": "書き出し失敗",
                    "ko": "내보내기 실패", "fr": "Échec d'export", "de": "Export fehlgeschlagen", "es": "Error al exportar"},
    "export_empty": {"zh_CN": "日志为空", "zh_TW": "日誌為空", "en": "Log is empty", "ja": "ログは空です",
                     "ko": "로그가 비어 있음", "fr": "Journal vide", "de": "Protokoll leer", "es": "Registro vacío"},

    # ---- 托盘 ---- #
    "tray_show": {"zh_CN": "显示窗口", "zh_TW": "顯示視窗", "en": "Show window", "ja": "ウィンドウを表示",
                  "ko": "창 표시", "fr": "Afficher la fenêtre", "de": "Fenster anzeigen", "es": "Mostrar ventana"},
    "tray_stop": {"zh_CN": "停止接收", "zh_TW": "停止接收", "en": "Stop", "ja": "停止",
                  "ko": "수신 중지", "fr": "Arrêter", "de": "Stopp", "es": "Detener"},
    "tray_quit": {"zh_CN": "退出", "zh_TW": "結束", "en": "Quit", "ja": "終了",
                  "ko": "종료", "fr": "Quitter", "de": "Beenden", "es": "Salir"},

    # ---- 关于 ---- #
    "about_version": {"zh_CN": "版本", "zh_TW": "版本", "en": "Version", "ja": "バージョン",
                      "ko": "버전", "fr": "Version", "de": "Version", "es": "Versión"},
    "about_body": {
        "zh_CN": (
            "把 Steam Deck / Linux 桌面变成 AirPlay 接收端，包装 UxPlay 引擎。\n\n"
            "1. 在「检查与日志」确认容器可用。\n"
            "2. 回主页，打开开关开始接收。\n"
            "3. iPhone / iPad 控制中心 → 屏幕镜像 → 选本机名称。\n"
            "游戏模式（SteamOS 全屏接管）暂未支持，后续版本计划加入；当前请使用桌面模式。"
        ),
        "zh_TW": (
            "把 Steam Deck / Linux 桌面變成 AirPlay 接收端，包裝 UxPlay 引擎。\n\n"
            "1. 在「檢查與日誌」確認容器可用。\n"
            "2. 回主頁，打開開關開始接收。\n"
            "3. iPhone / iPad 控制中心 → 螢幕鏡像 → 選本機名稱。\n"
            "遊戲模式（SteamOS 全螢幕接管）暫未支援，後續版本計劃加入；目前請使用桌面模式。"
        ),
        "en": (
            "Turn your Steam Deck / Linux desktop into an AirPlay receiver, powered by UxPlay.\n\n"
            "1. Verify the container in Diagnostics.\n"
            "2. Back on Home, flip the switch to start.\n"
            "3. iPhone / iPad → Control Center → Screen Mirroring → pick this device.\n"
            "Game mode (full-screen takeover on SteamOS) is not supported yet, but planned for a future release; please use desktop mode for now."
        ),
        "ja": (
            "Steam Deck / Linux を AirPlay 受信機にします（UxPlay 使用）。\n\n"
            "1. 「診断」でコンテナを確認。\n"
            "2. ホームに戻り、スイッチをオン。\n"
            "3. iPhone / iPad のコントロールセンター → 画面ミラーリング → 本機を選択。\n"
            "ゲームモード（SteamOS での全画面占有）はまだ未対応です。今後のバージョンで対応予定です。現時点ではデスクトップモードをご利用ください。"
        ),
        "ko": (
            "Steam Deck / Linux 데스크톱을 AirPlay 수신기로 만듭니다(UxPlay 엔진 기반).\n\n"
            "1. '환경 확인 및 로그'에서 컨테이너 사용 가능 확인.\n"
            "2. 홈으로 돌아가 스위치를 켜고 수신 시작.\n"
            "3. iPhone / iPad 제어 센터 → 화면 미러링 → 이 기기 선택.\n"
            "게임 모드(SteamOS 전체 화면 점유)는 아직 지원되지 않으며, 향후 버전에서 추가할 예정입니다. 현재는 데스크톱 모드를 이용해 주세요."
        ),
        "fr": (
            "Transformez votre Steam Deck / Linux en récepteur AirPlay, via UxPlay.\n\n"
            "1. Vérifiez le conteneur dans Diagnostic.\n"
            "2. De retour à l'accueil, activez pour démarrer.\n"
            "3. iPhone / iPad → Centre de contrôle → Mise en miroir → choisissez cet appareil.\n"
            "Le mode jeu (prise en charge plein écran sur SteamOS) n'est pas encore pris en charge, mais est prévu pour une version future ; utilisez le mode bureau pour l'instant."
        ),
        "de": (
            "Verwandelt Steam Deck / Linux in einen AirPlay-Empfänger (UxPlay).\n\n"
            "1. Container in Diagnose prüfen.\n"
            "2. Zurück zur Startseite, Schalter aktivieren.\n"
            "3. iPhone / iPad → Kontrollzentrum → Bildschirmspiegelung → dieses Gerät.\n"
            "Der Spielmodus (Vollbild-Übernahme auf SteamOS) wird noch nicht unterstützt, ist aber für eine künftige Version geplant; nutzen Sie vorerst den Desktop-Modus."
        ),
        "es": (
            "Convierte tu Steam Deck / Linux en un receptor AirPlay con UxPlay.\n\n"
            "1. Verifica el contenedor en Diagnóstico.\n"
            "2. Vuelve al inicio y activa el interruptor.\n"
            "3. iPhone / iPad → Centro de control → Pantalla duplicada → elige este dispositivo.\n"
            "El modo juego (control de pantalla completa en SteamOS) aún no es compatible, pero está previsto para una versión futura; usa el modo escritorio por ahora."
        ),
    },

    # ---- 日志文案 ---- #
    "log_started": {"zh_CN": "AirPlay Deck v{ver} 已启动", "zh_TW": "AirPlay Deck v{ver} 已啟動",
                    "en": "AirPlay Deck v{ver} started", "ja": "AirPlay Deck v{ver} を起動しました",
                    "ko": "AirPlay Deck v{ver} 시작됨", "fr": "AirPlay Deck v{ver} démarré",
                    "de": "AirPlay Deck v{ver} gestartet", "es": "AirPlay Deck v{ver} iniciado"},
    "log_autostart": {"zh_CN": "已开启开机自启，正在自动启动…", "zh_TW": "已開啟自動啟動，正在自動啟動…",
                      "en": "Autostart enabled, starting…", "ja": "自動起動が有効，開始します…",
                      "ko": "자동 시작 켜짐, 자동 시작 중…", "fr": "Auto-démarrage activé, lancement…",
                      "de": "Autostart an, startet…", "es": "Autoinicio activado, iniciando…"},
    "log_saved": {"zh_CN": "设置已保存", "zh_TW": "設定已儲存", "en": "Settings saved",
                  "ja": "設定を保存しました",
                  "ko": "설정 저장됨", "fr": "Paramètres enregistrés", "de": "Einstellungen gespeichert", "es": "Ajustes guardados"},
    "log_reset_done": {"zh_CN": "已重置为默认设置", "zh_TW": "已重置為預設設定",
                       "en": "Restored defaults", "ja": "初期設定に戻しました",
                       "ko": "기본값으로 복원됨", "fr": "Réglages par défaut restaurés",
                       "de": "Standard wiederhergestellt", "es": "Valores predeterminados restaurados"},
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
