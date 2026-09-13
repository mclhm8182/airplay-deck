"""把当前 App / AppImage 写成 Steam「非 Steam 游戏」快捷方式。

优先调用 SteamOS 自带的 `steamos-add-to-steam`；否则写入各用户
`userdata/*/config/shortcuts.vdf`（二进制 VDF）。已存在相同目标路径时不重复添加。
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


APP_NAME = "AirPlay Deck"


def detect_launch_target() -> Tuple[str, str]:
    """返回 (可执行路径, 启动目录)。优先 APPIMAGE 环境变量。"""
    appimage = (os.environ.get("APPIMAGE") or "").strip()
    if appimage and os.path.isfile(appimage):
        p = str(Path(appimage).resolve())
        return p, str(Path(p).parent)
    # AppImage 内部运行时 argv0 可能是 mount 点；仍尽量用绝对路径
    argv0 = os.path.abspath(sys.argv[0] if sys.argv else "")
    if argv0 and os.path.isfile(argv0):
        return argv0, str(Path(argv0).parent)
    exe = os.path.abspath(sys.executable or "")
    if exe and os.path.isfile(exe):
        return exe, str(Path(exe).parent)
    raise FileNotFoundError("cannot detect AppImage / executable path")


def _steam_roots() -> List[Path]:
    home = Path.home()
    candidates = [
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
        Path("/home/deck/.steam/steam"),
        Path("/home/deck/.local/share/Steam"),
    ]
    # follow .steam/steam symlink carefully
    out: List[Path] = []
    seen = set()
    for c in candidates:
        try:
            p = c.resolve() if c.exists() else c
        except Exception:
            p = c
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if (p / "userdata").is_dir() or c.exists():
            out.append(p if (p / "userdata").is_dir() else c)
    # de-dup preferring ones with userdata
    with_ud = [p for p in out if (p / "userdata").is_dir()]
    return with_ud or out


def _iter_shortcut_files(steam_root: Path) -> Iterable[Path]:
    ud = steam_root / "userdata"
    if not ud.is_dir():
        return
    for user_dir in sorted(ud.iterdir()):
        if not user_dir.is_dir() or not user_dir.name.isdigit():
            continue
        yield user_dir / "config" / "shortcuts.vdf"


def _quote_exe(path: str) -> str:
    # Steam shortcuts store Exe with quotes
    if path.startswith('"') and path.endswith('"'):
        return path
    return f'"{path}"'


def _norm_exe_compare(s: str) -> str:
    s = (s or "").strip().strip('"')
    try:
        return str(Path(s).resolve()).lower()
    except Exception:
        return s.lower()


# ---- minimal binary VDF (Steam shortcuts) ----

_TYPE_MAP = 0x00
_TYPE_STR = 0x01
_TYPE_INT = 0x02
_TYPE_END = 0x08


def _read_cstring(data: bytes, i: int) -> Tuple[str, int]:
    j = data.index(b"\x00", i)
    return data[i:j].decode("utf-8", errors="replace"), j + 1


def _parse_map(data: bytes, i: int = 0) -> Tuple[dict, int]:
    out = {}
    while i < len(data):
        t = data[i]
        i += 1
        if t == _TYPE_END:
            return out, i
        key, i = _read_cstring(data, i)
        if t == _TYPE_MAP:
            val, i = _parse_map(data, i)
            out[key] = val
        elif t == _TYPE_STR:
            val, i = _read_cstring(data, i)
            out[key] = val
        elif t == _TYPE_INT:
            out[key] = struct.unpack_from("<i", data, i)[0]
            i += 4
        else:
            # unknown — abort parse of this map
            raise ValueError(f"unsupported VDF type {t:#x} at {i}")
    return out, i


def _write_cstring(s: str) -> bytes:
    return s.encode("utf-8", errors="replace") + b"\x00"


def _write_map(d: dict) -> bytes:
    buf = bytearray()
    for k, v in d.items():
        if isinstance(v, dict):
            buf.append(_TYPE_MAP)
            buf += _write_cstring(str(k))
            buf += _write_map(v)
        elif isinstance(v, str):
            buf.append(_TYPE_STR)
            buf += _write_cstring(str(k))
            buf += _write_cstring(v)
        elif isinstance(v, int):
            buf.append(_TYPE_INT)
            buf += _write_cstring(str(k))
            buf += struct.pack("<i", int(v))
        else:
            raise TypeError(type(v))
    buf.append(_TYPE_END)
    return bytes(buf)


def _load_shortcuts(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return {"shortcuts": {}}
    data = path.read_bytes()
    root, _ = _parse_map(data, 0)
    if "shortcuts" not in root or not isinstance(root["shortcuts"], dict):
        root["shortcuts"] = {}
    return root


def _next_index(shortcuts: dict) -> str:
    nums = []
    for k in shortcuts.keys():
        try:
            nums.append(int(k))
        except ValueError:
            pass
    return str(max(nums) + 1 if nums else 0)


def _entry_matches(entry: dict, target: str) -> bool:
    exe = entry.get("Exe") or entry.get("exe") or ""
    return _norm_exe_compare(str(exe)) == _norm_exe_compare(target)


def _make_entry(app_name: str, exe_path: str, start_dir: str, icon: str = "") -> dict:
    return {
        "appname": app_name,
        "Exe": _quote_exe(exe_path),
        "StartDir": _quote_exe(start_dir),
        "icon": icon or "",
        "ShortcutPath": "",
        "LaunchOptions": "",
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "LastPlayTime": 0,
        "tags": {},
    }


def _add_via_vdf(exe_path: str, start_dir: str, app_name: str = APP_NAME) -> Tuple[bool, str]:
    roots = _steam_roots()
    if not roots:
        return False, "steam_not_found"
    wrote = 0
    already = 0
    for root in roots:
        for sc in _iter_shortcut_files(root):
            sc.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = _load_shortcuts(sc)
            except Exception:
                # corrupt / unexpected — start fresh map but keep backup
                try:
                    sc.rename(sc.with_suffix(".vdf.bak-airplaydeck"))
                except Exception:
                    pass
                data = {"shortcuts": {}}
            shortcuts = data.setdefault("shortcuts", {})
            if any(_entry_matches(e, exe_path) for e in shortcuts.values() if isinstance(e, dict)):
                already += 1
                continue
            idx = _next_index(shortcuts)
            shortcuts[idx] = _make_entry(app_name, exe_path, start_dir)
            sc.write_bytes(_write_map(data))
            wrote += 1
    if wrote:
        return True, "added"
    if already:
        return True, "already"
    return False, "no_userdata"


def _add_via_steamos(exe_path: str) -> Optional[bool]:
    """Return True/False if helper ran; None if helper missing."""
    from shutil import which
    helper = which("steamos-add-to-steam")
    if not helper:
        return None
    try:
        r = subprocess.run(
            [helper, exe_path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def add_to_steam(app_name: str = APP_NAME) -> Tuple[bool, str]:
    """添加当前程序到 Steam 库。

    返回 (ok, code)：
      added / already / steamos_ok / steam_not_found / no_userdata / no_target / error:...
    """
    try:
        exe_path, start_dir = detect_launch_target()
    except Exception as e:
        return False, f"no_target:{e}"

    via = _add_via_steamos(exe_path)
    if via is True:
        return True, "steamos_ok"
    # steamos helper missing or failed — still try VDF (covers Desktop Mode / non-SteamOS)
    ok, code = _add_via_vdf(exe_path, start_dir, app_name=app_name)
    if ok:
        return True, code
    if via is False:
        return False, code if code != "steam_not_found" else "steamos_failed"
    return False, code
