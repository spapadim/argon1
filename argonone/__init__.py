# (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
#
# This file is released under the MIT License:
#    https://opensource.org/licenses/MIT
# This software is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.

import smbus
import RPi.GPIO as GPIO
from threading import Thread, Lock
import os
from contextlib import contextmanager
import shlex
import subprocess
import time
import yaml
import logging

from typing import Generic, TypeVar, Sequence, List, Dict, Iterator, Tuple, Union, Optional

from gi.repository import GLib
import dbus
import dbus.service
import dbus.mainloop.glib

__all__ = [
  'Fan', 'get_pi_temperature', 'StepFunction',
  'ArgonDaemon', 'dbus_proxy'
]

dbus.mainloop.glib.threads_init()
log = logging.getLogger("argononed")

NOTIFY_VALUE_TEMPERATURE = "temperature"
NOTIFY_VALUE_FAN_SPEED = "fan_speed"
NOTIFY_VALUE_FAN_CONTROL_ENABLED = "fan_control_enabled"
NOTIFY_VALUE_POWER_CONTROL_ENABLED = "power_control_enabled"
NOTIFY_EVENT_SHUTDOWN = "shutdown_request"
NOTIFY_EVENT_REBOOT = "reboot_request"


############################################################################
# Constants (private)

_SHUTDOWN_BCM_PIN = 4
_SHUTDOWN_GPIO_TIMEOUT_MS = 10000
_SMBUS_DEV = 1 if GPIO.RPI_INFO['P1_REVISION'] > 1 else 0
_SMBUS_ADDRESS = 0x1a
_VCGENCMD_PATH = '/usr/bin/vcgencmd'
_SYSFS_TEMPERATURE_PATH = '/sys/class/thermal/thermal_zone0/temp'
_CONFIG_LOCATIONS = [
  '/etc/argonone.yaml',
  '$HOME/.config/argonone.yaml',   # XXX - is this safe??
]


############################################################################
# Fan hardware API (I2C)

class Fan:
  def __init__(self, initial_speed: int = 0):
    self._bus = smbus.SMBus(_SMBUS_DEV)
    self.speed = initial_speed

  @property
  def speed(self) -> int:
    return self._speed

  @speed.setter
  def speed(self, value: int) -> None:
    # Threshold speed value between 0 and 100 (inclusive)
    value = max(min(value, 100), 0)
    # Send I2C command
    try:
      self._bus.write_byte_data(_SMBUS_ADDRESS, 0, int(value))
      self._speed = value  # Only update if write was successful
    except IOError:
      log.warn("Fan control I2C command failed")

  def close(self) -> None:
    self._bus.close()

  def __del__(self):
    self.close()


############################################################################
# Auxilliary classes and functions

def _is_monotone_increasing(seq: Sequence) -> bool:
  return all(seq[i-1] < seq[i] for i in range(1, len(seq)))


T = TypeVar('T')

class StepFunction(Generic[T]):  # noqa: E302
  ItemIterator = Iterator[Tuple[Optional[float], T]]

  @classmethod
  def from_config_lut(cls, lut: Sequence[Dict[Union[str, float], T]]) -> 'StepFunction[T]':
    # Check arguments
    if len(lut) < 1:
      raise ValueError("LUT spec is empty!")
    if not all(len(d) == 1 for d in lut):  # lut must be sequence of singleton dicts
      raise ValueError("LUT entries must consist of a single temp:speed pair")
    if 'default' not in lut[0]:  # Works because we know that len(lut[0]) == 1
      raise ValueError("First LUT entry must specify default value")
    # Convert LUT to parallel lists (for "normal" constructor)
    thresholds: List[float] = []
    values: List[float] = []
    # XXX - is list(d.items())[0] less abstruse than next(iter(d.items())) ?
    lut_pairs = (next(iter(d.items())) for d in lut)
    for x, y in lut_pairs:
      if x != 'default':
        thresholds.append(x)
      values.append(y)
    # Construct step function object
    return cls(thresholds, values)

  def __init__(self, thresholds: Sequence[float], values: Sequence[T]):
    if len(values) != len(thresholds) + 1:
      raise ValueError("Number of thresholds and values do not match")
    if not _is_monotone_increasing(thresholds):
      raise ValueError("Threshold values are not sorted and/or not distinct")
    self._values = values
    self._thresholds = thresholds

  def __call__(self, x: float) -> T:
    for i, xi in enumerate(self._thresholds):
      if x < xi:
        return self._values[i]
    return self._values[-1]

  def items(self) -> ItemIterator:
    yield (None, self._values[0])
    yield from zip(self._thresholds, self._values[1:])  # XXX use itertools.islice?


