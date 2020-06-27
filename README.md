# argon1

This package provides temperature-based fan control and power button monitoring for the Argon One case for Raspberry Pi 4.  It is a complete re-write and does not share any code with the official Argon40 packages (which were only used to figure out relatively simple hardware protocols).  


> ## DISCLAIMER
> 
> **The package is in no way affiliated or endorsed by Argon40, and is _not_ officially supported.**
> Use it at your own discretion and risk.

I just got an Argon One case recently, I had a free weekend, and this provided an excellent excuse to play with setuptools and dpkg (which I always wanted but never got around to doing).  This was a personal "distraction" project, which you _may_ find useful.  As such, feel free to do as you wish with it, but do **not** expect any support or serious maintenance from me!

# Differences from official scripts

The main differences from the Argon40 script are:

* The daemon is much more configurable, via `/etc/argonone.yaml`.
* The daemon registers a system D-Bus service, which publishes notification signals, as well as methods to query and control it.
* The daemon does not run as root.
* Users can be selectively granted permission to control the daemon (all users can query it).
* The `argonctl` commandline utility provides an easy way to query/control over D-Bus.
* The daemon is installed as a systemd service.
* The package is "debianized" natively, and can be easily installed via `dpkg` or `apt`.

# Installation

Simply download the `.deb` file and install it via

```shell
sudo apt install argon1_x.y.z.deb
```

where `x.y.z` is the package version.

If the installer detects user accounts other than `pi`, it will prompt you to grant permission to control the daemon.  If you wish to add users yourself (e.g., if you want to selectively add a subset of user accounts, or if you create additional user accounts at a later time), you simply need to add them to the `argonone` group via

```shell
sudo usermod -a -G argonone username
```

where `username` should be replaced with the actual username of the user you wish to grant control permissions to.

# Usage

You can query the daemon using the `argonctl` utility command:

* `argonctl speed` shows the current fan speed setting.
* `argonctl temp` shows the last CPU temperature measurement.
* `argonctl pause` pauses temperature-based fan control; the fan will stay at whatever speed it was at the time the command was executed.
* `argonctl resume` resumes temperature-based fan control.
* `argonctl set_speed NNN` will set the fan speed to the requested value (must be between 0..100); if temperature-based fan control is not paused, then the daemon may change it the next time the temperature is measured (by default, this happens every 10 seconds).

There are additional commands that are probably less useful (use the source :).  If you wish to shutdown the daemon, please do so via systemd, e.g., `sudo systemctl stop argonone`.  If you use `argonctl shutdown` directly, systemd will think the daemon crashed and will attempt to restart it.

# Configuration

All configuration can be found in `/etc/argonone.yaml`, which should be self-explanatory.  

The default values should be fine and should not need to be adjusted.  The setting you are more likely to want to experiment with is the temperature-based fan control lookup table (LUT).  

If you modify the configuration file, then you need to restart the daemon for the changes to take effect, via 

```shell
sudo systemctl argonone restart
```

Finally, note that the `enabled` configuration values simply determine the _initial_ "paused"/"unpaused" state of each daemon component each time the daemon starts up.  However, this state can be toggled while the server is running, via the `argonctl` utility. For all other settings you _must_ restart the daemon (after editing `/etc/argonone.yaml`) to change them.

# Troubleshooting and monitoring

As stated earlier, you are on your own here! :)  However, you may wish to start by inspecting the systemd logs, e.g., via

```shell
systemctl status argonone
```

Furthermore, if you wish to monitor all daemon events, you can do so via, e.g.,

```shell
dbus-monitor --system "sender='net.clusterhack.ArgonOne'"
```

