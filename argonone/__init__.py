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
import socket
from jsonrpclib.SimpleJSONRPCServer import SimpleJSONRPCServer
from jsonrpclib import ServerProxy as JSONRPCProxy
from contextlib import contextmanager
import shlex
import subprocess
import time
import yaml

from typing import Generic, TypeVar, Sequence, List, Dict, Union, Optional

# TODO - Add logging support

__all__ = ['get_pi_temperature', 'Fan', 'StepFunction', 'ArgonDaemon', 'daemon_client']

_SHUTDOWN_BCM_PIN = 4
_SMBUS_DEV = 1 if GPIO.RPI_INFO['P1_REVISION'] > 1 else 0
_SMBUS_ADDRESS = 0x1a
_VCGENCMD_PATH = '/usr/bin/vcgencmd'
_SYSFS_TEMPERATURE_PATH = '/sys/class/thermal/thermal_zone0/temp'
_CONFIG_LOCATIONS = [
  '/etc/argonone.yaml', '/usr/local/etc/argonone.yaml',
  '$HOME/.config/argonone.yaml', './argonone.yaml',   # XXX - are these two, esp last one, safe??
]
_RPC_SOCK_PATH = '/tmp/argonone.sock'


def _is_monotone_increasing(seq: Sequence) -> bool:
  return all(seq[i-1] < seq[i] for i in range(1, len(seq)))


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
      pass

  def close(self) -> None:
    self._bus.close()

  def __del__(self):
    self.close()


T = TypeVar('T')

class StepFunction(Generic[T]):  # noqa: E302
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


