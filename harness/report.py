"""
Measure what the device can do compared with the same sensor unrestricted.

Everything here is measured on the spot; nothing is hard-coded.
"""

import socket
import statistics
import tempfile
import time
import urllib.request
from pathlib import Path

from harness.device import Device, start_sensor
from harness.link import start_link


def measure(url, seconds):
    """Send GET requests one after another for `seconds`."""
    latencies, errors = [], 0
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                response.read()
            latencies.append(time.perf_counter() - start)
        except OSError:
            errors += 1
    return latencies, errors


def run_probe(profile, name, restricted):
    """Run one probe on a fresh device and return its result line."""
    with tempfile.TemporaryDirectory() as tmp, \
            socket.create_server(("127.0.0.1", 0)) as listener:
        args = {
            "outbound_connect": [listener.getsockname()[1]],
            "file_write": [Path(tmp) / "probe.txt"],
            "allocate": [profile["device"]["memory_mb"] * 2],
        }.get(name, [])
        with Device(profile, ["probe", name, *args], restricted) as device:
            result = device.read_result("RESULT ")
            if result is None:
                return f"blocked: device was killed (exit code {device.process.wait()})"
            return result


def benchmark(profile, seconds):
    """Returns the measured rows and the limits the restricted device had."""
    rows, enforced = [], []
    cases = [
        ("unrestricted sensor", False, False),
        ("device, direct", True, False),
        ("device via link", True, True),
    ]
    for label, restricted, via_link in cases:
        device, port = start_sensor(profile, restricted)
        link = start_link(profile["link"], port) if via_link else None
        try:
            target = link.server_address[1] if link else port
            latencies, errors = measure(f"http://127.0.0.1:{target}/measurements", seconds)
            rows.append((label, latencies, errors, device.peak_memory_mb()))
            enforced = enforced or device.enforced
        finally:
            if link:
                link.shutdown()
                link.server_close()
            device.stop()
    return rows, enforced


def print_report(profile, seconds):
    device = profile["device"]
    link = profile["link"]
    print(f"Profile: CPU {device['cpu_percent']}% of one core, "
          f"memory {device['memory_mb']} MB, link {link['bandwidth_kbps']} kbit/s, "
          f"{link['latency_ms']}+/-{link['jitter_ms']} ms, drop {link['drop_rate']:.0%}")
    print(f"Sequential GET /measurements for {seconds} s per row.\n")

    rows, enforced = benchmark(profile, seconds)
    print("Limits in force on this machine:")
    for limit in enforced:
        print(f"  - {limit}")

    print(f"\n{'':22}{'req/s':>8}{'p50 ms':>9}{'p95 ms':>9}{'errors':>8}{'peak MB':>9}")
    for label, latencies, errors, peak in rows:
        if latencies:
            p50 = statistics.median(latencies) * 1000
            p95 = statistics.quantiles(latencies, n=20)[-1] * 1000 if len(latencies) > 1 else p50
        else:
            p50 = p95 = float("nan")
        rate = len(latencies) / seconds
        print(f"{label:22}{rate:8.1f}{p50:9.1f}{p95:9.1f}{errors:8}{peak:9.1f}")

    print("\nCapabilities (each one is attempted, not assumed):")
    for name in ("cpu_work", "outbound_connect", "file_write",
                 "spawn_process", "import_tls", "allocate"):
        print(f"  {name}")
        print(f"    unrestricted: {run_probe(profile, name, restricted=False)}")
        print(f"    device:       {run_probe(profile, name, restricted=True)}")
