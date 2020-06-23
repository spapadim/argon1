# Based on https://gist.github.com/grantjenks/095de18c51fa8f118b68be80a624c45a

import socket
import os
from http.client import HTTPConnection
from socketserver import UnixStreamServer

from xmlrpc.client import Transport, ServerProxy
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCDispatcher

__all__ = ['UnixServerProxy', 'UnixXMLRPCServer']


##########################################################################
# Client

class UnixHTTPConnection(HTTPConnection):
  def connect(self):
    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    self.sock.connect(self.host)


class UnixTransport(Transport, object):
  def __init__(self, socket_path):
    self.socket_path = socket_path
    super().__init__()

  def make_connection(self, host):
    return UnixHTTPConnection(self.socket_path)


class UnixServerProxy(ServerProxy):
  def __init__(self, addr, **kwargs):
    super().__init__("http://", transport=UnixTransport(addr), **kwargs)


##########################################################################
# Server

class UnixXMLRPCRequestHandler(SimpleXMLRPCRequestHandler):
  disable_nagle_algorithm = False

  def address_string(self):
    return self.client_address


class UnixXMLRPCServer(UnixStreamServer, SimpleXMLRPCDispatcher):
  def __init__(self, addr, socket_permissions=0o755,
               log_requests=True, allow_none=True, encoding=None,
               bind_and_activate=True, use_builtin_types=True):
    self.logRequests = log_requests
    self.socket_permissions = socket_permissions
    SimpleXMLRPCDispatcher.__init__(self, allow_none, encoding, use_builtin_types)
    UnixStreamServer.__init__(self, addr, UnixXMLRPCRequestHandler, bind_and_activate)

  def server_bind(self):
    super().server_bind()
    os.chmod(self.server_address, self.socket_permissions)
