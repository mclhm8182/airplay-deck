"""把当前 App / AppImage 写成 Steam「非 Steam 游戏」，并尽量补齐图标与库封面。

- 显示名固定为 AirPlay Deck
- 若库中已有同名或同路径 / 旧版 AppImage 条目：就地更新 Exe / StartDir / icon
- 写入 userdata/*/config/grid/ 封面（grid / portrait / hero / logo）
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

APP_NAME = "AirPlay Deck"


def detect_launch_target() -> Tuple[str, str]:
    appimage = (os.environ.get("APPIMAGE") or "").strip()
    if appimage and os.path.isfile(appimage):
        p = str(Path(appimage).resolve())
        return p, str(Path(p).parent)
    argv0 = os.path.abspath(sys.argv[0] if sys.argv else "")
    if argv0 and os.path.isfile(argv0):
        return argv0, str(Path(argv0).parent)
    exe = os.path.abspath(sys.executable or "")
    if exe and os.path.isfile(exe):
        return exe, str(Path(exe).parent)
    raise FileNotFoundError("cannot detect AppImage / executable path")


def detect_icon_path() -> str:
    here = Path(__file__).resolve().parent.parent
    for p in (
        here / "resources" / "icon.png",
        here / "resources" / "icon.svg",
        Path(os.environ.get("APPDIR") or "") / "resources" / "icon.png",
    ):
        if p.is_file():
            return str(p)
    return ""


def _steam_roots() -> List[Path]:
    home = Path.home()
    candidates = [
        home / ".steam" / "steam",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
        Path("/home/deck/.steam/steam"),
        Path("/home/deck/.local/share/Steam"),
    ]
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
        if (p / "userdata").is_dir():
            out.append(p)
    return out


def _iter_shortcut_files(steam_root: Path) -> Iterable[Path]:
    ud = steam_root / "userdata"
    if not ud.is_dir():
        return
    for user_dir in sorted(ud.iterdir()):
        if not user_dir.is_dir() or not user_dir.name.isdigit():
            continue
        yield user_dir / "config" / "shortcuts.vdf"


def _quote_exe(path: str) -> str:
    if path.startswith('"') and path.endswith('"'):
        return path
    return f'"{path}"'


def _norm_exe_compare(s: str) -> str:
    s = (s or "").strip().strip('"')
    try:
        return str(Path(s).resolve()).lower()
    except Exception:
        return s.lower()


def _looks_like_ours(entry: dict, target: str, app_name: str) -> bool:
    if not isinstance(entry, dict):
        return False
    name = str(entry.get("appname") or entry.get("AppName") or "").strip()
    if name == app_name:
        return True
    exe = str(entry.get("Exe") or entry.get("exe") or "")
    if _norm_exe_compare(exe) == _norm_exe_compare(target):
        return True
    low = _norm_exe_compare(exe).replace("-", "").replace("_", "")
    if "airplaydeck" in low:
        return True
    return False


_TYPE_MAP, _TYPE_STR, _TYPE_INT, _TYPE_END = 0x00, 0x01, 0x02, 0x08


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
            raise ValueError(f"unsupported VDF type {t:#x}")
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
    root, _ = _parse_map(path.read_bytes(), 0)
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


def shortcut_grid_id(exe_path: str, app_name: str) -> int:
    exe = _quote_exe(exe_path)
    crc = zlib.crc32((exe + app_name).encode("utf-8")) & 0xFFFFFFFF
    return crc | 0x80000000


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
        "FlatpakAppID": "",
        "tags": {},
    }


def _write_grid_art(config_dir: Path, exe_path: str, app_name: str, icon_path: str) -> None:
    if not icon_path or not os.path.isfile(icon_path):
        return
    grid = config_dir / "grid"
    grid.mkdir(parents=True, exist_ok=True)
    gid = shortcut_grid_id(exe_path, app_name)
    for name in (f"{gid}.png", f"{gid}p.png", f"{gid}_hero.png", f"{gid}_logo.png"):
        try:
            shutil.copyfile(icon_path, grid / name)
        except Exception:
            pass


def _upsert_vdf(
    exe_path: str,
    start_dir: str,
    app_name: str = APP_NAME,
    icon_path: str = "",
) -> Tuple[bool, str]:
    roots = _steam_roots()
    if not roots:
        return False, "steam_not_found"
    touched = updated = added = 0
    for root in roots:
        for sc in _iter_shortcut_files(root):
            sc.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = _load_shortcuts(sc)
            except Exception:
                try:
                    sc.rename(sc.with_suffix(".vdf.bak-airplaydeck"))
                except Exception:
                    pass
                data = {"shortcuts": {}}
            shortcuts = data.setdefault("shortcuts", {})
            found_key = None
            for k, e in list(shortcuts.items()):
                if _looks_like_ours(e, exe_path, app_name):
                    found_key = k
                    break
            entry = _make_entry(app_name, exe_path, start_dir, icon=icon_path)
            if found_key is not None:
                old = shortcuts.get(found_key) or {}
                if isinstance(old.get("tags"), dict):
                    entry["tags"] = old["tags"]
                shortcuts[found_key] = entry
                updated += 1
            else:
                shortcuts[_next_index(shortcuts)] = entry
                added += 1
            sc.write_bytes(_write_map(data))
            _write_grid_art(sc.parent, exe_path, app_name, icon_path)
            touched += 1
    if not touched:
        return False, "no_userdata"
    if updated and not added:
        return True, "updated"
    if added and not updated:
        return True, "added"
    return True, "updated" if updated else "added"


def _add_via_steamos(exe_path: str) -> Optional[bool]:
    from shutil import which
    helper = which("steamos-add-to-steam")
    if not helper:
        return None
    try:
        r = subprocess.run([helper, exe_path], capture_output=True, text=True, timeout=60, check=False)
        return r.returncode == 0
    except Exception:
        return False


def add_to_steam(app_name: str = APP_NAME) -> Tuple[bool, str]:
    app_name = (app_name or APP_NAME).strip() or APP_NAME
    try:
        exe_path, start_dir = detect_launch_target()
    except Exception as e:
        return False, f"no_target:{e}"
    icon_path = detect_icon_path()
    via = _add_via_steamos(exe_path)
    ok, code = _upsert_vdf(exe_path, start_dir, app_name=app_name, icon_path=icon_path)
    if ok:
        return True, code
    if via is True:
        return True, "steamos_ok"
    if via is False:
        return False, code if code != "steam_not_found" else "steamos_failed"
    return False, code
