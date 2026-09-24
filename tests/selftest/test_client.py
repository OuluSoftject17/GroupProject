"""harness.client: Transport interface, validation, HARNESS_TARGET_URL."""

import pytest

import harness.process
from harness.client import (
    TARGET_URL_ENV, HttpTransport, InvalidResponseError, Measurement, Response,
    SensorClient, SensorHTTPError, Transport, open_target, parse_measurement,
)


class FakeTransport(Transport):
    """Stands in for a future transport (e.g. ML-KEM): same interface, no HTTP."""

    def __init__(self, status, body):
        self.status, self.body = status, body
        self.calls = []

    def request(self, method, path, *, headers=None, body=b"", timeout=2.0):
        self.calls.append((method, path, timeout))
        return Response(self.status, "", {}, self.body, 0.0)


def test_sensor_client_works_over_any_transport():
    transport = FakeTransport(200, b'{"temperatureC": 22.5, "humidity%": 44.0}')
    client = SensorClient(transport, timeout=0.7)

    assert client.measurement() == Measurement(22.5, 44.0)
    assert transport.calls == [("GET", "/measurements", 0.7)]


def test_sensor_client_raises_on_non_200():
    client = SensorClient(FakeTransport(404, b"Not Found"))

    with pytest.raises(SensorHTTPError) as info:
        client.health()
    assert info.value.status == 404


def test_parse_measurement_accepts_integers():
    assert parse_measurement(b'{"temperatureC": 22, "humidity%": 45}') == Measurement(22.0, 45.0)


@pytest.mark.parametrize("body", [b"{}", b"null", b'{"temperatureC": false, "humidity%": 1}'])
def test_parse_measurement_rejects_bad_shapes(body):
    with pytest.raises(InvalidResponseError):
        parse_measurement(body)


def test_base_url_path_prefix_is_prepended():
    transport = HttpTransport("http://127.0.0.1:9/gateway/")
    request = transport._encode("GET", "/health", None, b"")

    assert request.startswith(b"GET /gateway/health HTTP/1.1\r\n")
    assert b"\r\nHost: 127.0.0.1:9\r\n" in request


@pytest.mark.parametrize("url", ["http://example.com:8000", "http://10.0.0.5:8000", "http://0.0.0.0:80"])
def test_non_localhost_targets_are_refused(url):
    with pytest.raises(ValueError, match="localhost-only"):
        HttpTransport(url)
    with pytest.raises(ValueError, match="localhost-only"):
        with open_target({TARGET_URL_ENV: url}):
            pass


@pytest.mark.parametrize("url", ["https://127.0.0.1:1", "127.0.0.1:8000"])
def test_only_plain_http_urls_are_accepted(url):
    with pytest.raises(ValueError):
        HttpTransport(url)


def test_target_url_env_is_used_instead_of_starting_the_sensor(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("must not start a sensor when HARNESS_TARGET_URL is set")

    monkeypatch.setattr(harness.process, "SensorProcess", refuse)
    with open_target({TARGET_URL_ENV: "http://127.0.0.1:12345/"}) as url:
        assert url == "http://127.0.0.1:12345"


def test_without_target_url_env_a_sensor_is_started():
    with open_target({}) as url:
        assert SensorClient(HttpTransport(url)).health() == "OK"
