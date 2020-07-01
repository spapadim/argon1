# (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
#
# This file is released under the MIT License:
#    https://opensource.org/licenses/MIT
# This software is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.

import sys
from . import ArgonDaemon, dbus_proxy

from typing import Optional, Callable, Sequence


def _error_message(msg: str) -> None:
  print(msg, file=sys.stderr)


def _error_exit(error_msg: str, exit_status: int = 1) -> None:
  _error_message(error_msg)
  sys.exit(exit_status)


# Simple class to describe how commandline arguments should be parsed,
# and how return values should be presented
class _CmdInfo(object):
  __slots__ = ['dbus_method', 'arg_fmt', 'return_fmt']

  def __init__(self, dbus_method: str, arg_fmt: Optional = None, return_fmt: Optional[Callable] = None):
    # We allow arg_fmt to be a single non-sequence item, to avoid singleton literal clutter
    if arg_fmt is None:
      arg_fmt = ()
    if not isinstance(arg_fmt, (tuple, list)):
      arg_fmt = (arg_fmt,)
    arg_fmt = tuple(arg_fmt)  # ensure immutable

    # Validate arg_fmt first
    if arg_fmt is not None:
      val_seen = False
      for val_or_func in arg_fmt:
        if callable(val_or_func):
          if val_seen:
            raise ValueError('Callables cannot follow values in arg_fmt')
        else:  # not is_func
          val_seen = True

    self.dbus_method = dbus_method
    self.arg_fmt = arg_fmt
    self.return_fmt = return_fmt

  @property
  def num_user_args(self) -> int:
    return sum(callable(af) for af in self.arg_fmt)  # XXX ugh?

  def call_dbus(self, dbus_proxy, argv: Sequence[str]) -> None:
    if len(argv) != self.num_user_args:
      raise ValueError("Wrong number of user-provided arguments (argv)")
    # Construct argument list for method call
    dbus_args = []
    for i, af in enumerate(self.arg_fmt):
      if callable(af):
        try:
          dbus_args.append(af(argv[i]))
        except:  # noqa: E722
          raise ValueError(f"Failed to convert arg{i} value for {self.dbus_method}")
      else:
        dbus_args.append(af)
    # Issue RPC and format return value (if needed)
    dbus_func = getattr(dbus_proxy, self.dbus_method)
    retval = dbus_func(*dbus_args)
    if retval is not None and self.return_fmt is not None:
      retval = self.return_fmt(retval)
    return retval


# Dictionary values are either _CmdInfo or strings.  A string value
# denotes an alias and should be equal to another key of the dictionary.
_argonctl_cmds = {
  'temp': _CmdInfo('GetTemperature'),
  'temperature': 'temp',
  
  'speed': _CmdInfo('GetFanSpeed'),
  'fan_speed': 'speed',
  'set_speed': _CmdInfo('SetFanSpeed', int),

  'pause': _CmdInfo('SetFanControlEnabled', False), 
  'pause_fan': 'pause',
  'resume': _CmdInfo('SetFanControlEnabled', True), 
  'resume_fan': 'resume',
  'fan_status': _CmdInfo('GetFanControlEnabled'),

  'lut': _CmdInfo(
    'GetFanSpeedLUT',
    None,
    lambda pairs: '\n'.join(f"{x if x != -1 else 'default'}: {int(y)}" for x, y in pairs)
  ),
  'fan_lut': 'lut',

  'pause_button': _CmdInfo('SetPowerControlEnabled', False),
  'resume_button': _CmdInfo('SetPowerControlEnabled', True),
  'button_status': _CmdInfo('GetPowerControlEnabled'),

  'shutdown': _CmdInfo('Shutdown'),
}

def argonctl_main() -> None:  # noqa: E302
  # Check and parse arguments
  if len(sys.argv) < 2:
    _error_exit("Command name is missing")
  cmd_name = sys.argv[1]
  try:
    # Look up _CmdInfo, resolving aliases
    cmd_info = cmd_name
    while isinstance(cmd_info, str):
      cmd_info = _argonctl_cmds[cmd_info]
  except KeyError:
    _error_exit(f"Unrecognized command {cmd_name}")
  # Make RPC call and print any result
  with dbus_proxy() as dbus:
    retval = cmd_info.call_dbus(dbus, sys.argv[2:])
    if retval:
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
