# VLC Stream Keeper
Keeps a camera stream playing full screen in VLC.

## Description
Keeps an RTSP camera stream on screen on a small display driven by a
Raspberry Pi 4 (or similar) with no keyboard or mouse attached. Without it,
the stream stopped about every 1–2 days, and I had to remote into the Pi's
desktop to restart it.

Every few seconds the keeper asks VLC for its state over D-Bus (MPRIS) and
recovers on its own when:

- **VLC isn't running** (crashed, closed, or exited at the end of the stream):
  it launches VLC.
- **VLC is stopped or paused:** it sends Play, and relaunches VLC if that
  doesn't work.
- **The picture is frozen:** VLC says "Playing" but its playback clock hasn't
  moved for `STALL_TIMEOUT` seconds, so it relaunches VLC.
- **The window flickers between full screen and windowed:** more than one VLC
  is running, or the playback state keeps flipping, so it kills every VLC and
  starts exactly one.

Before each launch, any running VLC is stopped and force-killed if it doesn't
exit within 5 seconds, so two full-screen windows never fight each other. If
the camera stays down, the wait between relaunches doubles each time
(20 s, 40 s, 80 s … up to `MAX_BACKOFF`). It resets after two minutes of
healthy playback.

By default the stream plays in `cvlc`, VLC without its window and menus,
which avoids most full-screen glitches.

Note: the keeper stops **every** VLC run by the same user before launching its
own. Run it on a machine (or user account) dedicated to the display.

## Requirements
- Raspberry Pi OS / Debian with a desktop session
- Python 3.7+
- `vlc`, `python3-dbus`, `python3-dotenv`

## Setup
```bash
sudo apt install -y vlc python3-dbus python3-dotenv
git clone https://github.com/Jessie-newtworks/VLC_Stream_Keeper.git ~/VLC_Stream_Keeper
cd ~/VLC_Stream_Keeper
cp .env.example .env
nano .env        # set STREAM_URL; the other settings are optional
```

Try it by hand first, from a terminal on the Pi's desktop:
```bash
python3 vlcstreamkeeper.py
```
Press Ctrl+C to stop. VLC keeps playing.

If something else already starts VLC on login (an autostart entry or a
desktop shortcut), remove it and let the keeper launch VLC instead.

## Run automatically with systemd
```bash
mkdir -p ~/.config/systemd/user
cp vlcstreamkeeper.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vlcstreamkeeper
sudo loginctl enable-linger "$USER"
```
systemd restarts the keeper if it ever exits. While the desktop is still
starting up after a boot, VLC launches may fail; the keeper keeps retrying
until the desktop is ready.

Useful commands:
```bash
journalctl --user -u vlcstreamkeeper -f     # live log
systemctl --user restart vlcstreamkeeper
systemctl --user stop vlcstreamkeeper       # stops the keeper; VLC keeps playing
```

## Configuration
All settings live in `.env`. See `.env.example` for every option and its
default. The main ones:

| Setting | Default | Meaning |
|---|---|---|
| `STREAM_URL` | *(required)* | RTSP address of the camera |
| `POLL_TIME` | 2 | Seconds between checks |
| `COOLDOWN_TIME` | 10 | Seconds before the first relaunch retry; doubles on repeat failures |
| `MAX_BACKOFF` | 300 | Longest wait between relaunches, in seconds |
| `STALL_TIMEOUT` | 30 | Seconds without the playback clock moving before the picture counts as frozen |
| `VLC_BINARY` | cvlc | `cvlc` (no window or menus) or `vlc` |
| `VLC_VOUT` | xcb_x11 | Video output; leave empty to let VLC pick |

## Troubleshooting
- **Flicker or a black screen on a Wayland desktop:** try `VLC_VOUT=` (empty).
  Check which session you have with `echo $XDG_SESSION_TYPE` in a desktop
  terminal.
- **Log says "not answering on D-Bus":** VLC's MPRIS control isn't
  reachable. Make sure the keeper runs as the desktop user, not root or cron.
- **To see what VLC itself is doing,** set `LOG_LEVEL=DEBUG`, or run the
  command from `vlc_command()` by hand with `--verbose=2`.

## Version Notes
0.1.0: Relaunches VLC when it exits, enforces a single VLC instance, uses
cvlc, detects frozen and flickering playback, backs off between restarts,
validates settings, logs with timestamps, and adds a systemd service.
Still in testing.
