# (c) 2020- Spiros Papadimitriou <spapadim@gmail.com>
#
# This file is released under the MIT License:
#    https://opensource.org/licenses/MIT
# This software is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.

import sys
from . import ArgonDaemon, daemon_client


def _error_message(msg: str) -> None:
  print(msg, file=sys.stderr)


def _error_exit(error_msg: str, exit_status: int = 1) -> None:
  _error_message(error_msg)
  sys.exit(exit_status)


_argonctl_cmd_aliases = {
  'temperature': 'get_temperature',
  'get_temp': 'get_temperature', 'temp': 'get_temperature',
  'get_speed': 'get_fan_speed', 'speed': 'get_fan_speed',
  'set_speed': 'set_fan_speed',
  'pause_fan': 'disable_fan_control', 'pause': 'disable_fan_control',
  'resume_fan': 'enable_fan_control', 'resume': 'enable_fan_control',
  'fan_status': 'is_fan_control_enabled',
  'power_status': 'is_power_control_enabled',
}

def argonctl_main() -> None:  # noqa: E302
  # Check and parse arguments
  if len(sys.argv) < 2:
    _error_exit("Command argument is missing")
  cmd_name = sys.argv[1]
  cmd_name = _argonctl_cmd_aliases.get(cmd_name, cmd_name)
  if cmd_name == 'set_fan_speed':
    if len(sys.argv) != 3:
      _error_exit("set_fan_speed requires an integer argument")
    arg_val = int(sys.argv[2])
  else:
    if len(sys.argv) != 2:
      _error_exit("Too many anrguments!")
  # Make RPC call and print any result
  with daemon_client() as daemon:
    func = getattr(daemon, cmd_name)
    retval = func(arg_val) if cmd_name == 'set_fan_speed' else func()
    if retval is not None:
      print(retval)


def argondaemon_main() -> None:
  import logging
  logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%m/%d/%Y %H:%M:%S', level=logging.INFO)
  daemon = ArgonDaemon()
  try:
    daemon.start()
    daemon.wait()
  finally:
    daemon.close()
