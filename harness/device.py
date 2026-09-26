"""
Start the legacy sensor as a separate process with OS-enforced limits.

Windows: a Job Object caps CPU rate and committed memory, allows only one
process, and kills the device when the harness exits.
Linux: setrlimit caps the data segment and the device dies with the harness.
If `systemd-run --user` is available, a user-level cgroup also caps the CPU
rate and memory. Without it there is no CPU rate cap on Linux.
"""

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = Path(__file__).resolve().parent / "profile.toml"

# On Windows the venv python.exe is a launcher that starts the real
# interpreter as a second process, so start the real one directly.
# The device only needs the standard library, not the venv packages.
PYTHON = getattr(sys, "_base_executable", sys.executable)


def load_profile(path=PROFILE_PATH, **overrides):
    """Read the profile. overrides look like link={"drop_rate": 0}."""
    with open(path, "rb") as f:
        profile = tomllib.load(f)
    for section, values in overrides.items():
        profile[section].update(values)
    return profile


class Device:
    """A running device process. Use it as a context manager."""

    def __init__(self, profile, args, restricted=True):
        limits = profile["device"]
        cmd = [PYTHON, "-E", "-s", "-B", "-m", "harness.firmware"]
        if restricted:
            cmd += ["--allow", ",".join(limits["allowed_modules"])]
        cmd += [str(arg) for arg in args]

        self.enforced = []
        self._job = None
        preexec = None
        if restricted and sys.platform != "win32":
            cmd, preexec = self._linux_limits(cmd, limits)

        self.process = subprocess.Popen(
            cmd, cwd=REPO_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, preexec_fn=preexec,
        )
        if restricted and sys.platform == "win32":
            from harness import winjob
            try:
                self._job = winjob.apply(self.process, limits)
            except OSError:
                self.process.kill()
                raise
            self.enforced += [
                f"CPU {limits['cpu_percent']}% of one core (Job Object)",
                f"memory {limits['memory_mb']} MB committed (Job Object)",
                "one process only (Job Object)",
            ]
        if restricted:
            self.enforced.append("module allowlist, no disk writes, no outbound "
                                 "network or DNS, no new processes (audit hook, emulated)")

        # The device waits for this line, so the limits are in place before
        # any device code runs.
        self.process.stdin.write("go\n")
        self.process.stdin.flush()

    def _linux_limits(self, cmd, limits):
        memory = limits["memory_mb"] * 1024 * 1024

        def preexec():
            import ctypes
            import resource
            import signal
            resource.setrlimit(resource.RLIMIT_DATA, (memory, memory))
            # PR_SET_PDEATHSIG: kill the device if the harness dies.
            ctypes.CDLL(None).prctl(1, signal.SIGKILL)

        self.enforced.append(f"memory {limits['memory_mb']} MB data segment (setrlimit)")
        if _systemd_run_works():
            cmd = ["systemd-run", "--user", "--scope", "--quiet",
                   "-p", f"CPUQuota={limits['cpu_percent']}%",
                   "-p", f"MemoryMax={limits['memory_mb']}M", "--"] + cmd
            self.enforced.append(f"CPU {limits['cpu_percent']}% of one core (systemd cgroup)")
        else:
            self.enforced.append("CPU: NOT capped (systemd-run --user unavailable)")
        return cmd, preexec

    def read_result(self, prefix):
        """Return the rest of the first stdout line that starts with prefix."""
        for line in self.process.stdout:
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return None  # the device exited without printing it

    def peak_memory_mb(self):
        """Peak resident memory of the device so far."""
        if sys.platform == "win32":
            from harness import winjob
            return winjob.peak_working_set(self.process) / 2**20
        with open(f"/proc/{self.process.pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
        return float("nan")

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        if self._job is not None:
            self._job.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def _systemd_run_works():
    if shutil.which("systemd-run") is None:
        return False
    check = subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet", "true"],
        capture_output=True,
    )
    return check.returncode == 0


def start_sensor(profile, restricted=True):
    """Start the sensor on the device. Returns (device, internal_port)."""
    device = Device(profile, ["serve"], restricted)
    port = device.read_result("PORT ")
    if port is None:
        device.stop()
        raise RuntimeError("the device exited before the sensor started")
    return device, int(port)

