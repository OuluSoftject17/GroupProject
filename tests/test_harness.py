"""Checks that the simulated device's restrictions are really in force."""

import http.client
import re
import sys
import urllib.error
import urllib.request

import pytest

from harness.device import _systemd_run_works, load_profile, start_sensor
from harness.link import start_link
from harness.report import run_probe

# The link's delays are switched off so the checks run fast and never flake.
FAST_LINK = {"latency_ms": 0, "jitter_ms": 0, "drop_rate": 0, "bandwidth_kbps": 10_000}


@pytest.fixture
def profile():
    return load_profile(link=FAST_LINK)


@pytest.fixture
def link_port(profile):
    device, device_port = start_sensor(profile)
    link = start_link(profile["link"], device_port)
    yield link.server_address[1]
    link.shutdown()
    link.server_close()
    device.stop()


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
        return response.status, response.read().decode()


def test_endpoints_work_through_link(link_port):
    assert get(link_port, "/health") == (200, "OK")
    for path in ("/", "/measurements"):
        status, body = get(link_port, path)
        assert status == 200
        assert "temperatureC" in body and "humidity%" in body


def status_of(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, headers=headers or {})
    status = connection.getresponse().status
    connection.close()
    return status


def test_link_rejects_everything_else(link_port):
    assert status_of(link_port, "GET", "/admin") == 404
    assert status_of(link_port, "GET", "/measurements?x=1") == 404
    assert status_of(link_port, "POST", "/measurements") == 501
    assert status_of(link_port, "DELETE", "/health") == 501
    assert status_of(link_port, "GET", "/health", {"X-Big": "a" * 2000}) == 413


def test_link_drops_requests():
    profile = load_profile(link={**FAST_LINK, "drop_rate": 1})
    device, device_port = start_sensor(profile)
    link = start_link(profile["link"], device_port)
    try:
        with pytest.raises((urllib.error.URLError, http.client.HTTPException, OSError)):
            get(link.server_address[1], "/health")
    finally:
        link.shutdown()
        link.server_close()
        device.stop()


@pytest.mark.parametrize("probe", [
    "outbound_connect", "file_write", "spawn_process", "import_tls", "allocate",
])
def test_device_lacks_capability(profile, probe):
    # The same probe must work unrestricted, otherwise the check proves nothing.
    assert run_probe(profile, probe, restricted=False).startswith("ok")
    assert run_probe(profile, probe, restricted=True).startswith("blocked")


@pytest.mark.skipif(sys.platform != "win32" and not _systemd_run_works(),
                    reason="no CPU rate cap on this machine")
def test_device_cpu_is_slower(profile):
    def seconds(restricted):
        result = run_probe(profile, "cpu_work", restricted)
        return float(re.search(r"([\d.]+) s", result).group(1))

    # 10 % of one core should be about 10x slower. Allow plenty of slack.
    assert seconds(restricted=True) > 3 * seconds(restricted=False)
