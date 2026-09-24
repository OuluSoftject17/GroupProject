"""Start sensor.py in a child process on a free localhost port and stop it again.

Usage:

    with SensorProcess() as sensor:
        print(sensor.base_url)   # e.g. http://127.0.0.1:53817

Readiness is detected by polling GET /health (no fixed sleeps). The child is
always stopped: on context exit, on start-up failure, and at interpreter exit
as a last resort. Works on Windows and POSIX (terminate() then kill()).
"""

import atexit
import collections
import http.client
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from harness._launch_sensor import PORT_LINE_PREFIX

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = Path(__file__).resolve().parent / "_launch_sensor.py"

_live_processes = set()


class SensorStartError(RuntimeError):
    """The sensor process did not become healthy in time (or crashed)."""


class SensorProcess:
    def __init__(self, sensor_dir=REPO_ROOT, host="127.0.0.1",
                 startup_timeout=10.0, python=sys.executable):
        self.sensor_dir = Path(sensor_dir)
        self.host = host
        self.startup_timeout = startup_timeout
        self.python = python

        self.port = None
        self.returncode = None  # exit code of the last stopped child
        self._proc = None
        self._reader = None
        self._output = collections.deque(maxlen=50)
        self._port_found = threading.Event()

    @property
    def base_url(self):
        if self.port is None:
            raise RuntimeError("sensor process is not running")
        return f"http://{self.host}:{self.port}"

    @property
    def pid(self):
        return self._proc.pid if self._proc else None

    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def start(self):
        if self._proc is not None:
            raise RuntimeError("sensor process already started")

        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self._proc = subprocess.Popen(
            [self.python, str(LAUNCHER),
             "--sensor-dir", str(self.sensor_dir), "--host", self.host],
            cwd=self.sensor_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            # stderr is merged and continuously drained by the reader thread,
            # so the child can never block on a full pipe buffer.
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _live_processes.add(self)
        self._reader = threading.Thread(
            target=self._read_output, name="sensor-output", daemon=True)
        self._reader.start()

        try:
            deadline = time.monotonic() + self.startup_timeout
            self._wait_for_port(deadline)
            self._wait_for_health(deadline)
        except BaseException:
            self.stop()
            raise
        return self.base_url

    def stop(self):
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        finally:
            if self._reader is not None:
                self._reader.join(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
            _live_processes.discard(self)
            self.returncode = proc.returncode
            self._proc = None
            self.port = None

    def output(self):
        """Last lines the child wrote to stdout/stderr (for diagnostics)."""
        return "".join(self._output)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc_info):
        self.stop()

    def _read_output(self):
        for line in self._proc.stdout:
            self._output.append(line)
            if self.port is None and line.startswith(PORT_LINE_PREFIX):
                self.port = int(line[len(PORT_LINE_PREFIX):])
                self._port_found.set()
        # EOF: the child exited. Wake up anyone waiting for the port.
        self._port_found.set()

    def _wait_for_port(self, deadline):
        self._port_found.wait(max(0.0, deadline - time.monotonic()))
        if self.port is None:
            self._fail("did not report its port")

    def _wait_for_health(self, deadline):
        interval = 0.01
        while True:
            if self._health_ok():
                return
            if time.monotonic() >= deadline:
                self._fail("did not answer GET /health with 200 OK")
            # Waiting on the process doubles as crash detection: it returns
            # early if the child exits, otherwise it times out and we re-poll.
            try:
                self._proc.wait(timeout=min(interval, max(0.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
            else:
                self._fail("exited during start-up")
            interval = min(interval * 2, 0.2)

    def _health_ok(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=0.5)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            return response.status == 200 and response.read() == b"OK"
        except (OSError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def _fail(self, reason):
        exit_code = self._proc.poll() if self._proc else None
        raise SensorStartError(
            f"sensor process {reason} within {self.startup_timeout}s "
            f"(exit code: {exit_code}). Output:\n{self.output()}")


@atexit.register
def _stop_leftover_processes():
    for process in list(_live_processes):
        process.stop()
