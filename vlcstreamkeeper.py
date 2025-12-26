"""
vlcstreamkeeper.py

Monitors the VLC viewer, if the viewer stops a stream, the spacebar is depressed
to restart the stream.

This module is used to ensure an RTSP camera stream is displayed on a small
display driven by a raspberry pi 4, or similar device, with no attached
keyboard or mouse. It eliminates the need to remote into the desktop
GUI of the RPI4 and restart the stream ~1-2 days as observed by the author.

Functions:
main():
    Main function
get_vlc_status():
    Gets the VLC status
press_space_in_vlc:
    Presses the spacebar

Dependencies:
    python3-dbus
    xdotool - no longer required on current version
"""

import time
import subprocess
import dbus #there is an error when this is viewed in pycharm on Windows, can be ignored for linux


__author__ = ""
__copyright__ = "Jessie-Newtworks"
__credits__ = ["Jessie-Newtworks"]
__license__ = "MIT"
__version__ = "0.0.1"
__maintainer__ = "Jessie-Newtworks"
__email__ = "via github"
__status__ = "Alpha"

POLL_TIME = 2
COOLDOWN_TIME = 10


def get_vlc_status():
    """
    Checks the status of the VLC stream
    :raises: dbus.exceptions.DBusException if VLC is not available
    :return: String of 'Playing', 'Paused', 'Stopped'
    """

    bus = dbus.SessionBus()
    proxy = bus.get_object("org.mpris.MediaPlayer2.vlc", "/org/mpris/MediaPlayer2")
    props = dbus.Interface(proxy, "org.freedesktop.DBus.Properties")
    status = props.Get("org.mpris.MediaPlayer2.Player", "PlaybackStatus")
    return str(status)


def find_vlc_id():
    """
    Finds the VLC ID
    :return: subprocess id of the vlc window or none if not found
    :raises: subprocess.CalledProcessError if VLC is not available
    """

    try:
        out = subprocess.check_output(["xdotool", "search", "--onlyvisible"
                                          , "--class", "vlc"], text=True)
        vlc_id = out.strip().splitlines()[0].strip()
        return vlc_id if vlc_id else None
    except subprocess.CalledProcessError:
        return None


def press_space_in_vlc():
    try:
        bus = dbus.SessionBus()
        proxy = bus.get_object("org.mpris.MediaPlayer2.vlc", "/org/mpris/MediaPlayer2")
        player = dbus.Interface(proxy, "org.mpris.MediaPlayer2.Player")
        player.PlayPause()
        print("Sent play/pause signal to VLC with MPRIS")
    except dbus.exceptions.DBusException as e:
        print(f"Command via MPRIS failed: {e}")

    # removed since this isn't working with wayland
    # win_id = find_vlc_id()
    # if not win_id:
    #     print("VLC window not found (is the GUI open?)")
    #     return
    # subprocess.run(["xdotool", "windowactivate", "--sync", win_id], check=False)
    # subprocess.run(["xdotool", "key", "--window", win_id, "space"], check=False)
    # print("Sent SPACE to VLC")


def main():
    """
    Main function, runs the script to monitor the vlc stream and click spacebar
    :return: none
    """
    prev_action_time = 0.0
    prev_status = None

    while True:
        try:
            status = get_vlc_status()
        except Exception as e:
            print(f"VLC not available: {e}")
            time.sleep(POLL_TIME)
            continue

        #Debugging checks
        if status != prev_status:
            print(f"VLC status: {status}")
            prev_status = status

        now = time.time()

        if status != "Playing" and (now - prev_action_time) >= COOLDOWN_TIME:
            press_space_in_vlc()
            prev_action_time = now

        time.sleep(POLL_TIME)


if __name__ == '__main__':
    main()