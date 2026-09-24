"""Characterization of the legacy HTTP API as it behaves today.

These tests pin current behaviour so that a later change (gateway, ML-KEM
transport, refactor) that alters it is noticed. They assert properties and
invariants, never exact random values.

Assumption: the harness is the only client of the target while tests run
(true for the harness-started sensor). Other clients reading in between would
skip simulation steps and could break the step-size assertions.
"""

import json
import re
import socket
from urllib.parse import urlsplit

import pytest

from harness.client import SensorTimeout, make_client

MEASUREMENT_PATHS = ["/", "/measurements"]
FIELDS = {"temperatureC", "humidity%"}

# Over HTTP the sensor is always in the COLD state (target 22.0 C / 45.0 %).
# Each step moves 15 % towards the target plus bounded noise (+-0.5 C, +-1.0 %),
# so readings can never leave target +- noise/0.15. Add 0.05 for rounding.
COLD_TEMPERATURE_BAND = (22.0 - 0.5 / 0.15 - 0.05, 22.0 + 0.5 / 0.15 + 0.05)
COLD_HUMIDITY_BAND = (45.0 - 1.0 / 0.15 - 0.05, 45.0 + 1.0 / 0.15 + 0.05)
# Worst-case single COLD step: 0.15 * band half-width + noise (+0.1 rounding).
COLD_MAX_TEMPERATURE_STEP = 0.15 * (0.5 / 0.15) + 0.5 + 0.1
COLD_MAX_HUMIDITY_STEP = 0.15 * (1.0 / 0.15) + 1.0 + 0.1


def read_many(client, count, paths=("/measurements",)):
    readings = []
    for i in range(count):
        response = client.get(paths[i % len(paths)])
        assert response.status == 200
        readings.append(json.loads(response.body))
    return readings


# -- /health -------------------------------------------------------------------

def test_health_returns_200_plain_text_ok(client):
    response = client.get("/health")

    assert response.status == 200
    assert response.headers["content-type"] == "text/plain"
    assert response.body == b"OK"


# -- / and /measurements --------------------------------------------------------

@pytest.mark.parametrize("path", MEASUREMENT_PATHS)
def test_measurement_status_headers_and_length(client, path):
    response = client.get(path)

    assert response.status == 200
    assert response.headers["content-type"] == "application/json"
    assert int(response.headers["content-length"]) == len(response.body)


@pytest.mark.parametrize("path", MEASUREMENT_PATHS)
def test_measurement_has_exactly_two_float_fields(client, path):
    data = json.loads(client.get(path).body)

    assert set(data) == FIELDS
    for key in FIELDS:
        # json.dumps(round(x, 1)) always yields a float literal such as 22.0
        assert type(data[key]) is float, key
        assert data[key] == round(data[key], 1), key


@pytest.mark.parametrize("path", MEASUREMENT_PATHS)
def test_measurement_wire_format(client, path):
    # Pins key order and json.dumps default spacing, e.g.
    # {"temperatureC": 22.3, "humidity%": 44.9}
    body = client.get(path).body.decode("utf-8")

    assert re.fullmatch(r'\{"temperatureC": \d+\.\d, "humidity%": \d+\.\d\}', body), body


def test_measurements_are_within_sensor_limits(client):
    for reading in read_many(client, 30, MEASUREMENT_PATHS):
        assert 20.0 <= reading["temperatureC"] <= 100.0
        assert 10.0 <= reading["humidity%"] <= 80.0


def test_http_readings_always_reflect_the_cold_state(client):
    # CHARACTERIZATION: possibly a bug - the HTTP API has no way to change the
    # sauna state, and the handler's sensor starts in COLD, so over HTTP the
    # sensor only ever reports a cold sauna (~22 C). HEATING/HOT/COOLING are
    # reachable only in-process via SaunaSensor.set_state().
    for reading in read_many(client, 40, MEASUREMENT_PATHS):
        assert COLD_TEMPERATURE_BAND[0] <= reading["temperatureC"] <= COLD_TEMPERATURE_BAND[1]
        assert COLD_HUMIDITY_BAND[0] <= reading["humidity%"] <= COLD_HUMIDITY_BAND[1]


