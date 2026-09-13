"""Game Mode X11 auth overlays (uid match, reason ranking, cookie fallback).

Applied at launcher start so Desktop Mode keeps using the same code paths with
host uid (no behavior change) while Game Mode stops running probes/uxplay as root.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from . import x11

_REASON_NOISE = (
    "ld.so:", "ld_preload", "elfclass", "gameoverlayrenderer",
    "wrong elf class", "cannot be preloaded",
)
_REASON_PREFERRED = (
    "authorization required",
    "no authorization protocol",
    "cannot open display",
    "unable to open display",
    "not authorized",
    "could not initialise x",
    "could not initialize x",
    "no element",
    "failed to initialize gstreamer",
)
_REASON_FALLBACK = (
    "denied", "could not", "failed",
)

_applied = False

# Successful probe_container_display cache: ~5 minutes, keyed by
# (container, host DISPLAY, is_gamemode-ish env).
_PROBE_CACHE_TTL = 300.0
_probe_cache: Dict[Tuple[str, str, bool], Tuple[float, str, Tuple[str, Optional[str]]]] = {}


def _is_gamemode_ish_env() -> bool:
    """Cheap env fingerprint for cache key (gamescope / Steam Game Mode)."""
    markers = (
        os.environ.get("GAMESCOPE_WAYLAND_DISPLAY"),
        os.environ.get("XDG_CURRENT_DESKTOP"),
        os.environ.get("DESKTOP_SESSION"),
    )
    blob = " ".join(m or "" for m in markers).lower()
    if "gamescope" in blob:
        return True
    try:
        from . import session
        return bool(session.is_gamemode())
    except Exception:
        return False


def _probe_cache_key(container: str, display: str) -> Tuple[str, str, bool]:
    host_disp = (os.environ.get("DISPLAY") or display or "").strip()
    return (container.strip(), host_disp, _is_gamemode_ish_env())


def invalidate_probe_cache(reason: str = "") -> None:
    """Drop cached successful probes (e.g. after uxplay X init failure)."""
    global _probe_cache
    if _probe_cache:
        n = len(_probe_cache)
        _probe_cache = {}
        try:
            import sys
            msg = f"[gamemode_x11] invalidated probe cache ({n} entries)"
            if reason:
                msg += f": {reason}"
            print(msg, file=sys.stderr)
        except Exception:
            pass


def _is_reason_noise(line: str) -> bool:
    low = line.lower()
    return any(n in low for n in _REASON_NOISE)


def _short_reason(r) -> str:
    """Prefer Authorization / Cannot open display; ignore ld.so LD_PRELOAD noise."""
    txt = ((getattr(r, "stderr", b"") or b"") + b"\n"
           + (getattr(r, "stdout", b"") or b"")).decode("utf-8", "replace")
    lines = [l.strip() for l in txt.splitlines()
             if l.strip() and not _is_reason_noise(l)]
    if not lines:
        return ""
    for hint in _REASON_PREFERRED:
        for l in lines:
            if hint in l.lower():
                return l[:160]
    for hint in _REASON_FALLBACK:
        for l in lines:
            if hint in l.lower():
                return l[:160]
    return lines[0][:160]


def _host_user_args() -> List[str]:
    """Match gamescope/Xwayland owner (Deck uid) inside the container."""
    try:
        return ["--user", f"{os.getuid()}:{os.getgid()}"]
    except Exception:
        return []


def _best_effort_xauth_fallback(
    display: str,
    auth_map: Dict[str, str],
    hosts: List[str],
    log,
) -> Tuple[str, Optional[str]]:
    """Prefer a cookie path over unset when container probes all fail.

    Prefer cookies whose parsed display list **covers** the chosen display
    number over ones that do not (不含当前显示号). Never recommend unset when
    a real cookie exists.
    """
    host_ok = False
    try:
        for xa in (None, "/dev/null", *hosts[:5]):
            r = x11.host_can_open_display(xa)
            if r is True:
                host_ok = True
                break
    except Exception:
        pass

    want = x11._display_number_of(display)

    covering: List[str] = []
    other: List[str] = []

    def _add(path: str) -> None:
        if not path or path in covering or path in other:
            return
        ok, disps, _mt = x11.xauth_file_info(path)
        if not ok:
            # Not a real cookie structure — skip for best-effort (avoid junk).
            return
        covers = (want is None) or (not disps) or ("" in disps) or (want in disps)
        if covers:
            covering.append(path)
        else:
            other.append(path)

    if display in auth_map:
        _add(auth_map[display])
    for _d, p in sorted(auth_map.items()):
        _add(p)
    for h in hosts:
        _add(h)

    candidates = covering + other
    if candidates:
        path = candidates[0]
        tag = "（覆盖当前显示号）" if path in covering else "（不含当前显示号，次优）"
        log("warn",
            f"容器内探针全失败；宿主侧显示可连={host_ok}。"
            f"回退 best-effort cookie{tag}（避免 unset XAUTHORITY）：{path}")
        return "path", path

    log("warn",
        f"容器内探针全失败且无可用 cookie；宿主侧显示可连={host_ok}。"
        "只能回退 unset XAUTHORITY（游戏模式下很可能 Authorization required）")
    return "unset", None


def apply() -> None:
    """Monkey-patch x11 helpers for Game Mode X11 auth. Idempotent."""
    global _applied
    if _applied:
        return
    _applied = True

    x11._short_reason = _short_reason  # type: ignore[attr-defined]

    def _exec(container: str, env_args: List[str], cmd: str, timeout: float = 15.0):
        return subprocess.run(
            ["podman", "exec", *_host_user_args(), *x11.SANITIZE_ENV, *env_args,
             container, "sh", "-c", cmd],
            capture_output=True, timeout=timeout,
        )

    x11._exec = _exec  # type: ignore[assignment]

    def _copy_in(container: str, src: str, dst: str, chmod: str) -> bool:
        try:
            cp = subprocess.run(
                ["podman", "cp", src, f"{container}:{dst}"],
                capture_output=True, timeout=25.0,
            )
        except Exception:
            return False
        if cp.returncode != 0:
            return False
        try:
            # Keep root for chown so host-uid probes can read mode 600 cookies.
            subprocess.run(
                ["podman", "exec", *x11.SANITIZE_ENV, container, "sh", "-c",
                 f"chown {os.getuid()}:{os.getgid()} '{dst}' 2>/dev/null; "
                 f"chmod {chmod} '{dst}' 2>/dev/null; true"],
                capture_output=True, timeout=10.0,
            )
        except Exception:
            pass
        return True

    x11._copy_in = _copy_in  # type: ignore[assignment]

    _orig_build = x11.build_podman_cmd

    def build_podman_cmd(
        container: str,
        display: str,
        xauth: Tuple[str, Optional[str]],
        binpath: str,
        args: List[str],
        home: str,
        dbus_address: str = "",
        extra_env=None,
    ) -> List[str]:
        cmd = _orig_build(
            container, display, xauth, binpath, args, home,
            dbus_address=dbus_address, extra_env=extra_env,
        )
        # Insert --user after "exec" if missing. Always keep --user uid:gid.
        if len(cmd) >= 2 and cmd[0] == "podman" and cmd[1] == "exec":
            if "--user" not in cmd:
                cmd = cmd[:2] + _host_user_args() + cmd[2:]
        return cmd

    x11.build_podman_cmd = build_podman_cmd  # type: ignore[assignment]

    _orig_probe = x11.probe_container_display

    def probe_container_display(container: str, display: str, log=None):
        log = log or (lambda *a, **k: None)
        key = _probe_cache_key(container, display)
        now = time.time()
        hit = _probe_cache.get(key)
        if hit is not None:
            ts, cached_disp, cached_pair = hit
            if now - ts <= _PROBE_CACHE_TTL:
                mode, path = cached_pair
                log("info",
                    f"X11 探针缓存命中（{int(now - ts)}s 前，"
                    f"key=container={key[0]} DISPLAY={key[1]} gamemode-ish={key[2]}）"
                    f"→ DISPLAY={cached_disp} mode={mode}"
                    + (f" path={path}" if path else ""))
                return cached_disp, cached_pair
            else:
                _probe_cache.pop(key, None)

        disp, pair = _orig_probe(container, display, log)
        mode, path = pair
        if mode == "unset" and path is None:
            auth_map = x11.find_x_server_auth_map()
            hosts = x11.find_host_xauth_candidates()
            pair2 = _best_effort_xauth_fallback(disp, auth_map, hosts, log)
            if pair2[0] == "path" and pair2[1]:
                _probe_cache[key] = (time.time(), disp, pair2)
            return disp, pair2

        _probe_cache[key] = (time.time(), disp, pair)
        return disp, pair

    x11.probe_container_display = probe_container_display  # type: ignore[assignment]

    def _container_home(container: str) -> str:
        host_home = (os.environ.get("HOME") or "").strip() or "/root"
        try:
            r = x11._exec(container, [], "printenv HOME || true", timeout=6.0)
            v = (r.stdout or b"").decode(errors="replace").strip().splitlines()
            home = v[-1].strip() if v else ""
            return home or host_home
        except Exception:
            return host_home

    x11._container_home = _container_home  # type: ignore[assignment]