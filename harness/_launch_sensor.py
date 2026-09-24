"""Child-process entry point that serves sensor.py on an OS-assigned port.

`python sensor.py` always binds port 8000, so instead of running the file as a
script we import it and call its own `create_server()` (the exact function the
`__main__` block uses) with port 0. The OS picks a free port, which is printed
on stdout as `PORT <n>` so the parent process can find it without races.

Run as a file path (not `-m`) so it works regardless of the working directory:

    python harness/_launch_sensor.py --sensor-dir <dir containing sensor.py>
"""

import argparse
import sys

PORT_LINE_PREFIX = "PORT "


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--sensor-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    sys.path.insert(0, args.sensor_dir)
    import sensor  # noqa: E402  (import after sys.path is set on purpose)

    server = sensor.create_server(host=args.host, port=0)
    try:
        print(f"{PORT_LINE_PREFIX}{server.server_address[1]}", flush=True)
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