def test_consecutive_readings_change_gradually_across_both_endpoints(client):
    # "/" and "/measurements" step the same simulated sensor, so alternating
    # between them still yields one smooth series.
    readings = read_many(client, 40, MEASUREMENT_PATHS)
    for previous, current in zip(readings, readings[1:]):
        assert abs(current["temperatureC"] - previous["temperatureC"]) <= COLD_MAX_TEMPERATURE_STEP
        assert abs(current["humidity%"] - previous["humidity%"]) <= COLD_MAX_HUMIDITY_STEP


def test_readings_are_not_constant(client):
    readings = read_many(client, 20)
    assert len({(r["temperatureC"], r["humidity%"]) for r in readings}) > 1


# -- other paths and methods ----------------------------------------------------

@pytest.mark.parametrize("path", [
    "/nope", "/measurements/", "/health/", "/MEASUREMENTS", "/measurement", "/health/../health",
])
def test_unknown_paths_return_404_plain_text(client, path):
    response = client.get(path)

    assert response.status == 404
    assert response.headers["content-type"] == "text/plain"
    assert response.body == b"Not Found"


@pytest.mark.parametrize("path, expected_body_start", [
    ("//", b"{"), ("//measurements", b"{"), ("///health", b"OK"),
])
def test_repeated_leading_slashes_are_collapsed(client, path, expected_body_start):
    # Not sensor.py's own logic: http.server rewrites a leading "//" to "/"
    # before do_GET sees the path (stdlib security fix gh-87389, present in
    # every Python this project supports). A non-stdlib server may differ.
    response = client.get(path)

    assert response.status == 200
    assert response.body.startswith(expected_body_start)


@pytest.mark.parametrize("path", ["/measurements?unit=c", "/?x=1", "/health?probe=1"])
def test_query_strings_are_not_ignored_and_return_404(client, path):
    # CHARACTERIZATION: possibly a bug - paths are compared including the
    # query string, so any query parameter turns a valid endpoint into a 404.
    assert client.get(path).status == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@pytest.mark.parametrize("path", ["/", "/measurements", "/health"])
def test_non_get_methods_return_501(client, method, path):
    # 501 (stdlib default for a missing do_<METHOD>) rather than 405.
    response = client.request(method, path)

    assert response.status == 501


def test_head_returns_501_without_body(client):
    # CHARACTERIZATION: possibly a bug - HTTP expects HEAD wherever GET works,
    # but the handler has no do_HEAD, so HEAD is "Unsupported method".
    response = client.request("HEAD", "/health")

    assert response.status == 501
    assert response.body == b""


# -- concurrency ----------------------------------------------------------------

def test_one_idle_connection_blocks_every_other_client(target_url, run_bounded):
    # CHARACTERIZATION: possibly a bug - the server is a single-threaded
    # HTTPServer with no socket timeout. A client that connects and sends
    # nothing stalls all other requests until it disconnects.
    parts = urlsplit(target_url)
    client = make_client(target_url)
    idle = socket.create_connection((parts.hostname, parts.port), timeout=5)
    try:
        blocked = run_bounded(lambda: client.health(timeout=1.0), limit=5)
        assert isinstance(blocked.error, SensorTimeout)
    finally:
        idle.close()

    recovered = run_bounded(lambda: client.health(timeout=5.0), limit=10)
    assert recovered.error is None
    assert recovered.result == "OK"


# -- long run -------------------------------------------------------------------

@pytest.mark.slow
def test_soak_many_readings_keep_all_invariants(client):
    readings = read_many(client, 1500, MEASUREMENT_PATHS)
    for previous, current in zip(readings, readings[1:]):
        assert set(current) == FIELDS
        assert COLD_TEMPERATURE_BAND[0] <= current["temperatureC"] <= COLD_TEMPERATURE_BAND[1]
        assert COLD_HUMIDITY_BAND[0] <= current["humidity%"] <= COLD_HUMIDITY_BAND[1]
        assert abs(current["temperatureC"] - previous["temperatureC"]) <= COLD_MAX_TEMPERATURE_STEP
        assert abs(current["humidity%"] - previous["humidity%"]) <= COLD_MAX_HUMIDITY_STEP
