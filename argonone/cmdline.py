# (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
#
# This file is released under the MIT License:
#    https://opensource.org/licenses/MIT
# This software is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.

import sys
from . import ArgonOneBoard, ArgonDaemon, dbus_proxy

from typing import Any, Optional, Union, Callable, Sequence, Dict


def _error_message(msg: str) -> None:
  print("ERROR:", msg, file=sys.stderr)


def _error_exit(error_msg: str, exit_status: int = 1, usage: Optional[Callable] = None) -> None:
  if usage is not None:
    usage(file=sys.stderr)
  _error_message(error_msg)
  sys.exit(exit_status)


############################################################################
# argonctl utility

# Simple class to describe how commandline arguments should be parsed,
# and how return values should be presented
class _CmdInfo(object):
  __slots__ = ['dbus_method', 'arg_fmt', 'return_fmt']

  def __init__(self, dbus_method: str, arg_fmt: Any = None, return_fmt: Optional[Callable] = None):
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

  def call_dbus(self, dbus_proxy, argv: Sequence[str]) -> str:
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
    return retval  # type: ignore


def _enabled_fmt(val) -> str:
  return 'enabled' if val else 'disabled'

def _lut_fmt(pairs) -> str:  # noqa: E302
  return '\n'.join(f"{x if x != -1 else 'default'}: {int(y)}" for x, y in pairs)

# Dictionary values are either _CmdInfo or strings.  A string value
# denotes an alias and should be equal to another key of the dictionary.
_argonctl_cmds: Dict[str, Union[str, _CmdInfo]] = {  # noqa: E305
  'temp': _CmdInfo('GetTemperature'),
  'temperature': 'temp',

  'speed': _CmdInfo('GetFanSpeed'),
  'fan_speed': 'speed',
  'set_speed': _CmdInfo('SetFanSpeed', int),

  'pause': _CmdInfo('SetFanControlEnabled', False),
  'pause_fan': 'pause',
  'resume': _CmdInfo('SetFanControlEnabled', True),
  'resume_fan': 'resume',
  'fan_status': _CmdInfo('GetFanControlEnabled', None, _enabled_fmt),
  'fan_enabled': 'fan_status',

  'lut': _CmdInfo('GetFanSpeedLUT', None, _lut_fmt),
  'fan_lut': 'lut',

  'pause_button': _CmdInfo('SetPowerControlEnabled', False),
  'resume_button': _CmdInfo('SetPowerControlEnabled', True),
  'button_status': _CmdInfo('GetPowerControlEnabled', None, _enabled_fmt),
  'button_enabled': 'button_status',

  'shutdown': _CmdInfo('Shutdown'),
}

def _argonctl_print_usage(program_name=None, file=sys.stderr):  # noqa: E302
  if program_name is None:
    program_name = sys.argv[0]
  print(f"USAGE: {program_name} command [parameter]\n", file=file)
  # Collect aliases
  aliases = {}
  for cmd_name, cmd_info in _argonctl_cmds.items():  # XXX assumes order-preserving dicts (py >= 3.7)
    if isinstance(cmd_info, str):
      # TODO Assumes non-recursive aliases
      aliases[cmd_info].append(cmd_name)
    else:
      aliases[cmd_name] = []
  # Print list of commands
  print("COMMANDS", file=file)
  for cmd_name, alias_list in aliases.items():
    print("  " + " | ".join([cmd_name] + alias_list), file=file)
  print(file=file)

def argonctl_main() -> None:  # noqa: E302
  # Check and parse arguments
  if len(sys.argv) < 2:
    _error_exit("Command name is missing", usage=_argonctl_print_usage)
  cmd_name: str = sys.argv[1]
  # Handle "help" separately
  if cmd_name == 'help':
    _argonctl_print_usage()
    sys.exit(0)
  try:
    # Look up _CmdInfo, resolving aliases
    cmd_info: Union[str, _CmdInfo] = cmd_name
    while isinstance(cmd_info, str):
      cmd_info = _argonctl_cmds[cmd_info]
  except KeyError:
    _error_exit(f"Unrecognized command {cmd_name}", usage=_argonctl_print_usage)
  # Make RPC call and print any result
  assert isinstance(cmd_info, _CmdInfo)
  with dbus_proxy() as dbus:
    retval = cmd_info.call_dbus(dbus, sys.argv[2:])
    if retval is not None:
      print(retval)


############################################################################
# argononed system daemon

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
  try:
    daemon.start()
    daemon.wait()
  finally:
    daemon.close()


############################################################################
# systemd shutdown script

def argonshutdown_main() -> None:
  # Systemd shutdown script (runs after daemon is shut down)
  argon_board = ArgonOneBoard(initial_speed=None)  # no mutex necessary
  if len(sys.argv) > 1 and sys.argv[1] in ('poweroff', 'halt'):
    # The button press "ACK" command (set fan speed to zero)
    # will have already been sent by the daemon
    argon_board.power_off()
