"""Record a performance baseline for the sensor HTTP API.

    python -m harness.baseline --duration 30 --out baseline.json

A single client sends sequential GET requests (one at a time: sensor.py is a
single-threaded server, so concurrency would mostly measure queueing). The
first --warmup seconds are discarded; the following --duration seconds are
measured. Uses HARNESS_TARGET_URL if set, otherwise starts sensor.py.
"""

import argparse
import datetime
import json
import math
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from harness.client import (
    DEFAULT_TIMEOUT, TARGET_URL_ENV, SensorClientError, SensorHTTPError,
    make_client, open_target, parse_measurement,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1


def percentile(sorted_values, p):
    """Nearest-rank percentile (no interpolation) of an ascending list."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def measure(base_url, duration, warmup, path="/measurements", timeout=DEFAULT_TIMEOUT):
    client = make_client(base_url, timeout=timeout)
    latencies, sizes, errors = [], [], Counter()
    requests = 0

    def one_request():
        started = time.perf_counter()
        try:
            response = client.get(path)
            if response.status != 200:
                raise SensorHTTPError(response)
            parse_measurement(response.body)
        except SensorClientError as exc:
            return None, None, type(exc).__name__
        return time.perf_counter() - started, len(response.body), None

    warmup_end = time.perf_counter() + warmup
    while time.perf_counter() < warmup_end:
        one_request()

    measure_start = time.perf_counter()
    measure_end = measure_start + duration
    while time.perf_counter() < measure_end:
        latency, size, error = one_request()
        requests += 1
        if error:
            errors[error] += 1
        else:
            latencies.append(latency)
            sizes.append(size)
    elapsed = time.perf_counter() - measure_start

    latencies.sort()
    ms = lambda seconds: None if seconds is None else round(seconds * 1000, 3)
    return {
        "requests": requests,
        "successful": len(latencies),
        "errors": {"total": sum(errors.values()), "by_type": dict(errors),
                   "rate": round(sum(errors.values()) / requests, 6) if requests else None},
        "measured_seconds": round(elapsed, 3),
        "requests_per_second": round(requests / elapsed, 2) if elapsed else None,
        "latency_ms": {
            "p50": ms(percentile(latencies, 50)),
            "p95": ms(percentile(latencies, 95)),
            "p99": ms(percentile(latencies, 99)),
            "min": ms(latencies[0] if latencies else None),
            "max": ms(latencies[-1] if latencies else None),
            "mean": ms(sum(latencies) / len(latencies) if latencies else None),
        },
        "response_bytes": {
            "min": min(sizes) if sizes else None,
            "max": max(sizes) if sizes else None,
            "mean": round(sum(sizes) / len(sizes), 2) if sizes else None,
        },
    }


def _git(*args):
    try:
        result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                                text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def environment():
    status = _git("status", "--porcelain")
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": None if status is None else bool(status),
    }


def run(duration, warmup, path, timeout, environ=None):
    environ = os.environ if environ is None else environ
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with open_target(environ) as base_url:
        results = measure(base_url, duration, warmup, path, timeout)
    return {
        "schema_version": SCHEMA_VERSION,
        "started_at_utc": started_at,
        "config": {
            "duration_s": duration, "warmup_s": warmup, "path": path,
            "timeout_s": timeout, "concurrency": 1, "connection": "new TCP connection per request",
            "target": base_url,
            "target_source": TARGET_URL_ENV if environ.get(TARGET_URL_ENV, "").strip() else "harness-started sensor.py",
        },
        "environment": environment(),
        "results": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--duration", type=float, default=30.0, help="measured seconds (default 30)")
    parser.add_argument("--warmup", type=float, default=2.0, help="discarded seconds before measuring (default 2)")
    parser.add_argument("--path", default="/measurements", help="endpoint to request")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-request timeout in seconds")
    parser.add_argument("--out", type=Path, default=Path("baseline.json"), help="output JSON file")
    args = parser.parse_args(argv)
    if args.duration <= 0 or args.warmup < 0:
        parser.error("--duration must be > 0 and --warmup >= 0")

    report = run(args.duration, args.warmup, args.path, args.timeout)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    r = report["results"]
    print(f"{r['requests']} requests in {r['measured_seconds']}s "
          f"({r['requests_per_second']} req/s), errors: {r['errors']['total']}")
    print("latency ms p50/p95/p99: {p50} / {p95} / {p99}".format(**r["latency_ms"]))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
