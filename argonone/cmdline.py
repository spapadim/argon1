# (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
#
# This file is released under the MIT License:
#    https://opensource.org/licenses/MIT
# This software is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.

import sys
from . import ArgonDaemon, dbus_proxy


def _error_message(msg: str) -> None:
  print(msg, file=sys.stderr)


def _error_exit(error_msg: str, exit_status: int = 1) -> None:
  _error_message(error_msg)
  sys.exit(exit_status)


# Dictionary values are (dbus_method_name, arg0, arg1, ...)
# where argN is either a constant or a function to parse a string into value
# with restriction that functions cannot succeed constants (not validate)
# TODO validate restriction somewhere?
_argonctl_cmd_aliases = {
  'temperature': 'GetTemperature', 'temp': 'GetTemperature',
  'speed': 'GetFanSpeed',
  'set_speed': ('SetFanSpeed', int),
  'pause_fan': ('SetFanControlEnabled', False), 'pause': ('SetFanControlEnabled', False),
  'resume_fan': ('SetFanControlEnabled', True), 'resume': ('SetFanControlEnabled', True),
  'fan_status': 'GetFanControlEnabled',
  'fan_lut': 'GetFanSpeedLUT', 'lut': 'GetFanSpeedLUT',
  'power_status': 'GetPowerControlEnabled',
  'shutdown': 'Shutdown',
}

def argonctl_main() -> None:  # noqa: E302
  # Check and parse arguments
  if len(sys.argv) < 2:
    _error_exit("Command name is missing")
  cmd_name = sys.argv[1]
  try:
    cmd_info = _argonctl_cmd_aliases[cmd_name]
  except KeyError:
    _error_exit(f"Unrecognized command {cmd_name}")
  if not isinstance(cmd_info, tuple):
    cmd_info = (cmd_info,)
  num_user_args = sum(callable(ai) for ai in cmd_info[1:])  # XXX ugh.. also, see above
  if len(sys.argv) - 2 != num_user_args:
    _error_exit("Incorrect arguments for command f{cmd_name}")
  cmd_args = []
  for i, arg_info in enumerate(cmd_info[1:]):
    if callable(arg_info):
      try:
        cmd_args.append(arg_info(sys.argv[2 + i]))
      except:  # noqa: E722
        _error_exit("Failed to parse argument {i} for command f{cmd_name}")
    else:
      cmd_args.append(arg_info)
  # Make RPC call and print any result
  with dbus_proxy() as dbus:
    func = getattr(dbus, cmd_info[0])
    retval = func(*cmd_args)
    if retval is not None:
      print(retval)


# Utility function to try and pick a good logging format
def _is_started_by_system() -> bool:
  try:
    from psutil import Process
  except ImportError:
    return True
  parent_name = Process(Process().ppid()).name()
  return any(parent_name.endswith(progname) for progname in ('systemd', 'upstart', 'init'))


def argondaemon_main() -> None:
  import logging
  log_format = '%(levelname)s: %(message)s'
  if not _is_started_by_system():
    log_format = '%(asctime)s: ' + log_format
  logging.basicConfig(format=log_format, datefmt='%m/%d/%Y %H:%M:%S', level=logging.INFO)
  daemon = ArgonDaemon()
  daemon.start()
  daemon.wait()
