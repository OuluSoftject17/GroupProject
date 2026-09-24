# Sensor test harness

A test harness around the legacy sauna sensor (`sensor.py`). The harness
starts the sensor, talks to it over HTTP, injects faults between the client
and the sensor, and records a performance baseline. It does **not** change
`sensor.py` and does not implement cryptography, a gateway or a cloud service.

## Run commands

Run everything from the repository root.

```sh
poetry install                         # installs pytest (dev dependency)

poetry run pytest -m "not slow"        # fast suite, well under 60 s
poetry run pytest                      # everything, incl. slow soak/storm/30 s baseline (~2 min)
poetry run pytest tests/characterization
poetry run pytest tests/resilience

poetry run python -m unittest test_sensor    # legacy tests, unchanged (also collected by pytest)

poetry run python -m harness.baseline --duration 30 --out baseline.json
#   options: --warmup 2 (discarded seconds first)  --path /measurements  --timeout 2
```

Test against an already-running sensor (or, later, a gateway) instead of
starting one:

```sh
HARNESS_TARGET_URL=http://127.0.0.1:8000 poetry run pytest -m "not slow"     # bash
$env:HARNESS_TARGET_URL = "http://127.0.0.1:8000"; poetry run pytest -m "not slow"   # PowerShell
```

The URL must be a plain `http://` URL on a loopback host (`127.0.0.1`,
`localhost`, `::1`); anything else is refused. Prefer `127.0.0.1` over
`localhost`: on Windows, `localhost` tries IPv6 first, and a refused IPv6
connect costs about 2 s before the IPv4 fallback.

## Layout

| Path | Purpose |
| --- | --- |
| `harness/process.py` | `SensorProcess`: starts sensor.py on a free port, polls `GET /health` until it answers 200 `OK`, always stops the process afterwards (context exit, start-up failure, interpreter exit). |
| `harness/_launch_sensor.py` | Child entry point. It imports `sensor.py` and calls its own `create_server()` with port 0, so the OS picks a free port, then prints `PORT <n>`. (`python sensor.py` itself always binds 8000.) |
| `harness/client.py` | `Transport` interface, `HttpTransport` (plain HTTP), `SensorClient`, and `open_target()`, which honours `HARNESS_TARGET_URL`. |
| `harness/faults.py` | `FaultProxy` plus the faults `PassThrough`, `Delay`, `Drop(after_bytes)`, `ServerError(status)`, `InvalidJson(body)` and `Trickle`. |
| `harness/baseline.py` | Baseline recorder (latency p50/p95/p99, errors, response size, req/s, Python/OS/git info). |
| `tests/characterization/` | Pins current behaviour. Anything that looks wrong is tested as-is and marked `# CHARACTERIZATION: possibly a bug`. |
| `tests/resilience/` | The client fails cleanly and never hangs under every fault. |
| `tests/selftest/` | Tests for the harness itself (process lifecycle, client, baseline). |

## Client error model

Every failure is a `SensorClientError`:

- `TransportError`: refused, reset, closed early, truncated, or not valid HTTP
  - `SensorTimeout`: no complete response before the deadline
- `SensorHTTPError`: a status other than 200 (`.status`, `.response`)
- `InvalidResponseError`: the body is not a JSON object with finite numeric
  `temperatureC` and `humidity%` fields

The timeout is an **overall deadline** for connect + send + receive, not a
per-read socket timeout. A server that trickles one byte at a time therefore
cannot keep a request open indefinitely.

## Adding the ML-KEM transport later

Subclass `harness.client.Transport` and implement
`request(method, path, *, headers, body, timeout) -> Response`. The subclass
must honour `timeout` as an overall deadline and raise only `TransportError` /
`SensorTimeout`. `SensorClient`, the characterization tests and the baseline
then work unchanged. The fixtures currently build clients with `make_client()`
(plain HTTP); that function is the single place where a transport would be
selected.

## Known limitations and risks

### Not tested

- **The `python sensor.py` entry point itself.** Its `__main__` block always
  binds `localhost:8000`, so running it would break the no-hard-coded-ports
  rule. The harness calls the same `create_server()` with host `127.0.0.1`
  and port 0 instead. Not covered: the start-up banner, the `KeyboardInterrupt`
  handling, and binding by the name `localhost`.
