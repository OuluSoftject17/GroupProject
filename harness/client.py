"""Small stdlib HTTP client for the sauna sensor.

Layers:

* `Transport` - moves one request/response pair. `HttpTransport` is plain
  HTTP; an ML-KEM transport can be added later by implementing the same
  interface (same method, same `Response`, same exception types).
* `SensorClient` - knows the sensor API (/health, /, /measurements) and
  validates responses.
* `open_target()` - yields the base URL to test: `HARNESS_TARGET_URL` if set,
  otherwise a freshly started sensor.py process.

Every failure surfaces as a `SensorClientError` subclass, and every request is
bounded by a single overall deadline, so callers never hang.
"""

import http.client
import io
import ipaddress
import json
import math
import os
import socket
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

TARGET_URL_ENV = "HARNESS_TARGET_URL"
DEFAULT_TIMEOUT = 2.0
MAX_RESPONSE_BYTES = 1024 * 1024


class SensorClientError(Exception):
    """Base class for every error the client raises."""


class TransportError(SensorClientError):
    """Connection refused, reset, closed early, or an unparseable HTTP response."""


class SensorTimeout(TransportError):
    """The request did not complete before its deadline."""


class SensorHTTPError(SensorClientError):
    """The sensor answered with an unexpected HTTP status."""

    def __init__(self, response):
        super().__init__(f"unexpected HTTP status {response.status} {response.reason}")
        self.response = response
        self.status = response.status


class InvalidResponseError(SensorClientError):
    """The response body was not the expected JSON measurement."""


@dataclass(frozen=True)
class Response:
    status: int
    reason: str
    headers: dict = field(repr=False)  # lower-cased header names
    body: bytes = field(repr=False)
    elapsed: float                     # seconds, connect to last byte


@dataclass(frozen=True)
class Measurement:
    temperature_c: float
    humidity_percent: float


class Transport(ABC):
    """Sends one request and returns the complete response.

    Implementations must honour `timeout` as an overall deadline and raise
    only `TransportError` / `SensorTimeout` on failure.
    """

    @abstractmethod
    def request(self, method, path, *, headers=None, body=b"",
                timeout=DEFAULT_TIMEOUT):
        """Return a `Response` for `method path`."""

    def close(self):
        """Release resources. Plain HTTP holds none between requests."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class HttpTransport(Transport):
    """Plain HTTP/1.1 over a new TCP connection per request (`Connection: close`)."""

    def __init__(self, base_url, max_response_bytes=MAX_RESPONSE_BYTES):
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(f"expected an http://host:port URL, got {base_url!r}")
        require_localhost(parts.hostname)
        self.base_url = base_url
        self.host = parts.hostname
        self.port = parts.port or 80
        self.path_prefix = parts.path.rstrip("/")
        self.max_response_bytes = max_response_bytes
        host = f"[{self.host}]" if ":" in self.host else self.host
        self._host_header = f"{host}:{self.port}"

    def request(self, method, path, *, headers=None, body=b"",
                timeout=DEFAULT_TIMEOUT):
        if not path.startswith("/"):
            raise ValueError(f"path must start with '/', got {path!r}")
        started = time.perf_counter()
        deadline = time.monotonic() + timeout
        try:
            raw = self._exchange(self._encode(method, path, headers, body), deadline)
            status, reason, response_headers, response_body = _parse_response(raw, method)
        except TimeoutError as exc:
            raise SensorTimeout(f"{method} {path}: no complete response within {timeout}s") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
        return Response(status, reason, response_headers, response_body,
                        time.perf_counter() - started)

    def _encode(self, method, path, headers, body):
        lines = [
            f"{method} {self.path_prefix}{path} HTTP/1.1",
            f"Host: {self._host_header}",
            "Connection: close",
            "User-Agent: sensor-harness",
            "Accept: */*",
        ]
        if body or method in ("POST", "PUT", "PATCH"):
            lines.append(f"Content-Length: {len(body)}")
        lines.extend(f"{name}: {value}" for name, value in (headers or {}).items())
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body

    def _exchange(self, request_bytes, deadline):
        def remaining():
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("deadline exceeded")
            return left

        # Re-arming the socket timeout with the time left before every call
        # turns per-operation timeouts into one overall deadline, so a server
        # that trickles bytes cannot keep the request alive forever.
        with socket.create_connection((self.host, self.port), timeout=remaining()) as sock:
            sock.settimeout(remaining())
            sock.sendall(request_bytes)
            chunks, size = [], 0
            while True:
                sock.settimeout(remaining())
                chunk = sock.recv(65536)
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise http.client.HTTPException(
                        f"response larger than {self.max_response_bytes} bytes")
                chunks.append(chunk)


class _BytesSocket:
    """Lets http.client.HTTPResponse parse a response we already received."""

    def __init__(self, data):
        self._data = data

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self._data)


def _parse_response(raw, method):
    response = http.client.HTTPResponse(_BytesSocket(raw), method=method)
    response.begin()     # RemoteDisconnected if the connection closed with no reply
    body = response.read()  # IncompleteRead if shorter than Content-Length
    headers = {name.lower(): value for name, value in response.getheaders()}
    return response.status, response.reason, headers, body


class SensorClient:
    def __init__(self, transport, timeout=DEFAULT_TIMEOUT):
        self.transport = transport
        self.timeout = timeout

    def get(self, path, *, timeout=None):
        """Raw GET: returns any HTTP status without judging it."""
        return self.request("GET", path, timeout=timeout)

    def request(self, method, path, *, headers=None, body=b"", timeout=None):
        return self.transport.request(
            method, path, headers=headers, body=body,
            timeout=self.timeout if timeout is None else timeout)

    def health(self, *, timeout=None):
        response = self._get_ok("/health", timeout)
        return response.body.decode("utf-8", errors="replace")

    def measurement(self, path="/measurements", *, timeout=None):
        response = self._get_ok(path, timeout)
        return parse_measurement(response.body)

    def _get_ok(self, path, timeout):
        response = self.get(path, timeout=timeout)
        if response.status != 200:
            raise SensorHTTPError(response)
        return response

    def close(self):
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def parse_measurement(body):
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidResponseError(f"body is not valid JSON: {body[:80]!r}") from exc
    if not isinstance(data, dict):
        raise InvalidResponseError(f"expected a JSON object, got {type(data).__name__}")
    values = []
    for key in ("temperatureC", "humidity%"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise InvalidResponseError(f"{key!r} is missing or not a finite number: {value!r}")
        values.append(float(value))
    return Measurement(*values)


def require_localhost(hostname):
    """The harness only ever talks to this machine."""
    if hostname == "localhost":
        return
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError(f"harness is localhost-only; refusing to talk to {hostname!r}")


def make_client(base_url, timeout=DEFAULT_TIMEOUT):
    return SensorClient(HttpTransport(base_url), timeout=timeout)


@contextmanager
def open_target(environ=None):
    """Yield the base URL under test.

    If HARNESS_TARGET_URL is set, it is used as-is (nothing is started or
    stopped). Otherwise sensor.py is started on a free port and stopped on exit.
    """
    environ = os.environ if environ is None else environ
    url = environ.get(TARGET_URL_ENV, "").strip()
    if url:
        require_localhost(urlsplit(url).hostname or "")
        yield url.rstrip("/")
        return

    from harness.process import SensorProcess  # local import: process.py is optional here

    with SensorProcess() as sensor:
        yield sensor.base_url
