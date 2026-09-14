"""把当前 App / AppImage 写成 Steam「非 Steam 游戏」，并补齐图标与库封面。

- 显示名固定为 AirPlay Deck（覆盖 steamos-add-to-steam 用文件名命名的条目）
- 以 shortcuts.vdf 为唯一真相源（不再依赖 steamos 助手命名）
- 写入 userdata/*/config/grid/ 封面（grid / portrait / hero / logo）
"""

from __future__ import annotations

import os
import shutil
import struct
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


def _resource_roots() -> List[Path]:
    here = Path(__file__).resolve().parent.parent
    roots = [here]
    appdir = (os.environ.get("APPDIR") or "").strip()
    if appdir:
        roots.append(Path(appdir) / "opt" / "airplay-deck")
        roots.append(Path(appdir))
    return roots


def detect_icon_path() -> str:
    for root in _resource_roots():
        for name in ("icon.png", "icon.svg"):
            p = root / "resources" / name
            if p.is_file():
                return str(p)
    return ""


def detect_steam_art() -> dict:
    """返回 grid/portrait/hero/logo 绝对路径（缺的用 icon 兜底）。"""
    icon = detect_icon_path()
    out = {"grid": "", "portrait": "", "hero": "", "logo": ""}
    for root in _resource_roots():
        steam = root / "resources" / "steam"
        for key in list(out.keys()):
            if out[key]:
                continue
            p = steam / f"{key}.png"
            if p.is_file():
                out[key] = str(p)
    for key in out:
        if not out[key] and icon:
            out[key] = icon
    return out


def _steam_roots() -> List[Path]:
    home = Path.home()
    candidates = [
        home / ".steam" / "steam",
        home / ".steam" / "root",
        home / ".local" / "share" / "Steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
        Path("/home/deck/.steam/steam"),
        Path("/home/deck/.steam/root"),
        Path("/home/deck/.local/share/Steam"),
    ]
    # Flatpak / symlink friendly: also follow ~/.steam/steam.pid parent chains
    for extra in (home / ".steam" / "steam.pid",):
        try:
            if extra.exists():
                candidates.append(extra.resolve().parent)
        except Exception:
            pass
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


def _entry_name(entry: dict) -> str:
    if not isinstance(entry, dict):
        return ""
    for k in ("AppName", "appname", "Appname"):
        if k in entry and str(entry[k]).strip():
            return str(entry[k]).strip()
    return ""


def _entry_exe(entry: dict) -> str:
    if not isinstance(entry, dict):
        return ""
    for k in ("Exe", "exe", "AppExe"):
        if k in entry and str(entry[k]).strip():
            return str(entry[k]).strip()
    return ""


def _looks_like_ours(entry: dict, target: str, app_name: str) -> bool:
    if not isinstance(entry, dict):
        return False
    name = _entry_name(entry)
    if name == app_name:
        return True
    # steamos-add-to-steam 常用 AppImage 文件名当显示名
    if name.lower().startswith("airplaydeck") and name.lower().endswith(".appimage"):
        return True
    exe = _entry_exe(entry)
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
    """SteamGrid 风格 short app id：crc32('\"'+exe+'\"'+appname) | 0x80000000。"""
    exe = _quote_exe(exe_path)
    crc = zlib.crc32((exe + app_name).encode("utf-8")) & 0xFFFFFFFF
    return crc | 0x80000000


def _make_entry(app_name: str, exe_path: str, start_dir: str, icon: str = "") -> dict:
    # Steam 二进制 shortcuts.vdf 常见键名（大小写敏感）
    return {
        "appname": app_name,
        "AppName": app_name,
        "exe": _quote_exe(exe_path),
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


def _durable_icon_copy(icon_path: str) -> str:
    """AppImage 挂载路径会消失；把图标拷到用户可写目录再填入 Steam icon 字段。"""
    if not icon_path or not os.path.isfile(icon_path):
        return ""
    dest_dir = Path.home() / ".local" / "share" / "airplay-deck"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "icon.png"
        shutil.copyfile(icon_path, dest)
        return str(dest)
    except Exception:
        return icon_path


def _write_grid_art(config_dir: Path, exe_path: str, app_name: str, art: dict) -> None:
    grid = config_dir / "grid"
    try:
        grid.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    gid = shortcut_grid_id(exe_path, app_name)
    mapping = {
        f"{gid}.png": art.get("grid") or "",
        f"{gid}p.png": art.get("portrait") or "",
        f"{gid}_hero.png": art.get("hero") or "",
        f"{gid}_logo.png": art.get("logo") or "",
    }
    for name, src in mapping.items():
        if not src or not os.path.isfile(src):
            continue
        try:
            shutil.copyfile(src, grid / name)
        except Exception:
            pass


def _upsert_vdf(
    exe_path: str,
    start_dir: str,
    app_name: str = APP_NAME,
    icon_path: str = "",
    art: Optional[dict] = None,
) -> Tuple[bool, str]:
    roots = _steam_roots()
    if not roots:
        return False, "steam_not_found"
    art = art or detect_steam_art()
    icon_path = _durable_icon_copy(icon_path or art.get("logo") or art.get("grid") or "")
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
            # 合并所有「像我们」的条目成一条，删掉 steamos 用文件名建的重复项
            ours_keys = [k for k, e in list(shortcuts.items()) if _looks_like_ours(e, exe_path, app_name)]
            keep = ours_keys[0] if ours_keys else None
            for k in ours_keys[1:]:
                shortcuts.pop(k, None)
            entry = _make_entry(app_name, exe_path, start_dir, icon=icon_path)
            if keep is not None:
                old = shortcuts.get(keep) or {}
                if isinstance(old.get("tags"), dict):
                    entry["tags"] = old["tags"]
                shortcuts[keep] = entry
                updated += 1
            else:
                shortcuts[_next_index(shortcuts)] = entry
                added += 1
            sc.write_bytes(_write_map(data))
            _write_grid_art(sc.parent, exe_path, app_name, art)
            touched += 1
    if not touched:
        return False, "no_userdata"
    if updated and not added:
        return True, "updated"
    if added and not updated:
        return True, "added"
    return True, "updated" if updated else "added"


def add_to_steam(app_name: str = APP_NAME) -> Tuple[bool, str]:
    """只走 VDF upsert。不再先调 steamos-add-to-steam（它会用 AppImage 文件名命名）。"""
    app_name = (app_name or APP_NAME).strip() or APP_NAME
    try:
        exe_path, start_dir = detect_launch_target()
    except Exception as e:
        return False, f"no_target:{e}"
    art = detect_steam_art()
    icon_path = detect_icon_path() or art.get("logo") or ""
    return _upsert_vdf(exe_path, start_dir, app_name=app_name, icon_path=icon_path, art=art)