- **Sauna states over HTTP.** The API cannot change state, so over HTTP the
  sensor is always COLD. HEATING, HOT and COOLING are only characterized
  in-process.
- **Concurrency and load.** The baseline uses one sequential client with a new
  TCP connection per request. Parallel clients and keep-alive are not measured;
  the server is single-threaded and speaks HTTP/1.0 anyway.
- **Faults not injected:** TCP reset (RST) as opposed to a normal close (`Drop`
  sends FIN), slow request uploads, oversized responses (the client has a 1 MiB
  cap, untested), IPv6, TLS.
- **Fidelity of the fault proxy.** `ServerError` is synthesized by the proxy;
  sensor.py is never made to fail itself. `InvalidJson` keeps the upstream
  headers and swaps the body. The proxy reads the upstream response until the
  upstream closes the connection. That is true for sensor.py (HTTP/1.0), but a
  keep-alive upstream (for example a future gateway) would stall each proxied
  request for up to 10 s, so the proxy would need updating first.
- **Server-side resilience.** Apart from the idle-connection characterization,
  nothing checks how sensor.py copes with misbehaving clients.
- **Platforms and Python versions.** Verified on Windows 11 with CPython 3.14
  only. The POSIX paths (`terminate()` = SIGTERM) and Python 3.12/3.13 have not
  been run.

### Possibly flaky

- **Timing assertions.** Resilience tests require failures within the timeout
  (1 s) plus 1 s of slack, and `Delay(0.3)` must succeed under a 1 s timeout.
  A heavily loaded CI runner could exceed these. Raise `SLACK` in
  `tests/resilience/test_client_faults.py` if needed.
- **The idle-connection test** depends on a 1 s client timeout while one
  connection is held idle.
- **Legacy `test_sensor.py`** uses a fixed port (8765) and fails if something
  else holds it. It also takes about 2 s per HTTP test on Windows because of
  the `localhost` IPv6 fallback. It cannot be changed under the rules.
- **Connection-refused test.** It binds and releases a port, then connects to
  it. Another process could grab the port in between (unlikely). On Windows the
  test usually times out rather than being refused; both count as
  `TransportError`.
- **Step-size characterization** assumes the harness is the only client of the
  target. Another client reading from a shared `HARNESS_TARGET_URL` sensor
  skips simulation steps and can break those assertions.
- **The fault-storm thread-count check** (slow test) is a heuristic.
- **Baseline numbers are machine-specific.** On Windows, p50 sits around
  15.5 ms while the minimum is around 0.4 ms, which looks like the 15.6 ms
  Windows timer tick (cause not confirmed). Only compare baselines from the
  same machine and OS, and check `git_dirty`.
- **Orphaned sensor process.** If the test process is killed hard (Task
  Manager, SIGKILL), the sensor child survives: there is no Windows job object
  or process group. Normal exits and Ctrl+C are handled.

### Assumptions to verify

- Calling `create_server(port=0)` through the launcher is an acceptable stand-in
  for `python sensor.py`.
- "Localhost only" should be enforced even for `HARNESS_TARGET_URL`. If the
  gateway will run on another host or in a container, relax `require_localhost`
  in `harness/client.py`.
- `pyproject.toml` now sets `package-mode = false`. Without it, `poetry install`
  fails because there is no `legacy_sensor` package. Poetry 2.5.1 wrote pytest
  as a PEP 735 `[dependency-groups]` entry, which older Poetry releases may not
  read, so check that everyone uses a recent Poetry.
- Decide which `# CHARACTERIZATION: possibly a bug` items are intended:
  - The HTTP API is COLD-only.
  - Query strings return 404.
  - HEAD returns 501.
  - One idle connection blocks every other client.
  - One sensor is shared at class level across all servers.
  - Port 8000 is hard-coded.
  - Humidity steps are not clamped.

  Intended items become requirements. Real bugs should get a fix plus a
  deliberate update of the characterization test.
- The `//` → `/` collapsing is Python stdlib behaviour (gh-87389), not
  sensor.py's. A non-stdlib replacement server may behave differently.
