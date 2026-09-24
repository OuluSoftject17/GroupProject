"""The client fails cleanly and never hangs when the connection misbehaves.

"Cleanly" means: a SensorClientError subclass that names the failure, raised
within the client's timeout (+ scheduling slack). Every call is wrapped in
run_bounded(), so a hang fails the test instead of stalling the suite.
"""

import random
import socket
import threading

import pytest

from harness.client import (
    InvalidResponseError, SensorClientError, SensorHTTPError, SensorTimeout,
    TransportError, make_client,
)
from harness.faults import (
    Delay, Drop, FaultProxy, InvalidJson, PassThrough, ServerError, Trickle,
)

TIMEOUT = 1.0
SLACK = 1.0            # generous allowance for slow CI machines / Windows
HANG_LIMIT = TIMEOUT + SLACK + 3.0


@pytest.fixture
def proxy(target_url):
    with FaultProxy(target_url) as p:
        yield p


@pytest.fixture
def proxied(proxy):
    return make_client(proxy.url, timeout=TIMEOUT)


def expect_error(run_bounded, call, error_type):
    outcome = run_bounded(call, limit=HANG_LIMIT)
    assert isinstance(outcome.error, error_type), repr(outcome.error)
    assert outcome.elapsed <= TIMEOUT + SLACK
    return outcome.error


# -- baseline -----------------------------------------------------------------------

def test_pass_through_proxy_is_transparent(proxy, proxied):
    assert proxied.health() == "OK"
    assert proxied.measurement().temperature_c >= 20.0
    assert proxy.requests_seen == 2


# -- delay --------------------------------------------------------------------------

def test_delay_shorter_than_timeout_still_succeeds(proxy, proxied, run_bounded):
    proxy.set_fault(Delay(0.3))
    outcome = run_bounded(proxied.measurement, limit=HANG_LIMIT)

    assert outcome.error is None
    assert outcome.elapsed >= 0.3


@pytest.mark.parametrize("call", ["health", "measurement"])
def test_delay_longer_than_timeout_raises_timeout(proxy, proxied, run_bounded, call):
    proxy.set_fault(Delay(10))
    error = expect_error(run_bounded, getattr(proxied, call), SensorTimeout)

    assert "1.0s" in str(error)


def test_trickled_response_is_cut_off_by_the_overall_deadline(proxy, proxied, run_bounded):
    # Each byte arrives well within a per-read timeout, but the whole response
    # would take ~20s. Only an overall deadline stops this.
    proxy.set_fault(Trickle(chunk_size=1, interval=0.1))
    expect_error(run_bounded, proxied.measurement, SensorTimeout)


def test_slow_but_complete_response_succeeds(proxy, proxied, run_bounded):
    proxy.set_fault(Trickle(chunk_size=64, interval=0.05))
    outcome = run_bounded(proxied.measurement, limit=HANG_LIMIT)

    assert outcome.error is None


# -- dropped connections ----------------------------------------------------------

def test_connection_closed_before_any_response(proxy, proxied, run_bounded):
    proxy.set_fault(Drop(after_bytes=0))
    error = expect_error(run_bounded, proxied.measurement, TransportError)

    assert not isinstance(error, SensorTimeout)  # detected immediately, not by waiting


def test_connection_dropped_at_any_point_never_yields_a_measurement(proxy, proxied, run_bounded):
    full_length = len(_raw_response(proxy.url, "/measurements"))
    cut_points = sorted(set(range(0, full_length, 9)) | {1, full_length - 1})

    for after_bytes in cut_points:
        proxy.set_fault(Drop(after_bytes=after_bytes))
        outcome = run_bounded(proxied.measurement, limit=HANG_LIMIT)
        assert isinstance(outcome.error, SensorClientError), (after_bytes, outcome)
        assert outcome.elapsed <= TIMEOUT + SLACK, after_bytes


def test_connection_refused(run_bounded):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
    client = make_client(f"http://127.0.0.1:{closed_port}", timeout=TIMEOUT)

    # Refused on Linux/macOS; Windows retries the SYN and may time out instead.
    expect_error(run_bounded, client.health, TransportError)


# -- HTTP errors ------------------------------------------------------------------

@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_error_status_raises_http_error(proxy, proxied, run_bounded, status):
    proxy.set_fault(ServerError(status=status, body=b"boom"))
    error = expect_error(run_bounded, proxied.measurement, SensorHTTPError)

    assert error.status == status
    assert error.response.body == b"boom"


def test_health_reports_500_as_http_error(proxy, proxied, run_bounded):
    proxy.set_fault(ServerError())
    error = expect_error(run_bounded, proxied.health, SensorHTTPError)

    assert error.status == 500


def test_raw_get_returns_500_without_raising(proxy, proxied):
    proxy.set_fault(ServerError())

    assert proxied.get("/measurements").status == 500


# -- invalid bodies ---------------------------------------------------------------

@pytest.mark.parametrize("body", [
    b'{"temperatureC": 22.',                              # truncated JSON
    b"not json at all",
    b"",
    b"\xff\xfe\x00garbage",                               # not UTF-8
    b"[22.0, 45.0]",                                      # valid JSON, wrong shape
    b'{"temperatureC": 22.0}',                            # missing field
    b'{"temperatureC": "hot", "humidity%": 45.0}',        # wrong type
    b'{"temperatureC": true, "humidity%": 45.0}',         # bool is not a number
    b'{"temperatureC": NaN, "humidity%": 45.0}',          # Python json accepts NaN
])
def test_invalid_json_raises_invalid_response(proxy, proxied, run_bounded, body):
    proxy.set_fault(InvalidJson(body))
    expect_error(run_bounded, proxied.measurement, InvalidResponseError)


# -- recovery -------------------------------------------------------------------

def test_client_recovers_once_faults_stop(proxy, proxied, run_bounded):
    for fault in [Delay(10), Drop(), ServerError(), InvalidJson(), Trickle(1, 0.1)]:
        proxy.set_fault(fault)
        outcome = run_bounded(proxied.measurement, limit=HANG_LIMIT)
        assert isinstance(outcome.error, SensorClientError), fault

    proxy.set_fault(PassThrough())
    assert proxied.measurement().humidity_percent >= 10.0


@pytest.mark.slow
def test_random_fault_storm_never_hangs_or_leaks_threads(proxy, proxied, run_bounded):
    faults = [PassThrough(), Delay(0.2), Delay(5), Drop(), Drop(30), ServerError(),
              InvalidJson(), Trickle(1, 0.05), Trickle(64, 0.01)]
    rng = random.Random(1234)
    threads_before = threading.active_count()

    for _ in range(150):
        proxy.set_fault(rng.choice(faults))
        outcome = run_bounded(proxied.measurement, limit=HANG_LIMIT)
        assert outcome.error is None or isinstance(outcome.error, SensorClientError)
        assert outcome.elapsed <= TIMEOUT + SLACK

    proxy.set_fault(PassThrough())
    assert proxied.measurement()
    # Proxy handler threads for timed-out requests may still be finishing;
    # they must not accumulate without bound.
    assert threading.active_count() - threads_before < 20


def _raw_response(url, path):
    host, port = url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        chunks = []
        while chunk := s.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)
