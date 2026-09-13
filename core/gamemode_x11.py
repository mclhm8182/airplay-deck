"""Game Mode X11 auth overlays (uid match, reason ranking, cookie fallback).

Applied at launcher start so Desktop Mode keeps using the same code paths with
host uid (no behavior change) while Game Mode stops running probes/uxplay as root.
"""

from __future__ import annotations

import os
import subprocess
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
    """Prefer a cookie path over unset when container probes all fail."""
    host_ok = False
    try:
        for xa in (None, "/dev/null", *hosts[:5]):
            r = x11.host_can_open_display(xa)
            if r is True:
                host_ok = True
                break
    except Exception:
        pass

    candidates: List[str] = []
    if display in auth_map:
        candidates.append(auth_map[display])
    for _d, p in sorted(auth_map.items()):
        if p not in candidates:
            candidates.append(p)
    for h in hosts:
        if h in candidates:
            continue
        ok, _disps, _mt = x11.xauth_file_info(h)
        if ok:
            candidates.append(h)

    if candidates:
        path = candidates[0]
        log("warn",
            f"容器内探针全失败；宿主侧显示可连={host_ok}。"
            f"回退 best-effort cookie（避免 unset XAUTHORITY）：{path}")
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
        # Insert --user after "exec" if missing.
        if len(cmd) >= 2 and cmd[0] == "podman" and cmd[1] == "exec":
            if "--user" not in cmd:
                cmd = cmd[:2] + _host_user_args() + cmd[2:]
        return cmd

    x11.build_podman_cmd = build_podman_cmd  # type: ignore[assignment]

    _orig_probe = x11.probe_container_display

    def probe_container_display(container: str, display: str, log=None):
        log = log or (lambda *a, **k: None)
        disp, pair = _orig_probe(container, display, log)
        mode, path = pair
        if mode == "unset" and path is None:
            auth_map = x11.find_x_server_auth_map()
            hosts = x11.find_host_xauth_candidates()
            return disp, _best_effort_xauth_fallback(disp, auth_map, hosts, log)
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
