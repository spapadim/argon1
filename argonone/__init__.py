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
from contextlib import contextmanager, nullcontext
from enum import Enum
import shlex
import subprocess
import time
import yaml
import logging

from typing import Generic, TypeVar, Sequence, List, Dict, Iterator, Tuple, Union, Optional, ContextManager

from gi.repository import GLib
import dbus
import dbus.service
import dbus.mainloop.glib

__all__ = [
  'ArgonOneHardware', 'BUTTON_PRESS', 'get_pi_temperature', 'StepFunction',
  'ArgonDaemon', 'dbus_proxy', 'NOTIFY',
]

dbus.mainloop.glib.threads_init()
log = logging.getLogger("argononed")

NOTIFY = Enum('NOTIFY', [
  ('VALUE_TEMPERATURE', "temperature"),
  ('VALUE_FAN_SPEED', "fan_speed"),
  ('VALUE_FAN_CONTROL_ENABLED', "fan_control_enabled"),
  ('VALUE_POWER_CONTROL_ENABLED', "power_control_enabled"),
  ('EVENT_SHUTDOWN', "shutdown_request"),
  ('EVENT_REBOOT', "reboot_request"),
  ('EVENT_FAN_SPEED_LUT_CHANGED', "fan_speed_lut_changed"),
])

BUTTON_PRESS = Enum('BUTTON_PRESS', [
  'SHUTDOWN',
  'REBOOT',
])

############################################################################
# Constants (private)

_SHUTDOWN_BCM_PIN = 4
_SHUTDOWN_GPIO_TIMEOUT_MS = 10000
_SMBUS_DEV = 1 if GPIO.RPI_INFO['P1_REVISION'] > 1 else 0
_SMBUS_ADDRESS = 0x1a
_SMBUS_REGISTER = 0x00
_SMBUS_VALUE_ACK = 0x00  # Official scripts use 0x00, other values could work?
_SMBUS_VALUE_POWEROFF = 0xff
_VCGENCMD_PATH = '/usr/bin/vcgencmd'
_SYSFS_TEMPERATURE_PATH = '/sys/class/thermal/thermal_zone0/temp'
_CONFIG_LOCATIONS = [
  '/etc/argonone.yaml',
  '$HOME/.config/argonone.yaml',   # XXX - is this safe??
]


############################################################################
# Hardware API (GPIO & I2C)

