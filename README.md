# GroupProject

Group project for the **Software Development, Maintenance and Operations** course.

The project starts with a legacy sauna sensor system running on simulated limited hardware. The system is then extended with a Webedge component that provides an encrypted communication layer between clients and the sensor.

The project uses **LLM-assisted development and human review** as required by the course.

---

## Table of Contents

* [Project Overview](#project-overview)
* [Architecture](#architecture)
* [Requirements](#requirements)
* [Installation](#installation)
* [Legacy Sauna Sensor](#legacy-sauna-sensor)
* [Running the Sensor](#running-the-sensor)
* [Sensor API](#sensor-api)
* [Constrained Device Harness](#constrained-device-harness)
* [Running the Harness](#running-the-harness)
* [Harness Tests](#harness-tests)
* [Performance Report](#performance-report)
* [Webedge](#webedge)
* [Webedge Security](#webedge-security)
* [Webedge Configuration](#webedge-configuration)
* [Webedge Tests](#webedge-tests)
* [Project Structure](#project-structure)
* [Testing](#testing)
* [Development Rules](#development-rules)
* [Current Status](#current-status)

---

## Project Overview

The project represents a simplified legacy sauna monitoring system.

The initial system contains a simulated sensor that produces temperature and relative-humidity measurements. The sensor exposes these measurements through a small HTTP API.

The project also includes a **constrained-device harness** that simulates the limitations of an embedded sauna controller, including restricted CPU, memory, modules, filesystem access, networking and process creation.

A separate **Webedge** component provides an RSA-encrypted communication layer between a client and the sensor or constrained device.

The long-term project goal is to modernize the legacy communication system using modern cryptography, including ML-KEM as part of the later modernization work. The current Webedge implementation uses **RSA-OAEP**.

---

## Architecture

### Legacy sensor

```text
Client
   |
   | HTTP
   v
sensor.py
   |
   +-- simulated temperature
   |
   +-- simulated humidity
```

The sensor does not use physical hardware. Measurements are generated randomly within the configured ranges.

### Constrained device

```text
Client
   |
   | HTTP
   v
+----------------------+
|       Link           |
|  localhost:<port>   |
+----------+-----------+
           |
           | HTTP
           v
+----------------------+
|  Simulated Device    |
|                      |
| CPU / Memory limits  |
| Module restrictions  |
| Read-only filesystem |
| No outbound network  |
| One process          |
+----------+-----------+
           |
           v
       sensor.py
```

The harness runs `sensor.py` in a separate process and applies the simulated hardware restrictions.

### Webedge

```text
                 RSA-OAEP
Client  <======================>  Webedge
                                     |
                                     | HTTP
                                     v
                              Sensor / Device
```

The client-to-Webedge communication is encrypted with RSA-OAEP.

The Webedge-to-target communication uses normal HTTP.

The response from the target is encrypted by Webedge using the client's public key before it is returned.

---

## Requirements

* Python **3.12 or newer**
* Poetry
* Git

The project uses Poetry for dependency management and running commands.

---

## Installation

Clone the repository and enter the project directory:

```bash
git clone <repository-url>
cd GroupProject
```

Install the project dependencies:

```bash
poetry install
```

Activate the Poetry environment if required:

```bash
poetry shell
```

Alternatively, commands can be executed directly through Poetry:

```bash
poetry run <command>
```

For example:

```bash
poetry run pytest
```

The repository is configured with:

```toml
package-mode = false
```

because this project is an application/project repository rather than an installable Python package.

---

# Legacy Sauna Sensor

The legacy sensor is implemented in:

```text
sensor.py
```

It contains two main components:

* `SaunaSensor`
* `SaunaRequestHandler`

`SaunaSensor` generates simulated measurements.

The current implementation does not use predefined sauna states. Instead, every reading is generated using random values within the configured limits.

### Measurement ranges

| Measurement       |         Range |
| ----------------- | ------------: |
| Temperature       | 20.0–100.0 °C |
| Relative humidity |   10.0–80.0 % |

Measurements are rounded to one decimal place.

---

## Running the Sensor

Start the legacy sensor with:

```bash
poetry run python sensor.py
```

By default, the server starts at:

```text
http://localhost:8000
```

The server prints the available endpoints when it starts.

Press `Ctrl+C` to stop it.

---

## Sensor API

### `GET /health`

Checks whether the sensor is running.

Response:

```text
OK
```

HTTP status:

```text
200
```

---

### `GET /`

Returns a simulated measurement.

Example:

```json
{
  "temperatureC": 72.4,
  "humidity%": 46.8
}
```

---

### `GET /measurements`

Returns a simulated measurement using the same format as `/`.

Example:

```json
{
  "temperatureC": 61.2,
  "humidity%": 52.7
}
```

---

### Unknown endpoints

Unknown paths return:

```text
404 Not Found
```

---

# Constrained Device Harness

The simulated constrained device is implemented under:

```text
harness/
```

The harness runs the legacy sensor as if it were installed on a small embedded controller.

The harness does **not modify `sensor.py`**.

Instead, it runs the sensor in a separate process and applies restrictions around it.

The simulated device profile is defined in:

```text
harness/profile.toml
```

---

## Device Profile

The current profile simulates a small embedded-Linux sauna controller.

| Setting               |                Value |
| --------------------- | -------------------: |
| CPU                   | 10% of one host core |
| Memory                |                32 MB |
| Maximum request size  |           1024 bytes |
| Maximum response size |           1024 bytes |
| Device timeout        |            5 seconds |
| Network               |        Loopback only |
| Processes             |                  One |
| Filesystem            |            Read-only |
| Runtime modules       | Restricted allowlist |

The exact restrictions that can be enforced depend on the operating system.

---

## Platform Restrictions

### Windows

Windows uses a **Job Object** to enforce:

* CPU hard limit
* Memory limit
* One active process
* Kill-on-job-close behavior

Peak working-set memory is also measured.

### Linux

Linux uses operating-system mechanisms where available:

* Memory restriction using `setrlimit`
* CPU restriction using `systemd-run --user` and cgroups when available
* Parent-death handling so the device terminates with the harness

If `systemd-run --user` is unavailable, the harness reports that the CPU rate is not capped.

### Runtime restrictions

The device also uses a Python audit hook to simulate restrictions on:

* importing unavailable modules
* writing to the filesystem
* outbound network connections
* DNS resolution
* starting additional processes
* listening outside loopback

These audit-hook restrictions are an **emulation**, not a security boundary.

---

# Running the Harness

Start the constrained device and link:

```bash
poetry run python -m harness up
```

The harness selects an available localhost port for the link.

A fixed port can also be selected:

```bash
poetry run python -m harness up --port 8080
```

The command prints:

* the address of the link
* the limits currently enforced
* instructions for stopping the device

Press `Ctrl+C` to stop the harness.

---

## How the Link Works

The link is the only external access point to the simulated device.

It accepts only:

```text
GET /health
GET /
GET /measurements
```

Other paths are rejected.

The link also enforces the configured request and response size limits.

For example:

```text
Client
   |
   | GET /measurements
   v
Link
   |
   | GET /measurements
   v
Device
   |
   v
sensor.py
```

---

# Harness Tests

The harness restrictions are tested in:

```text
tests/test_harness.py
```

Run the harness tests with:

```bash
poetry run pytest
```

The tests verify that:

### API access works

* `/health`
* `/`
* `/measurements`

work through the link.

### Unsupported requests are rejected

The tests verify rejection of:

* unsupported paths
* query-string variants
* unsupported HTTP methods
* oversized requests

### Device restrictions work

The tests check that the restricted device cannot:

* make outbound connections
* write files
* create another process
* import TLS/`ssl`
* allocate more memory than allowed

Each capability is first tested on an unrestricted process so that the restriction test has a meaningful baseline.

### CPU restriction

When a CPU rate cap is available, the test compares unrestricted and restricted CPU workloads.

The current test expects the restricted workload to take more than three times as long as the unrestricted workload.

---

# Performance Report

The harness includes a runtime performance report:

```text
harness/report.py
```

Run it with:

```bash
poetry run python -m harness report
```

The default measurement duration is 5 seconds per configuration.

A different duration can be specified:

```bash
poetry run python -m harness report --seconds 10
```

The report compares:

1. Unrestricted sensor
2. Restricted device directly
3. Restricted device through the link

It measures:

* requests per second
* p50 latency
* p95 latency
* errors
* peak memory

Example report structure:

```text
                         req/s   p50 ms   p95 ms   errors   peak MB
unrestricted sensor
device, direct
device via link
```

The report also performs capability probes for:

* CPU work
* outbound connections
* file writes
* process creation
* TLS import
* memory allocation

The results are measured on the current machine rather than being hard-coded.

---

# Webedge

Webedge is a separate HTTP forwarding component implemented in:

```text
webedge.py
```

Its purpose is to provide an encrypted communication layer between a client and the configured sensor/device target.

The current implementation uses:

**RSA-OAEP with a 2048-bit RSA key pair and SHA-256.**

Webedge does not share code with the sensor or harness.

---

## Webedge Communication Flow

The current communication flow is:

```text
                         RSA-OAEP
Client  ------------------------------------> Webedge
        encrypted request + client public key

Webedge
   |
   | normal HTTP
   v

Configured target
(sensor.py or constrained device)

   |
   | normal HTTP response
   v

Webedge

   |
   | RSA-OAEP encrypted response
   v

Client
```

The client encrypts the requested path using the Webedge public key.

Webedge decrypts the request using its private key.

Webedge then forwards the request to the configured target using normal HTTP.

The target response is encrypted using the client's public key before being returned.

---

# Webedge Security

## RSA Key Pair

Webedge generates a 2048-bit RSA key pair when it starts:

```text
WEBEDGE_PRIVATE_KEY
WEBEDGE_PUBLIC_KEY
```

The key pair is generated in memory at startup.

The current implementation does not persist these keys to disk.

---

## Request Encryption

The client encrypts the request using:

```text
RSA-OAEP
MGF1 SHA-256
SHA-256
```

The encrypted request is sent to Webedge.

The client also sends its public key using:

```text
X-Client-Public-Key
```

The public key is encoded as Base64 containing DER-encoded public-key data.

---

## Response Encryption

After receiving the target response, Webedge encrypts the response using the client's public key.

The encrypted response is then returned to the client.

This provides encryption on both sides of the Webedge boundary:

```text
Client
  |
  | encrypted
  v
Webedge
  |
  | normal HTTP
  v
Target
```

---

## Invalid Requests

Webedge rejects requests that do not provide valid encrypted communication.

Examples include:

* missing encrypted request
* missing client public key
* invalid Base64 data
* malformed public key
* invalid encrypted request data

These requests return:

```text
HTTP 400
```

If Webedge cannot reach the configured target, it returns:

```text
HTTP 502
```

---

# Webedge Configuration

The target can be configured using environment variables.

### Target host

```bash
WEBEDGE_TARGET_HOST
```

Default:

```text
localhost
```

### Target port

```bash
WEBEDGE_TARGET_PORT
```

Default:

```text
8001
```

For example:

### Windows PowerShell

```powershell
$env:WEBEDGE_TARGET_HOST="localhost"
$env:WEBEDGE_TARGET_PORT="8000"
poetry run python webedge.py
```

### Linux/macOS

```bash
WEBEDGE_TARGET_HOST=localhost WEBEDGE_TARGET_PORT=8000 poetry run python webedge.py
```

Webedge itself listens on:

```text
http://localhost:8000
```

when started directly with:

```bash
poetry run python webedge.py
```

If the sensor is also using port `8000`, choose a different port for one of the services.

---

# Webedge Tests

Webedge tests are implemented in:

```text
test_webedge.py
```

The tests create a temporary target HTTP server and a temporary Webedge server.

Run them with:

```bash
poetry run python -m unittest test_webedge.py
```

The current test suite contains five tests.

### 1. RSA encryption and decryption

Verifies that an encrypted request can be decrypted using the Webedge private key.

### 2. Request forwarding

Verifies that Webedge forwards the request and returns the target response.

### 3. Path preservation

Verifies that a path such as:

```text
/test
```

reaches the target as:

```text
/test
```

### 4. Unencrypted request rejection

Verifies that an ordinary unencrypted request is rejected with:

```text
HTTP 400
```

### 5. Invalid public-key rejection

Verifies that an invalid client public key is rejected with:

```text
HTTP 400
```

---

# Project Structure

The main repository structure is:

```text
GroupProject/
│
├── documentation/
│
├── harness/
│   ├── __init__.py
│   ├── __main__.py
│   ├── README.md
│   ├── device.py
│   ├── firmware.py
│   ├── link.py
│   ├── profile.toml
│   ├── report.py
│   └── winjob.py
│
├── tests/
│   └── test_harness.py
│
├── sensor.py
├── test_sensor.py
├── webedge.py
├── test_webedge.py
│
├── CONTRIBUTING.md
├── poetry.lock
├── pyproject.toml
└── README.md
```

The exact files present on a branch can differ while individual contributions are being developed.

---

# Testing

## Sensor tests

Run:

```bash
poetry run python -m unittest test_sensor.py
```

The sensor tests verify:

* sensor creation
* measurement structure
* temperature range
* humidity range
* changing/random measurements
* HTTP health endpoint
* root endpoint
* measurements endpoint

---

## Harness tests

Run:

```bash
poetry run pytest
```

The harness tests verify the simulated device restrictions and link behavior.

---

## Webedge tests

Run:

```bash
poetry run python -m unittest test_webedge.py
```

The Webedge tests verify RSA communication, forwarding, path preservation and invalid-request handling.

---

# Development Rules

This project follows the development rules defined in `CONTRIBUTING.md`.

Important practices include:

* Create a separate branch for each contribution.
* Keep changes focused on one project area.
* Open a pull request early when appropriate.
* Use descriptive commit prefixes.
* Code must be created with LLM assistance as required by the course.
* Generated code must be reviewed before being introduced into the project.
* Prompts used for code generation should be documented according to the project documentation requirements.
* Pull requests should be reviewed by someone other than the author.

Examples of commit prefixes used in the project include:

```text
webedge:
webedge-test:
documentation:
build:
```

---

# Dependencies

The core legacy sensor uses Python's standard library.

The project also uses:

* **Poetry** for project and dependency management
* **pytest** for harness testing
* **cryptography** for the Webedge RSA implementation

The Webedge implementation requires the `cryptography` dependency.

When the Webedge contribution is integrated into the main project, the Poetry configuration must include that dependency.

---

# Current Status

### Legacy sensor

Implemented:

* Simulated temperature measurements
* Simulated humidity measurements
* HTTP API
* Health endpoint
* Measurement endpoints
* Unit/API tests

### Constrained device

Implemented:

* Separate device process
* CPU restrictions where supported
* Memory restrictions
* Process limit
* Module restrictions
* Read-only filesystem simulation
* Network restrictions
* HTTP link
* Performance reporting
* Capability probes
* Harness tests

### Webedge

Implemented:

* HTTP forwarding
* Configurable target
* RSA-2048 encryption
* RSA-OAEP with SHA-256
* Client public-key handling
* Encrypted responses
* Invalid request handling
* Five Webedge tests

### Modernization

The project is intended to continue evolving from the legacy system toward a more modern cryptographic design. The current Webedge implementation uses RSA-OAEP; **ML-KEM modernization is not represented as an already-completed feature in this README.**

---

## License

This repository is a university course project for the **Software Development, Maintenance and Operations** course at the University of Oulu.
