# Simulated device harness

The harness runs the legacy sauna sensor (`sensor.py`) as if it were a cheap
embedded controller: a slow CPU, little memory and a stripped-down runtime.
It does not change `sensor.py`.

It is plain Python run through Poetry. The group chose not to use Docker or VMs.

## Running it

From the repository root:

```sh
poetry install

poetry run python -m harness up              # start the device and its link
poetry run python -m harness up --port 8080  # same, with a fixed link port
poetry run python -m harness report          # measure the device (about 30 s)
poetry run pytest                            # prove the restrictions are in force
```

`up` prints the link address and the limits in force. Use the sensor through
that address only. Ctrl+C stops everything.

## How it fits together

```
 client ──HTTP──▶  link  ──HTTP──▶  device
 (you)          127.0.0.1:<port>   127.0.0.1:<internal port>
                 - GET /health, /, /measurements only
                                     - sensor.py in its own process
                                     - OS limits: CPU, memory, one process
                                     - sandbox: modules, disk, network
```

## The profile

All values live in `profile.toml`. The device imitates an embedded-Linux sauna
controller.

| Setting | Value | Imitates |
| --- | --- | --- |
| `cpu_percent` | 10 | 10 % of one desktop core, roughly a single-core ARM at a few hundred MHz |
| `memory_mb` | 32 | a 64 MB controller with about half left for the application |
| `allowed_modules` | list | firmware that ships only what the sensor needs (no `ssl`, `hashlib`, `subprocess`, `ctypes`, ...) |
| `max_request_bytes`, `max_response_bytes` | 1024 | the real API needs far less |

## What is enforced, and how

| Limit | Windows | Linux |
| --- | --- | --- |
| CPU rate | **OS**: Job Object hard cap | **OS** if `systemd-run --user` works (cgroup `CPUQuota`), otherwise **not capped** |
| Memory | **OS**: Job Object commit limit | **OS**: `setrlimit(RLIMIT_DATA)`, plus cgroup `MemoryMax` with systemd |
| Only one process | **OS**: Job Object | emulated (audit hook) |
| Device dies with the harness | **OS**: Job Object kill-on-close | **OS**: `PR_SET_PDEATHSIG` |
| Module allowlist | emulated (audit hook) | emulated (audit hook) |
| No disk writes | emulated (audit hook) | emulated (audit hook) |
| No outbound connections or DNS | emulated (audit hook) | emulated (audit hook) |
| Only 3 endpoints reachable | link process | link process |

"Emulated" means a Python audit hook (`sys.addaudithook`) inside the device
process refuses the action. **This is a simulation, not a security boundary.**
Code running in the device could get around it. It is there to model what
the device lacks, not to stop an attacker.

`up` and `report` both print the list of limits that were actually applied on
the current machine.

## The report

`report` measures, on the spot:

- Requests per second, median and 95th-percentile latency, errors and peak
  memory for three setups: the sensor unrestricted, the device reached
  directly, and the device reached through the link.
- A set of probes. Each one is tried on an unrestricted process and on the
  device: a fixed CPU task (timed), an outbound connection, a file write,
  starting a process, importing `ssl`, and allocating twice the memory cap.