class ArgonOneBoard:
  _fan_speed: Optional[int]
  _bus_mutex: Union[ContextManager, Lock]

  def __init__(self, initial_speed: Optional[int] = 0, bus_mutex: Optional[Lock] = None):
    self._bus_mutex = bus_mutex if bus_mutex is not None else nullcontext()
    # Set up I2C and initialize fan speed
    self._bus = smbus.SMBus(_SMBUS_DEV)
    if initial_speed is not None:
      self.fan_speed = initial_speed  # sets self._fan_speed, and also issues I2C command
    else:
      self._fan_speed = None  # self._fan_speed still needs to be defined
    # Set up GPIO pin to listen for power button presses
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(_SHUTDOWN_BCM_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

  def _bus_write(self, value: int, register: int = _SMBUS_REGISTER):
    # Could raise IOError, according to "official" scripts
    self._bus.write_byte_data(_SMBUS_ADDRESS, register, int(value))

  @property
  def is_threadsafe(self) -> bool:
    return not isinstance(self._bus_mutex, nullcontext)  # type: ignore

  @property
  def fan_speed(self) -> Optional[int]:
    # Since modifying fan_speed property involves bus write, we also protect this access...
    with self._bus_mutex:
      return self._fan_speed

  @fan_speed.setter
  def fan_speed(self, value: int) -> None:
    # Threshold speed value between 0 and 100 (inclusive)
    value = int(max(min(value, 100), 0))
    # Send I2C command
    with self._bus_mutex:
      try:
        self._bus_write(value)
        self._fan_speed = value  # Only update if write was successful
      except IOError:
        log.warn("Fan control I2C command failed")

  # XXX Originally assumed this would serve as an "ACK", to prevent board
  #   from cutting power, but that is not the case. In fact, the board will
  #   not only cut power after a short, fixed time, but it will also stop 
  #   reading from the I2C bus.  By the time the write times out, it is too
  #   late to start a shutdown and avoid a hard crash.
  #
  # def power_ack(self) -> None:
  #   # Send acknowledgment of power button press
  #   with self._bus_mutex:
  #     self._bus_write(_SMBUS_VALUE_ACK)

  def power_off(self) -> None:
    # Send request to turn power off
    with self._bus_mutex:
      self._bus_write(_SMBUS_VALUE_POWEROFF)

  def wait_for_button(self, timeout: int = _SHUTDOWN_GPIO_TIMEOUT_MS) -> Optional[BUTTON_PRESS]:
    # Logic based on Argon's scripts; it appears that:
    #  - if pulse duration is between 10-30msec, then should reboot
    #  - if pulse duration is betweenm 30-50msec, then should shutdown
    #  - otherwise, nothing should be done
    # Both ranges are inclusive-exlcuside
    if GPIO.wait_for_edge(_SHUTDOWN_BCM_PIN, GPIO.RISING, timeout=timeout) is None:
      return None  # Timed out
    rise_time = time.time()
    if GPIO.wait_for_edge(_SHUTDOWN_BCM_PIN, GPIO.FALLING, timeout=500) is None:
      log.warn("Power button monitor giving up on pulse that seems to exceed 500msec!")
      return None
    pulse_time = time.time() - rise_time
    if 0.01 <= pulse_time < 0.03:
      return BUTTON_PRESS.REBOOT
    elif 0.03 <= pulse_time < 0.05:
      return BUTTON_PRESS.SHUTDOWN
    else:
      return None

  def close(self) -> None:
    self._bus.close()

  def __del__(self):
    self.close()


############################################################################
# Auxilliary classes and functions

def _is_monotone_increasing(seq: Sequence) -> bool:
  return all(seq[i-1] < seq[i] for i in range(1, len(seq)))


# XXX failed to get this working
# from abc import abstractmethod, ABCMeta
# class Comparable(metaclass=ABCMeta):
#     @abstractmethod
#     def __lt__(self, other: Any) -> bool: ...

K = TypeVar('K')  # bound=Comparable)
V = TypeVar('V')

ItemIterator = Iterator[Tuple[Union[K, None], V]]

class StepFunction(Generic[K, V]):  # noqa: E302

  @classmethod
  def from_config_lut(cls, lut: Sequence[Dict[Union[str, K], V]]) -> 'StepFunction[K, V]':
    # Check arguments
    if len(lut) < 1:
      raise ValueError("LUT spec is empty!")
    if not all(len(d) == 1 for d in lut):  # lut must be sequence of singleton dicts
      raise ValueError("LUT entries must consist of a single temp:speed pair")
    if 'default' not in lut[0]:  # Works because we know that len(lut[0]) == 1
      raise ValueError("First LUT entry must specify default value")
    # Convert LUT to parallel lists (for "normal" constructor)
    thresholds: List[K] = []
    values: List[V] = []
    # XXX - is list(d.items())[0] less abstruse than next(iter(d.items())) ?
    lut_pairs = (next(iter(d.items())) for d in lut)
    for x, y in lut_pairs:
      if x != 'default':
        assert not isinstance(x, str)
        thresholds.append(x)
      values.append(y)
    # Construct step function object
    return cls(thresholds, values)

  @classmethod
  def from_iterator(cls, lut_iter: ItemIterator) -> 'StepFunction[K, V]':
    thresholds = []
    values = []
    for thr, val in lut_iter:
      if thr is not None:
        thresholds.append(thr)
      values.append(val)
    return cls(thresholds, values)

  def __init__(self, thresholds: Sequence[K], values: Sequence[V]):
    if len(values) != len(thresholds) + 1:
      raise ValueError("Number of thresholds and values do not match")
    if not _is_monotone_increasing(thresholds):
      raise ValueError("Threshold values are not sorted and/or not distinct")
    self._values: Sequence[V] = values
    self._thresholds: Sequence[K] = thresholds

  def __call__(self, x: K) -> V:
    for i, xi in enumerate(self._thresholds):
      if x < xi:  # type: ignore   # XXX see above for "Comparable" attempt
        return self._values[i]
    return self._values[-1]

  def items(self) -> ItemIterator[K, V]:
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
  def __init__(self, daemon: 'ArgonDaemon', argon_board: ArgonOneBoard,
               reboot_cmd: str, shutdown_cmd: str, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.argon_daemon = daemon  # XXX use weakref?
    self._argon_board = argon_board
    assert self._argon_board.is_threadsafe
    self._reboot_cmdargs = shlex.split(reboot_cmd)
    self._shutdown_cmdargs = shlex.split(shutdown_cmd)
    self._control_enabled = True

  @property
  def control_enabled(self) -> bool:
    return self._control_enabled

  def disable_control(self) -> None:
    self._control_enabled = False
    self.argon_daemon.notify(NOTIFY.VALUE_POWER_CONTROL_ENABLED, False)
    log.info("Power button control disabled")

  def enable_control(self) -> None:
    self._control_enabled = True
    self.argon_daemon.notify(NOTIFY.VALUE_POWER_CONTROL_ENABLED, True)
    log.info("Power button control enabled")

  def run(self):
    log.info("Power button monitoring and control thread starting")
    self._stop_requested = False
    while not self._stop_requested:
      log.info("DBG: calling .wait_for_button")
      button_press = self._argon_board.wait_for_button()
      log.info("DBG: button_press = %s", button_press)
      # XXX Originally assumed this would serve as an "ACK",
      #   but that is not the case (see comment above)
      # if button_press is not None:
      #   self._argon_board.power_ack()
      if button_press == BUTTON_PRESS.REBOOT:
        log.info("Power button reboot detected")
        self.argon_daemon.notify(NOTIFY.EVENT_REBOOT)
        if self._control_enabled:
          log.info("Issuing reboot command")
          subprocess.run(self._reboot_cmdargs)
      elif button_press == BUTTON_PRESS.SHUTDOWN:
        log.info("Power button shutdown detected")
        self.argon_daemon.notify(NOTIFY.EVENT_SHUTDOWN)
        if not self._control_enabled:
          log.warn("Ignoring disabled power control; ArgonOne will cut power in a hurry anyway")
        log.info("Issuing shutdown command")
        subprocess.run(self._shutdown_cmdargs)
    log.info("Power button monitoring and control thread exiting")

  def stop(self):
    self._stop_requested = True


############################################################################
# Temperature monitoring and fan control

LUTFunction = StepFunction[float, int]
LUTItemIterator = ItemIterator[float, int]

# Point-of-authority for fan and temperature.
# Monitors temperature, and controls fan.
# Anything related to fan and temperature should be delegated here.
class FanControlThread(Thread):  # noqa: E302
  def __init__(self, daemon: 'ArgonDaemon', argon_board: ArgonOneBoard, fan_speed_lut: LUTFunction,
               hysteresis_sec: float, poll_interval_sec: float, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.argon_daemon = daemon  # XXX use weakref?
    self._argon_board = argon_board
    assert self._argon_board.is_threadsafe
    self._fan_speed_lut = fan_speed_lut  # Need to guard direct access with mutex
    self._fan_speed_lut_mutex = Lock()
    self._poll_interval = poll_interval_sec
    self._hysteresis = hysteresis_sec  # How long to wait before reducing speed
    self._temperature = get_pi_temperature()
    self._control_enabled = True

  @property
  def temperature(self) -> Optional[float]:
    return self._temperature

  @property
  def fan_speed(self) -> Optional[int]:
    return self._argon_board.fan_speed

  @fan_speed.setter
  def fan_speed(self, value: int) -> None:
    self._argon_board.fan_speed = value
    # Read fan_speed back, as it's possible it wasn't actually changed
    self.argon_daemon.notify(NOTIFY.VALUE_FAN_SPEED, self._argon_board.fan_speed)

  @property
  def fan_speed_lut(self) -> LUTItemIterator:
    with self._fan_speed_lut_mutex:
      return self._fan_speed_lut.items()

  @fan_speed_lut.setter
  def fan_speed_lut(self, lut: Union[LUTFunction, LUTItemIterator]) -> None:
    if not isinstance(lut, StepFunction):
      lut = StepFunction.from_iterator(lut)
    with self._fan_speed_lut_mutex:
      self._fan_speed_lut = lut
    self.argon_daemon.notify(NOTIFY.EVENT_FAN_SPEED_LUT_CHANGED)

  @property
  def control_enabled(self) -> bool:
    return self._control_enabled

  def enable_control(self) -> None:
    self._control_enabled = True
    self.argon_daemon.notify(NOTIFY.VALUE_FAN_CONTROL_ENABLED, True)
    log.info("Fan control disabled")

  def disable_control(self) -> None:
    self._control_enabled = False
    self.argon_daemon.notify(NOTIFY.VALUE_FAN_CONTROL_ENABLED, False)
    log.info("Fan control enabled")

  def run(self) -> None:
    log.info("Fan control and temperature monitoring thread starting")
    self._stop_requested = False
    while not self._stop_requested:
      self._temperature = get_pi_temperature()
      if self._temperature is None:
        log.warn("Failed to read temperature")
      else:
        self.argon_daemon.notify(NOTIFY.VALUE_TEMPERATURE, self._temperature)
        if self._control_enabled:
          with self._fan_speed_lut_mutex:
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

class ArgonOneException(dbus.DBusException):
  _dbus_error_name = 'net.clusterhack.ArgonOneException'


# XXX python-dbus does not like type annotations
class ArgonOne(dbus.service.Object):
  def __init__(self, conn, daemon: 'ArgonDaemon', object_path: str = '/net/clusterhack/ArgonOne'):
    super().__init__(conn, object_path)
    self.argon_daemon = daemon

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='i')
  def GetFanSpeed(self):
    return self.argon_daemon.fan_speed

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='i', out_signature='')
  def SetFanSpeed(self, speed: int):
    self.argon_daemon.fan_speed = speed

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='d')
  def GetTemperature(self):
    return self.argon_daemon.temperature

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='b')
  def GetFanControlEnabled(self):
    return self.argon_daemon.fan_control_enabled

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='b', out_signature='')
  def SetFanControlEnabled(self, enable):
    if enable:
      self.argon_daemon.enable_fan_control()
    else:
      self.argon_daemon.disable_fan_control()

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='a(dd)')
  def GetFanSpeedLUT(self):
    lut_list = list(self.argon_daemon.fan_speed_lut)
    # None doesn't match D-Bus return signature, so replace with -1
    assert lut_list[0][0] is None
    lut_list[0] = (-1, lut_list[0][1])
    return lut_list

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='a(dd)', out_signature='')
  def SetFanSpeedLUT(self, lut_pairs):
    if len(lut_pairs) < 1 or lut_pairs[0][0] != -1:
      raise ArgonOneException("First LUT entry must be default value, with threshold of -1")
    lut_pairs[0][0] = None  # Couldn't do None with a clean D-Bus signature
    try:
      lut = StepFunction.from_iterator(lut_pairs)
    except ValueError as exc:
      raise ArgonOneException(f"Failed to parse LUT: {str(exc)}")
    self.argon_daemon.fan_speed_lut = lut

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='b')
  def GetPowerControlEnabled(self):
    return self.argon_daemon.power_control_enabled

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='b', out_signature='')
  def SetPowerControlEnabled(self, enable):
    if enable:
      self.argon_daemon.enable_power_control()
    else:
      self.argon_daemon.disable_power_control()

  @dbus.service.method("net.clusterhack.ArgonOne",
                       in_signature='', out_signature='')
  def Shutdown(self):
    self.argon_daemon.stop()

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
    self.argon_daemon = daemon  # XXX use weakref?
    self.argon_obj = None

  def notify(self, notify_type: NOTIFY, value: Optional[Union[bool, int, str]] = None) -> None:
    if self.argon_obj is None:
      return
    if value is not None:
      self.argon_obj.NotifyValue(notify_type.value, value)
    else:
      self.argon_obj.NotifyEvent(notify_type.value)

  def run(self) -> None:
    log.info("D-Bus server initialization")
    dbus_loop = dbus.mainloop.glib.DBusGMainLoop()
    system_bus = dbus.SystemBus(mainloop=dbus_loop)
    try:
      name = dbus.service.BusName("net.clusterhack.ArgonOne", system_bus)  # noqa: F841
      self.argon_obj = ArgonOne(system_bus, self.argon_daemon)
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
    return config  # type: ignore

  def __init__(self):
    # Load configuration and extract relevant parameters
    config_yaml = self.load_config()
    power_config = config_yaml['power_button']
    fan_config = config_yaml['fan_control']
    # Initialize members
    self._argon_board = ArgonOneBoard(initial_speed=0, bus_mutex=Lock())
    fan_lut = StepFunction.from_config_lut(fan_config['speed_lut'])
    hysteresis = fan_config.get('hysteresis_sec', 30.0)
    poll_interval = fan_config.get('poll_interval_sec', 10.0)
    fan_control_enabled = fan_config.get('enabled', True)
    self._fan_control_thread = FanControlThread(self, self._argon_board, fan_lut, hysteresis, poll_interval)
    if not fan_control_enabled:
      self._fan_control_thread.pause_control()
    reboot_cmd = power_config.get('reboot_cmd', 'sudo reboot')
    shutdown_cmd = power_config.get('shutdown_cmd', 'sudo shutdown -h now')
    power_control_enabled = power_config.get('enabled', True)
    self._power_control_thread = PowerControlThread(self, self._argon_board, reboot_cmd, shutdown_cmd)
    if not power_control_enabled:
      self._power_control_thread.disable_control()
    self._dbus_thread = DBusServerThread(self)

  @property
  def fan_speed(self) -> Optional[int]:
    return self._fan_control_thread.fan_speed  # type: ignore

  @fan_speed.setter
  def fan_speed(self, value: int) -> None:
    self._fan_control_thread.fan_speed = value

  @property
  def temperature(self) -> Optional[float]:
    return self._fan_control_thread.temperature  # type: ignore

  @property
  def fan_control_enabled(self) -> bool:
    return self._fan_control_thread.control_enabled  # type: ignore

  def disable_fan_control(self) -> None:
    self._fan_control_thread.disable_control()

  def enable_fan_control(self) -> None:
    self._fan_control_thread.enable_control()

  @property
  def fan_speed_lut(self) -> LUTItemIterator:
    return self._fan_control_thread.fan_speed_lut  # type: ignore

  @fan_speed_lut.setter
  def fan_speed_lut(self, lut: Union[LUTFunction, LUTItemIterator]) -> None:
    self._fan_control_thread.fan_speed_lut = lut

  @property
  def power_control_enabled(self) -> bool:
    return self._power_control_thread.control_enabled  # type: ignore

  def disable_power_control(self) -> None:
    self._power_control_thread.disable_control()

  def enable_power_control(self) -> None:
    self._power_control_thread.enable_control()

  def notify(self, notify_type: NOTIFY, value: Optional[Union[bool, float, int]] = None) -> None:
    self._dbus_thread.notify(notify_type, value)

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

  def close(self) -> None:
    self._argon_board.close()


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
