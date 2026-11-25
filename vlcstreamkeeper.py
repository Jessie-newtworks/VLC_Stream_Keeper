#!/usr/bin/env python3
"""
vlcstreamkeeper.py

Keeps an RTSP camera stream playing full screen in VLC on a display with no
keyboard or mouse, such as a small screen driven by a Raspberry Pi 4.

Every POLL_TIME seconds the keeper asks VLC for its state over D-Bus (MPRIS)
and recovers from these failures:

* VLC is not running (crashed, closed, or exited at the end of the stream):
  launch it.
* VLC is stopped or paused: send Play, and relaunch VLC if that does not work.
* VLC reports "Playing" but its playback clock has stopped moving (a frozen
  picture): relaunch VLC.
* More than one VLC is running, or the playback state keeps flipping between
  polls (the full-screen flicker): kill every VLC and launch exactly one.

Before launching, every VLC owned by this user is stopped, and force-killed
if it does not exit within a few seconds, so two full-screen windows can
never fight each other. Repeated relaunches back off exponentially (up to
MAX_BACKOFF seconds) so a camera that is down does not cause a restart storm.

Configuration is read from a .env file; see .env.example.

Dependencies:
    python3-dbus
    python3-dotenv
    vlc (provides cvlc)
"""

import logging
import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass

import dbus  # Linux only; PyCharm on Windows may flag this import, which is fine
from dotenv import load_dotenv

__author__ = "Jessie-Newtworks"
__copyright__ = "Jessie-Newtworks"
__credits__ = ["Jessie-Newtworks"]
__license__ = "MIT"
__version__ = "0.1.0"
__maintainer__ = "Jessie-Newtworks"
__email__ = "via github"
__status__ = "Alpha"

log = logging.getLogger("vlcstreamkeeper")

MPRIS_PREFIX = "org.mpris.MediaPlayer2.vlc"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

FLAP_WINDOW = 30.0  # seconds to look back when counting state changes
FLAP_CHANGES = 5  # this many state changes inside FLAP_WINDOW counts as flapping
STABLE_RESET = 120.0  # seconds of healthy playback before the backoff resets
PLAY_RECHECK = 5.0  # seconds to wait after sending Play before checking again
KILL_TIMEOUT = 5.0  # seconds to wait for VLC to exit before force-killing it
MAX_BACKOFF_STEPS = 16  # caps the exponent so the delay maths cannot overflow


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    stream_url: str
    poll_time: int
    cooldown_time: int
    network_cache: int
    stall_timeout: int
    startup_grace: int
    max_backoff: int
    vlc_binary: str
    vlc_vout: str


def env_int(name, default, minimum=1):
    """Read a whole-number setting, falling back to default when unset."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {raw!r}")
    if value < minimum:
        raise SystemExit(f"{name} must be at least {minimum}, got {value}")
    return value


def load_config():
    """Load settings from .env (searched from this script's folder upward)."""
    load_dotenv()
    stream_url = os.getenv("STREAM_URL", "").strip()
    if not stream_url:
        raise SystemExit("STREAM_URL is not set. Copy .env.example to .env "
                         "and fill in your camera's stream address.")
    return Config(
        stream_url=stream_url,
        poll_time=env_int("POLL_TIME", 2),
        cooldown_time=env_int("COOLDOWN_TIME", 10),
        network_cache=env_int("NETWORK_CACHE", 3000, minimum=0),
        stall_timeout=env_int("STALL_TIMEOUT", 30),
        startup_grace=env_int("STARTUP_GRACE", 20),
        max_backoff=env_int("MAX_BACKOFF", 300),
        vlc_binary=os.getenv("VLC_BINARY", "cvlc").strip() or "cvlc",
        vlc_vout=os.getenv("VLC_VOUT", "xcb_x11").strip(),
    )


def vlc_command(cfg):
    """Build the VLC command line. The stream URL is never logged."""
    cmd = [
        cfg.vlc_binary,
        "--fullscreen",
        "--no-video-title-show",
        "--rtsp-tcp",
        f"--network-caching={cfg.network_cache}",
    ]
    if cfg.vlc_vout:
        cmd.append(f"--vout={cfg.vlc_vout}")
    cmd.append(cfg.stream_url)
    return cmd


# --------------------------------------------------------------------------
# VLC processes
# --------------------------------------------------------------------------

def vlc_pids():
    """PIDs of this user's VLC processes (cvlc runs as a process named vlc)."""
    result = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "vlc"],
                            capture_output=True, text=True, check=False)
    return [int(pid) for pid in result.stdout.split()]