# vcgencmd-based implementation
# def get_pi_temperature() -> Optional[float]:
#   result = subprocess.run([_VCGENCMD_PATH, 'measure_temp'], capture_output=True)
#   output = result.stdout.strip()
#   if output.startswith(b'temp='):
#     return float(output[len('temp='):-len('\'C')])
#   return None  # Failed to parse temperature value


# sysfs-based implementation (using path found in gpiozero library)
def get_pi_temperature() -> Optional[float]:
  try:
    with open(_SYSFS_TEMPERATURE_PATH, 'r') as fp:
      return int(fp.read().strip()) / 1000.0
  except (IOError, ValueError):
    return None


############################################################################
# Power button monitoring and control

# Point-of-authority for power-button.
# Monitors power button signals, and controls power state.
# Anything related to power button should be delegated here.
class PowerControlThread(Thread):
  def __init__(self, daemon: 'ArgonDaemon', reboot_cmd: str, shutdown_cmd: str, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.daemon = daemon  # XXX use weakref?
    self._reboot_cmdargs = shlex.split(reboot_cmd)
    self._shutdown_cmdargs = shlex.split(shutdown_cmd)
    self._control_enabled = True

  @property
  def control_enabled(self) -> bool:
    return self._control_enabled

  def disable_control(self) -> None:
    self._control_enabled = False
    self.daemon.notify(NOTIFY_VALUE_POWER_CONTROL_ENABLED, False)
    log.info("Power button control disabled")

  def enable_control(self) -> None:
    self._control_enabled = True
    self.daemon.notify(NOTIFY_VALUE_POWER_CONTROL_ENABLED, True)
    log.info("Power button control enabled")

  def run(self):
    log.info("Power button monitoring and control thread starting")
    # Set up GPIO pin to listen
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(_SHUTDOWN_BCM_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    self._stop_requested = False
    while not self._stop_requested:
      # Logic based on Argon's scripts: it appears that:
      #  - if pulse duration is between 10-30msec, then should reboot
      #  - if pulse duration is betweenm 30-50msec, then should shutdown
      #  - otherwise, nothing should be done
      # Both ranges are inclusive-exlcuside
      if GPIO.wait_for_edge(_SHUTDOWN_BCM_PIN, GPIO.RISING, timeout=_SHUTDOWN_GPIO_TIMEOUT_MS) is None:
        continue  # Timed out
      rise_time = time.time()
      if GPIO.wait_for_edge(_SHUTDOWN_BCM_PIN, GPIO.FALLING, timeout=500) is None:
        log.warn("Power button monitor giving up on pulse that seems to exceed 500msec!")
        continue
      pulse_time = time.time() - rise_time
      if 0.01 <= pulse_time < 0.03:
        log.info("Power button reboot detected")
        self.daemon.notify(NOTIFY_EVENT_REBOOT)
        if self._control_enabled:
          log.info("Issuing reboot command")
          subprocess.run(self._reboot_cmdargs)
      elif 0.03 <= pulse_time < 0.05:
        log.info("Power button shutdown detected")
        self.daemon.notify(NOTIFY_EVENT_SHUTDOWN)
        if self._control_enabled:
          log.info("Issuing shutdown command")
          subprocess.run(self._shutdown_cmdargs)

    log.info("Power button monitoring and control thread exiting")

  def stop(self):
    self._stop_requested = True


############################################################################
# Temperature monitoring and fan control

# Point-of-authority for fan and temperature.
# Monitors temperature, and controls fan.
# Anything related to fan and temperature should be delegated here.
class FanControlThread(Thread):
  def __init__(self, daemon: 'ArgonDaemon', fan_speed_lut: StepFunction,
               hysteresis_sec: float, poll_interval_sec: float, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.daemon = daemon  # XXX use weakref?
    self._fan = Fan()
    self._fan_mutex = Lock()
    self._fan_speed_lut = fan_speed_lut
    self._poll_interval = poll_interval_sec
    self._hysteresis = hysteresis_sec  # How long to wait before reducing speed
    self._temperature = get_pi_temperature()
    self._control_enabled = True

  @property
  def temperature(self) -> float:
    return self._temperature

  @property
  def fan_speed(self) -> int:
    with self._fan_mutex:
      return self._fan.speed

  @fan_speed.setter
  def fan_speed(self, value: int) -> None:
    with self._fan_mutex:
      self._fan.speed = value
      self.daemon.notify(NOTIFY_VALUE_FAN_SPEED, self._fan.speed)

  @property
  def fan_lut(self) -> StepFunction.ItemIterator:
    return self._fan_speed_lut.items()

  @property
  def control_enabled(self) -> bool:
    return self._control_enabled

  def enable_control(self) -> None:
    self._control_enabled = True
    self.daemon.notify(NOTIFY_VALUE_FAN_CONTROL_ENABLED, True)
    log.info("Fan control disabled")

  def disable_control(self) -> None:
    self._control_enabled = False
    self.daemon.notify(NOTIFY_VALUE_FAN_CONTROL_ENABLED, False)
    log.info("Fan control enabled")

  def run(self) -> None:
    log.info("Fan control and temperature monitoring thread starting")
    self._stop_requested = False
    while not self._stop_requested:
      self._temperature = get_pi_temperature()
      self.daemon.notify(NOTIFY_VALUE_TEMPERATURE, self._temperature)
      if self._control_enabled:
        speed = round(self._fan_speed_lut(self._temperature))
        if speed != self.fan_speed:
          log.info(f"Adjusting fan speed to {speed} for temperature {self._temperature}")
          self.fan_speed = speed
          # TODO - Implement hysteresis
      time.sleep(self._poll_interval)
    log.info("Fan control and temperature monitoring thread exiting")

  def stop(self) -> None:
    self._stop_requested = True


############################################################################
# D-Bus service

# class ArgonOneException(dbus.DBusException):
#   _dbus_error_name = 'net.clusterhack.ArgonOneException'

# XXX python-dbus does not like type annotations
class ArgonOne(dbus.service.Object):
  def __init__(self, conn, daemon: 'ArgonDaemon', object_path: str = '/net/clusterhack/ArgonOne'):
    super().__init__(conn, object_path)
    self.daemon = daemon

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='i')
  def GetFanSpeed(self):
    return self.daemon.fan_speed

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='i', out_signature='')
  def SetFanSpeed(self, speed: int):
    self.daemon.fan_speed = speed

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='d')
  def GetTemperature(self):
    return self.daemon.temperature

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='b')
  def GetFanControlEnabled(self):
    return self.daemon.fan_control_enabled

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='b', out_signature='')
  def SetFanControlEnabled(self, enable):
    if enable:
      self.daemon.enable_fan_control()
    else:
      self.daemon.disable_fan_control()

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='b')
  def GetPowerControlEnabled(self):
    return self.daemon.power_control_enabled

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='b', out_signature='')
  def SetPowerControlEnabled(self, enable):
    if enable:
      self.daemon.enable_power_control()
    else:
      self.daemon.disable_power_control()

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='')
  def Shutdown(self):
    self.daemon.stop()

  @dbus.service.signal("net.clusterhack.ArgonOne", signature='sv')
  def NotifyValue(self, name, value):
    pass

  @dbus.service.signal("net.clusterhack.ArgonOne", signature='s')
  def NotifyEvent(self, name):
    pass


