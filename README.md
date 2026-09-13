# AirPlay Deck

Turn a Steam Deck / Linux box into an **AirPlay receiver** (powered by [UxPlay](https://github.com/FDH2/UxPlay)). **Desktop Mode and SteamOS Game Mode are both officially supported.**

## Highlights

- Cast in **Desktop Mode** and **Game Mode** (add the AppImage as a non-Steam game for Game Mode)
- Desktop Window/Auto modes **auto-maximize** portrait streams (e.g. iPhone vertical video); Game Mode always uses fullscreen (`-fs`)
- Keep-awake while mirroring: systemd-inhibit, plus host `xset` keepalive in Game Mode
- UI in 8 languages (follows system)
- One-click environment checks, container UxPlay install/update, diagnostic export

## How to use

### Desktop Mode

1. Download the AppImage, make it executable, and run it from the desktop.
2. Open **Diagnostics** and confirm the container is ready (first launch needs network).
3. On **Home**, flip the switch; on iPhone/iPad use Control Center → Screen Mirroring → this device.
4. Display mode: Window / Fullscreen / Auto. Portrait streams auto-maximize in Window mode.

### Game Mode (SteamOS)

1. Add the AppImage as a **non-Steam game** and launch it from Game Mode.
2. Flip the receive switch on Home, then mirror from your phone.
3. Casting uses fullscreen by default. For long videos, keep **Keep screen awake while mirroring** enabled.
4. If you hit rare auth or blank-screen issues, export logs; retry or use Desktop Mode if needed.

## Troubleshooting (short)

| Symptom | Tip |
|---------|-----|
| Phone cannot find the device | Same LAN; allow mDNS/Bonjour; restart Avahi if needed |
| Game Mode: audio but no video / Authorization required | Export logs; this build runs probes/uxplay as the host uid. Fall back to Desktop Mode if still failing |
| Game Mode: screen blacks after a few minutes, audio continues | Enable keep-awake; host needs `xset` for best results |
| Desktop: tiny portrait window | Window/Auto should auto-maximize; install `wmctrl` or `xdotool`, or use Fullscreen |

## Build

Build on **x86_64 Linux** (AppImage toolchain). See Actions / local build notes in the repo.

## License

See LICENSE; UxPlay follows its upstream license.
