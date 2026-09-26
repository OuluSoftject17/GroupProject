"""
Command line for the simulated device.

    poetry run python -m harness up [--port N]
    poetry run python -m harness report [--seconds N]
"""

import argparse
import time

from harness.device import load_profile, start_sensor
from harness.link import start_link
from harness.report import print_report


def up(profile, port):
    device, device_port = start_sensor(profile)
    link = start_link(profile["link"], device_port, port)
    print(f"Device running. Reach it through the link: http://127.0.0.1:{link.server_address[1]}")
    print("Limits in force:")
    for limit in device.enforced:
        print(f"  - {limit}")
    print("Press Ctrl+C to stop.", flush=True)
    try:
        # Poll instead of wait(): on Windows, Ctrl+C cannot interrupt wait().
        while device.process.poll() is None:
            time.sleep(0.5)
        print(f"The device stopped by itself (exit code {device.process.returncode}).")
    except KeyboardInterrupt:
        pass
    finally:
        link.shutdown()
        link.server_close()
        device.stop()


def main():
    parser = argparse.ArgumentParser(prog="python -m harness")
    commands = parser.add_subparsers(dest="command", required=True)
    up_parser = commands.add_parser("up", help="start the device and its link")
    up_parser.add_argument("--port", type=int, default=0,
                           help="link port on localhost (default: any free port)")
    report_parser = commands.add_parser("report", help="measure the device")
    report_parser.add_argument("--seconds", type=float, default=5,
                               help="how long to measure each row")
    args = parser.parse_args()

    profile = load_profile()
    if args.command == "up":
        up(profile, args.port)
    else:
        print_report(profile, args.seconds)


if __name__ == "__main__":
    main()