class PowerControlThread(Thread):
  def __init__(self, reboot_cmd: str, shutdown_cmd: str, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._reboot_cmdargs = shlex.split(reboot_cmd)
    self._shutdown_cmdargs = shlex.split(shutdown_cmd)
    self._control_enabled = True

  @property
  def control_enabled(self) -> bool:
    return self._control_enabled

  def disable_control(self) -> None:
    self._control_enabled = False

  def enable_control(self) -> None:
    self._control_enabled = True

  def run(self):
    # Set up GPIO pin to listen
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(_SHUTDOWN_BCM_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    self._stop = False
    while not self._stop:
      # Logic based on Argon's scripts: it appears that:
      #  - if pulse duration is between 10-30msec, then should reboot
      #  - if pulse duration is betweenm 30-50msec, then should shutdown
      #  - otherwise, nothing should be done
      # Both ranges are inclusive-exlcuside
      GPIO.wait_for_edge(_SHUTDOWN_BCM_PIN, GPIO.RISING)
      rise_time = time.time()
      GPIO.wait_for_edge(_SHUTDOWN_BCM_PIN, GPIO.FALLING)
      pulse_time = time.time() - rise_time
      if 0.01 <= pulse_time < 0.03:
        if self._control_enabled:
          subprocess.run(self._reboot_cmdargs)
      elif 0.03 <= pulse_time < 0.05:
        if self._control_enabled:
          subprocess.run(self._shutdown_cmdargs)

  def stop(self):
    self._stop = True


class FanControlThread(Thread):
  def __init__(self, fan_speed_lut: StepFunction,
               hysteresis_sec: float, poll_interval_sec: float, *args, **kwargs):
    super().__init__(*args, **kwargs)
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

  @property
  def control_enabled(self) -> bool:
    return self._control_enabled

  def enable_control(self) -> None:
    self._control_enabled = True

  def disable_control(self) -> None:
    self._control_enabled = False

  def run(self) -> None:
    self._stop = False
    while not self._stop:
      self._temperature = get_pi_temperature()
      if self._control_enabled:
        speed = round(self._fan_speed_lut(self._temperature))
        if speed != self.fan_speed:
          self.fan_speed = speed
          # TODO - Implement hysteresis
      time.sleep(self._poll_interval)

  def stop(self) -> None:
    self._stop = True


class RPCThread(Thread):
  def __init__(self, daemon: 'ArgonDaemon', *args, **kwargs):
    super().__init__(args, kwargs)
    self._daemon = daemon

  def _get_temperature(self) -> float:
    return self._daemon.temperature

  def _get_fan_speed(self) -> int:
    return self._daemon.fan_speed

  def _set_fan_speed(self, speed: int) -> None:
    self._daemon.fan_speed = speed

  def _disable_fan_control(self) -> None:
    self._daemon.disable_fan_control()

  def _enable_fan_control(self) -> None:
    self._daemon.enable_fan_control()

  def _is_fan_control_enabled(self) -> bool:
    return self._daemon.fan_control_enabled

  def _disable_power_control(self) -> None:
    self._daemon.disable_power_control()

  def _enable_power_control(self) -> None:
    self._daemon.enable_power_control()

  def _is_power_control_enabled(self) -> bool:
    return self._daemon.power_control_enabled

  def _shutdown(self) -> None:
    # Stop fan first
    self._daemon.disable_fan_control()
    self._daemon.fan_speed = 0
    # Finally, stop server
    self._daemon.stop()

  def run(self) -> None:
    if os.path.exists(_RPC_SOCK_PATH):
      os.remove(_RPC_SOCK_PATH)
    self._server = SimpleJSONRPCServer(_RPC_SOCK_PATH, address_family=socket.AF_UNIX)
    self._server.register_function(self._get_temperature, 'get_temp')
    self._server.register_function(self._get_fan_speed, 'get_fan_speed')
    self._server.register_function(self._set_fan_speed, 'set_fan_speed')
    self._server.register_function(self._disable_fan_control, 'disable_fan_control')
    self._server.register_function(self._enable_fan_control, 'enable_fan_control')
    self._server.register_function(self._is_fan_control_enabled, 'is_fan_control_enabled')
    self._server.register_function(self._disable_power_control, 'disable_power_control')
    self._server.register_function(self._enable_power_control, 'enable_power_control')
    self._server.register_function(self._is_power_control_enabled, 'is_power_control_enabled')
    self._server.register_function(self._shutdown, 'shutdown')
    try:
      self._server.serve_forever()
    finally:
      os.remove(_RPC_SOCK_PATH)

  def stop(self) -> None:
    self._server.shutdown()
    self._server.server_close()
    try:  # XXX - Is this necessary (given finally in run())?
      os.remove(_RPC_SOCK_PATH)
    except IOError:
      pass


class ArgonDaemon:
  @staticmethod
  def load_config() -> Optional[dict]:
    config = None
    for config_location in _CONFIG_LOCATIONS:
      config_path = os.path.expandvars(config_location)
      if os.path.isfile(config_path):
        with open(config_path, 'r') as fp:
          config = yaml.load(fp)
        break
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
    self._fan_control_thread = FanControlThread(fan_lut, hysteresis, poll_interval)
    if not fan_control_enabled:
      self._fan_control_thread.pause_control()
    reboot_cmd = power_config.get('reboot_cmd', 'sudo reboot')
    shutdown_cmd = power_config.get('shutdown_cmd', 'sudo shutdown -h now')
    power_control_enabled = power_config.get('enabled', True)
    self._power_control_thread = PowerControlThread(reboot_cmd, shutdown_cmd)
    if not power_control_enabled:
      self._power_control_thread.disable_control()
    self._rpc_thread = RPCThread(self)

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

  def start(self) -> None:
    self._power_control_thread.start()
    self._fan_control_thread.start()
    self._rpc_thread.start()

  def stop(self) -> None:
    self._power_control_thread.stop()
    self._fan_control_thread.stop()
    self._rpc_thread.stop()

  def wait(self) -> None:
    self._power_control_thread.join()
    self._fan_control_thread.join()
    self._rpc_thread.join()

  def close(self) -> None:
    self._rpc_thread.close()


@contextmanager
def daemon_client() -> JSONRPCProxy:
  try:
    proxy = JSONRPCProxy('unix+http://.' + _RPC_SOCK_PATH)
    yield proxy
  finally:
    proxy('close')()