# Point-of-authority for D-Bus.
# "Monitors" D-Bus, and "controls" signal emmissions.
# Anything related to D-Bus should be delegated here.
class DBusServerThread(Thread):
  def __init__(self, daemon: 'ArgonDaemon', *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.daemon = daemon  # XXX use weakref?
    self.argon = None

  def notify(self, name: str, value: Optional[Union[bool, int, str]] = None) -> None:
    if self.argon is None:
      return
    if value is not None:
      self.argon.NotifyValue(name, value)
    else:
      self.argon.NotifyEvent(name)

  def run(self) -> None:
    log.info("D-Bus server initialization")
    dbus_loop = dbus.mainloop.glib.DBusGMainLoop()
    system_bus = dbus.SystemBus(mainloop=dbus_loop)
    try:
      name = dbus.service.BusName("net.clusterhack.ArgonOne", system_bus)  # noqa: F841
      self.argon = ArgonOne(system_bus, self.daemon)
      self.mainloop = GLib.MainLoop()
      log.info("D-Bus server thread starting")
      self.mainloop.run()
    finally:
      system_bus.close()
    log.info("D-Bus server thread exiting")

  def stop(self) -> None:
    self.mainloop.quit()  # XXX - use GLib.idle_add ?


# Coordinates the three types of monitor & control threads,
# delegating requests accordingly.
class ArgonDaemon:
  @staticmethod
  def load_config() -> Optional[dict]:
    config = None
    for config_location in _CONFIG_LOCATIONS:
      config_path = os.path.expandvars(config_location)
      if os.path.isfile(config_path):
        log.info(f"Loading config file from {config_path}")
        with open(config_path, 'r') as fp:
          config = yaml.load(fp)
        break
    if config is None:
      raise RuntimeError("No configuration file found!")
    return config

  def __init__(self):
    # Load configuration and extract relevant parameters
    config_yaml = self.load_config()
    power_config = config_yaml['power_button']
    fan_config = config_yaml['fan_control']
    # Initialize members
    fan_lut = StepFunction.from_config_lut(fan_config['speed_lut'])
    hysteresis = fan_config.get('hysteresis_sec', 30.0)
    poll_interval = fan_config.get('poll_interval_sec', 10.0)
    fan_control_enabled = fan_config.get('enabled', True)
    self._fan_control_thread = FanControlThread(self, fan_lut, hysteresis, poll_interval)
    if not fan_control_enabled:
      self._fan_control_thread.pause_control()
    reboot_cmd = power_config.get('reboot_cmd', 'sudo reboot')
    shutdown_cmd = power_config.get('shutdown_cmd', 'sudo shutdown -h now')
    power_control_enabled = power_config.get('enabled', True)
    self._power_control_thread = PowerControlThread(self, reboot_cmd, shutdown_cmd)
    if not power_control_enabled:
      self._power_control_thread.disable_control()
    self._dbus_thread = DBusServerThread(self)

  @property
  def fan_speed(self) -> int:
    return self._fan_control_thread.fan_speed

  @fan_speed.setter
  def fan_speed(self, value: int) -> None:
    self._fan_control_thread.fan_speed = value

  @property
  def temperature(self) -> float:
    return self._fan_control_thread.temperature

  @property
  def fan_control_enabled(self) -> bool:
    return self._fan_control_thread.control_enabled

  def disable_fan_control(self) -> None:
    self._fan_control_thread.disable_control()

  def enable_fan_control(self) -> None:
    self._fan_control_thread.enable_control()

  @property
  def power_control_enabled(self) -> bool:
    return self._power_control_thread.control_enabled

  def disable_power_control(self) -> None:
    self._power_control_thread.disable_control()

  def enable_power_control(self) -> None:
    self._power_control_thread.enable_control()

  def notify(self, name: str, value: Optional[Union[bool, float, int]] = None) -> None:
    self._dbus_thread.notify(name, value)

  def start(self) -> None:
    log.info("Daemon starting")
    self._dbus_thread.start()
    self._power_control_thread.start()
    self._fan_control_thread.start()

  def stop(self) -> None:
    log.info("Daemon stopping")
    # Stop in reverse start order
    self._fan_control_thread.stop()
    self._power_control_thread.stop()
    self._dbus_thread.stop()

  def wait(self) -> None:
    self._fan_control_thread.join()
    self._power_control_thread.join()
    self._dbus_thread.join()


@contextmanager
def dbus_proxy(dbus_loop: Optional[dbus.mainloop.NativeMainLoop] = None) -> dbus.proxies.Interface:
    # mainloop must be specified if one will be used
    system_bus = dbus.SystemBus(mainloop=dbus_loop)
    try:
      proxy = system_bus.get_object('net.clusterhack.ArgonOne',
                                    '/net/clusterhack/ArgonOne')
      iface = dbus.Interface(proxy, 'net.clusterhack.ArgonOne')
      yield iface
    finally:
      system_bus.close()
