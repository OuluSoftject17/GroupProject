"""harness.process: start on a free port, readiness by polling, always stop."""

import textwrap
import time

import pytest

from harness.client import TransportError, make_client
from harness.process import SensorProcess, SensorStartError


def write_fake_sensor(directory, source):
    (directory / "sensor.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return directory


def test_start_serves_on_a_free_localhost_port_and_stop_frees_it():
    with SensorProcess() as process:
        assert process.base_url.startswith("http://127.0.0.1:")
        assert process.is_running()
        client = make_client(process.base_url, timeout=2.0)
        assert client.health() == "OK"
        url = process.base_url

    assert not process.is_running()
    assert process.returncode is not None
    with pytest.raises(TransportError):
        make_client(url, timeout=1.0).health()


def test_two_processes_get_different_ports():
    with SensorProcess() as a, SensorProcess() as b:
        assert a.port != b.port
        assert make_client(a.base_url).health() == make_client(b.base_url).health() == "OK"


def test_stop_is_idempotent_and_context_manager_stops_on_error():
    process = SensorProcess()
    with pytest.raises(RuntimeError, match="boom"):
        with process:
            raise RuntimeError("boom")

    assert not process.is_running()
    process.stop()  # second stop is a no-op


def test_crash_during_import_is_reported_quickly(tmp_path):
    write_fake_sensor(tmp_path, "raise RuntimeError('sensor exploded')\n")
    process = SensorProcess(sensor_dir=tmp_path, startup_timeout=10)
    started = time.monotonic()

    with pytest.raises(SensorStartError, match="sensor exploded"):
        process.start()

    assert time.monotonic() - started < 5  # detected by exit, not by timeout
    assert not process.is_running()


def test_missing_sensor_module_is_reported(tmp_path):
    with pytest.raises(SensorStartError, match="ModuleNotFoundError"):
        SensorProcess(sensor_dir=tmp_path, startup_timeout=10).start()


def test_process_that_never_reports_a_port_is_killed_after_timeout(tmp_path):
    write_fake_sensor(tmp_path, "import time\ntime.sleep(60)\n")
    process = SensorProcess(sensor_dir=tmp_path, startup_timeout=1.0)
    started = time.monotonic()

    with pytest.raises(SensorStartError, match="did not report its port"):
        process.start()

    assert time.monotonic() - started < 6
    assert process.returncode is not None


def test_unhealthy_server_is_not_considered_ready(tmp_path):
    # Listens and answers, but /health is 503: readiness must check the
    # response, not just that the port accepts connections.
    write_fake_sensor(tmp_path, """
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.end_headers()

            def log_message(self, *args):
                pass

        def create_server(host="localhost", port=8000):
            return HTTPServer((host, port), Handler)
    """)
    process = SensorProcess(sensor_dir=tmp_path, startup_timeout=1.0)

    with pytest.raises(SensorStartError, match="did not answer GET /health"):
        process.start()

    assert process.returncode is not None