def signal_vlc(sig):
    subprocess.run(["pkill", f"-{sig}", "-u", str(os.getuid()), "-x", "vlc"],
                   check=False)


# --------------------------------------------------------------------------
# D-Bus / MPRIS
# --------------------------------------------------------------------------

def mpris_names(bus):
    """MPRIS bus names owned by VLC (a second VLC registers a .instanceN name)."""
    return sorted(str(name) for name in bus.list_names()
                  if str(name) == MPRIS_PREFIX
                  or str(name).startswith(MPRIS_PREFIX + "."))


def read_player(bus, name):
    """Return (PlaybackStatus, Position in microseconds or None)."""
    proxy = bus.get_object(name, MPRIS_PATH)
    props = dbus.Interface(proxy, dbus.PROPERTIES_IFACE)
    status = str(props.Get(PLAYER_IFACE, "PlaybackStatus"))
    try:
        position = int(props.Get(PLAYER_IFACE, "Position"))
    except dbus.exceptions.DBusException:
        position = None
    return status, position


def send_play(bus, name):
    proxy = bus.get_object(name, MPRIS_PATH)
    dbus.Interface(proxy, PLAYER_IFACE).Play()


# --------------------------------------------------------------------------
# Keeper
# --------------------------------------------------------------------------

class Keeper:
    """Watches VLC and relaunches it when playback fails."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.bus = None
        self.proc = None
        self.launched_at = float("-inf")  # monotonic time of our last launch
        self.next_action_at = 0.0  # no relaunch before this (backoff)
        self.failures = 0
        self.waiting_logged = False
        self._reset_tracking()

    # -- state tracking ----------------------------------------------------

    def _reset_tracking(self):
        self.prev_status = None
        self.status_changes = deque()
        self.last_position = None
        self.last_progress_at = None
        self.position_moves = False
        self.no_position_logged = False
        self.healthy_since = None

    def _track_status(self, status, now):
        if status != self.prev_status:
            log.info("VLC status: %s -> %s", self.prev_status, status)
            self.status_changes.append(now)
            if status == "Playing":
                self.last_progress_at = now
            else:
                self.healthy_since = None
            self.prev_status = status
        while self.status_changes and now - self.status_changes[0] > FLAP_WINDOW:
            self.status_changes.popleft()

    def _is_flapping(self):
        return len(self.status_changes) >= FLAP_CHANGES

    def _track_position(self, position, now):
        if position is None:
            return
        if self.last_position is None:
            self.last_position = position
            self.last_progress_at = now
        elif position != self.last_position:
            self.position_moves = True
            self.last_position = position
            self.last_progress_at = now

    def _is_stalled(self, now):
        """True when the playback clock stopped after having been moving."""
        if self.last_progress_at is None:
            return False
        frozen_for = now - self.last_progress_at
        if frozen_for < self.cfg.stall_timeout:
            return False
        if not self.position_moves:
            # Some streams never report a moving position; don't restart
            # those forever, just turn frozen-picture detection off.
            if not self.no_position_logged:
                log.info("VLC is not reporting a moving playback position for "
                         "this stream; frozen-picture detection is off until "
                         "the next relaunch")
                self.no_position_logged = True
            return False
        return True

    def _mark_healthy(self, now):
        if self.healthy_since is None:
            self.healthy_since = now
        elif self.failures and now - self.healthy_since >= STABLE_RESET:
            log.info("Stream healthy for %ds; resetting restart backoff",
                     int(STABLE_RESET))
            self.failures = 0

    # -- processes ---------------------------------------------------------

    def _reap(self):
        """Collect our VLC if it exited, so it is not left as a zombie."""
        if self.proc is not None and self.proc.poll() is not None:
            log.info("VLC (pid %d) exited with code %s",
                     self.proc.pid, self.proc.returncode)
            self.proc = None

    def _kill_all(self):
        if not vlc_pids():
            return
        signal_vlc("TERM")
        deadline = time.monotonic() + KILL_TIMEOUT
        while time.monotonic() < deadline:
            self._reap()
            if not vlc_pids():
                return
            time.sleep(0.25)
        log.warning("VLC did not exit within %ds; force-killing it",
                    int(KILL_TIMEOUT))
        signal_vlc("KILL")
        time.sleep(1)
        self._reap()
        if vlc_pids():
            log.error("VLC is still running after SIGKILL: %s", vlc_pids())

    def relaunch(self, reason):
        log.warning("Relaunching VLC: %s", reason)
        self._kill_all()
        try:
            self.proc = subprocess.Popen(
                vlc_command(self.cfg),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log.info("Started %s (pid %d)", self.cfg.vlc_binary, self.proc.pid)
        except OSError as e:
            log.error("Could not start %s: %s", self.cfg.vlc_binary, e)
            self.proc = None

        now = time.monotonic()
        delay = min(self.cfg.cooldown_time * 2 ** min(self.failures, MAX_BACKOFF_STEPS),
                    self.cfg.max_backoff)
        delay = max(delay, self.cfg.startup_grace)
        self.failures += 1
        self.launched_at = now
        self.next_action_at = now + delay
        self.waiting_logged = False
        self._reset_tracking()
        if self.failures > 1:
            log.info("Restart #%d in a row; next relaunch allowed in %ds",
                     self.failures, int(delay))

    def _get_bus(self):
        if self.bus is None:
            self.bus = dbus.SessionBus()
        return self.bus

    # -- main check --------------------------------------------------------

    def poll(self):
        self._reap()
        now = time.monotonic()
        in_grace = now - self.launched_at < self.cfg.startup_grace
        may_restart = not in_grace and now >= self.next_action_at

        pids = vlc_pids()
        try:
            bus = self._get_bus()
            names = mpris_names(bus)
        except dbus.exceptions.DBusException as e:
            log.error("D-Bus session bus unavailable: %s", e)
            self.bus = None
            return

        # Two VLCs fighting over full screen: fix right away, ignoring the
        # backoff, once our own launch has had time to settle.
        if len(pids) > 1 or len(names) > 1:
            if not in_grace:
                self.relaunch(f"{len(pids)} VLC processes and {len(names)} "
                              "VLC players are running; keeping only one")
            return

        if not pids:
            if may_restart or (not in_grace and self.failures == 0):
                self.relaunch("VLC is not running")
            elif not self.waiting_logged:
                log.info("VLC is not running; next relaunch in %ds",
                         max(0, int(self.next_action_at - now)))
                self.waiting_logged = True
            return

        if not names:
            if may_restart:
                self.relaunch("VLC is running but not answering on D-Bus (MPRIS)")
            return

        try:
            status, position = read_player(bus, names[0])
        except dbus.exceptions.DBusException as e:
            if may_restart:
                self.relaunch(f"could not read VLC's state: {e}")
            return

        self._track_status(status, now)

        if self._is_flapping() and not in_grace:
            self.relaunch(f"playback state changed {len(self.status_changes)} "
                          f"times in {int(FLAP_WINDOW)}s")
            return

        if status == "Playing":
            self._track_position(position, now)
            if self._is_stalled(now):
                if may_restart:
                    self.relaunch("picture is frozen: playback position has not "
                                  f"moved for {int(now - self.last_progress_at)}s")
                return
            self._mark_healthy(now)
            return

        # Stopped or paused.
        if not may_restart:
            return
        log.info("VLC is %s; sending Play", status)
        try:
            send_play(bus, names[0])
            time.sleep(PLAY_RECHECK)
            new_status, _ = read_player(bus, names[0])
        except dbus.exceptions.DBusException as e:
            self.relaunch(f"Play over MPRIS failed: {e}")
            return
        if new_status == "Playing":
            log.info("VLC resumed playing")
            self._track_status(new_status, time.monotonic())
            self.next_action_at = time.monotonic() + self.cfg.cooldown_time
        else:
            self.relaunch(f"VLC stayed {new_status} after Play")


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    cfg = load_config()
    log.info("vlcstreamkeeper %s starting (poll %ds, cooldown %ds, "
             "stall timeout %ds, player %s)", __version__, cfg.poll_time,
             cfg.cooldown_time, cfg.stall_timeout, cfg.vlc_binary)
    keeper = Keeper(cfg)
    try:
        while True:
            try:
                keeper.poll()
            except Exception:
                log.exception("Unexpected error; carrying on")
            time.sleep(cfg.poll_time)
    except KeyboardInterrupt:
        log.info("Stopped by user")


if __name__ == "__main__":
    main()
