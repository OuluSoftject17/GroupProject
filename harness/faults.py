"""Fault-injecting TCP/HTTP proxy that sits between the client and the sensor.

    client  ->  FaultProxy (127.0.0.1:<free port>)  ->  sensor

    with FaultProxy(sensor_url) as proxy:
        proxy.set_fault(Delay(1.5))
        client = make_client(proxy.url)

Faults (applied to every request until changed):

* PassThrough()        - forward the request and relay the response unchanged
* Delay(seconds)       - wait before forwarding
* Drop(after_bytes=0)  - relay only the first N response bytes, then close
                         (0 = close without any response)
* ServerError(status)  - answer with an HTTP error without contacting the sensor
* InvalidJson(body)    - forward, then replace the body with broken JSON
* Trickle(chunk, interval) - relay the response a few bytes at a time

The proxy reads the whole upstream response until the upstream closes the
connection. sensor.py speaks HTTP/1.0 and always closes, so this holds for it.
"""

import re
import socket
import socketserver
import sys
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from harness.client import require_localhost

MAX_REQUEST_HEAD = 64 * 1024
CLIENT_READ_TIMEOUT = 5.0
UPSTREAM_TIMEOUT = 10.0


@dataclass(frozen=True)
class PassThrough:
    pass


@dataclass(frozen=True)
class Delay:
    seconds: float


@dataclass(frozen=True)
class Drop:
    after_bytes: int = 0


@dataclass(frozen=True)
class ServerError:
    status: int = 500
    body: bytes = b"Internal Server Error"


@dataclass(frozen=True)
class InvalidJson:
    body: bytes = b'{"temperatureC": 22.'


@dataclass(frozen=True)
class Trickle:
    chunk_size: int = 1
    interval: float = 0.2


_REASONS = {500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable"}


class FaultProxy:
    def __init__(self, upstream_url, fault=PassThrough()):
        parts = urlsplit(upstream_url)
        require_localhost(parts.hostname or "")
        self.upstream = (parts.hostname, parts.port or 80)
        self.fault = fault
        self.requests_seen = 0
        self.errors = []  # exceptions raised inside handler threads
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def set_fault(self, fault):
        self.fault = fault

    def start(self):
        self._server = _ProxyServer(("127.0.0.1", 0), _ProxyHandler, self)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
            name="fault-proxy", daemon=True)
        self._thread.start()
        return self.url

    def stop(self):
        if self._server is None:
            return
        self._closing.set()  # interrupts Delay / Trickle waits in handler threads
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._server = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc_info):
        self.stop()

    # -- called from handler threads ------------------------------------

    def _handle(self, client):
        fault = self.fault
        request = _read_request(client)
        if request is None:
            return
        with self._lock:
            self.requests_seen += 1

        if isinstance(fault, ServerError):
            client.sendall(_simple_response(fault.status, fault.body))
            return
        if isinstance(fault, Delay) and self._closing.wait(fault.seconds):
            return

        response = self._forward(request)
        if isinstance(fault, InvalidJson):
            response = _replace_body(response, fault.body)

        if isinstance(fault, Drop):
            client.sendall(response[:fault.after_bytes])
        elif isinstance(fault, Trickle):
            for start in range(0, len(response), fault.chunk_size):
                client.sendall(response[start:start + fault.chunk_size])
                if self._closing.wait(fault.interval):
                    return
        else:
            client.sendall(response)

    def _forward(self, request):
        try:
            with socket.create_connection(self.upstream, timeout=UPSTREAM_TIMEOUT) as upstream:
                upstream.sendall(request)
                return _recv_until_closed(upstream)
        except OSError as exc:
            return _simple_response(502, f"upstream error: {exc}".encode())


class _ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, address, handler, proxy):
        self.proxy = proxy
        super().__init__(address, handler)

    def handle_error(self, request, client_address):
        # Clients that time out and hang up are expected in these tests;
        # record instead of printing a traceback to stderr.
        self.proxy.errors.append(sys.exc_info()[1])


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.proxy._handle(self.request)


def _read_request(sock):
    """Read one request head (+ Content-Length body). None if the client went away."""
    sock.settimeout(CLIENT_READ_TIMEOUT)
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk or len(data) > MAX_REQUEST_HEAD:
                return None
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        match = re.search(rb"(?im)^content-length:\s*(\d+)\s*$", head)
        expected = int(match.group(1)) if match else 0
        while len(body) < expected:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            body += chunk
    except OSError:
        return None
    return head + b"\r\n\r\n" + body


def _recv_until_closed(sock):
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _simple_response(status, body):
    reason = _REASONS.get(status, "Error")
    return (f"HTTP/1.0 {status} {reason}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode("latin-1") + body


def _replace_body(response, new_body):
    head, sep, _ = response.partition(b"\r\n\r\n")
    if not sep:
        return response
    head = re.sub(rb"(?im)^content-length:[^\r\n]*", b"Content-Length: %d" % len(new_body), head)
    return head + sep + new_body
