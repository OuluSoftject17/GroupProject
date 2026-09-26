"""
Code that runs inside the simulated device.

The harness starts this module as a child process. It waits for a "go" line
on stdin (so the harness can apply OS limits first), optionally installs the
sandbox, and then either serves the legacy sensor or runs one probe.

The sandbox is an audit hook (sys.addaudithook). It simulates a stripped-down
device runtime. It is NOT a security boundary: code running in the process
could still find ways around it.
"""

import os
import sys

LOOPBACK = ("127.0.0.1", "::1", "localhost")
SPAWN_EVENTS = (
    "subprocess.Popen", "os.system", "os.exec", "os.spawn",
    "os.posix_spawn", "os.fork", "os.forkpty", "os.startfile",
)
DNS_EVENTS = (
    "socket.getaddrinfo", "socket.gethostbyname",
    "socket.gethostbyname_ex", "socket.gethostbyaddr",
)
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def install_sandbox(allowed_modules):
    """Block what the device does not have: extra modules, disk writes,
    outbound network, DNS and new processes."""
    allowed = set(allowed_modules)

    def hook(event, args):
        if event == "import":
            name = args[0]
            if name.split(".")[0] not in allowed:
                raise ImportError(f"module {name!r} is not installed on the device")

        elif event == "open":
            mode, flags = args[1], args[2] or 0
            if (mode and any(c in mode for c in "wax+")) or flags & WRITE_FLAGS:
                raise PermissionError("the device filesystem is read-only")

        elif event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            raise PermissionError("the device cannot open outbound connections")

        elif event in DNS_EVENTS:
            if args[0] not in LOOPBACK:
                raise PermissionError("the device has no DNS")

        elif event == "socket.bind":
            if args[1][0] not in LOOPBACK:
                raise PermissionError("the device may only listen on loopback")

        elif event in SPAWN_EVENTS:
            raise PermissionError("the device cannot start other processes")

    sys.addaudithook(hook)


def serve():
    import sensor

    server = sensor.create_server(host="127.0.0.1", port=0)
    print(f"PORT {server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# Probes: each one tries something a capable machine can do.

def probe_outbound_connect(port):
    import socket
    socket.create_connection(("127.0.0.1", int(port)), timeout=2).close()


def probe_file_write(path):
    with open(path, "w") as f:
        f.write("hello")


def probe_spawn_process():
    if os.system("exit 0") != 0:
        raise OSError("the child process failed")


def probe_import_tls():
    import ssl  # noqa: F401


def probe_cpu_work():
    """A fixed amount of pure-Python work. Returns how long it took."""
    import time
    start = time.perf_counter()
    total = 0
    for i in range(3_000_000):
        total += i * i
    return f"{time.perf_counter() - start:.2f} s for a fixed CPU task"


def probe_allocate(megabytes):
    data = bytearray(int(megabytes) * 1024 * 1024)
    data[-1] = 1


PROBES = {
    "outbound_connect": probe_outbound_connect,
    "file_write": probe_file_write,
    "spawn_process": probe_spawn_process,
    "import_tls": probe_import_tls,
    "allocate": probe_allocate,
    "cpu_work": probe_cpu_work,
}


def probe(name, *args):
    try:
        details = PROBES[name](*args)
    except (OSError, ImportError, MemoryError) as error:
        message = str(error) or "no details"
        print(f"RESULT blocked: {type(error).__name__}: {message}", flush=True)
    else:
        print(f"RESULT ok: {details}" if details else "RESULT ok", flush=True)


def main(argv):
    # Usage: firmware.py [--allow mod1,mod2] serve | probe NAME [ARG]
    allowed = None
    if argv[:1] == ["--allow"]:
        allowed = argv[1].split(",")
        argv = argv[2:]

    # Wait until the harness has applied the OS limits. Anything else
    # (for example the harness died) means: do not run at all.
    if sys.stdin.readline().strip() != "go":
        sys.exit("device: no go signal from the harness")
    if allowed is not None:
        install_sandbox(allowed)

    if argv[0] == "serve":
        serve()
    else:
        probe(argv[1], *argv[2:])


if __name__ == "__main__":
    main(sys.argv[1:])
