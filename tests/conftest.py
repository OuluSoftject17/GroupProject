import threading
import time
from dataclasses import dataclass

import pytest

from harness.client import make_client, open_target


@pytest.fixture(scope="session")
def target_url():
    """HARNESS_TARGET_URL if set, otherwise one sensor.py process for the session."""
    with open_target() as url:
        yield url


@pytest.fixture
def client(target_url):
    return make_client(target_url, timeout=2.0)


@dataclass
class Outcome:
    result: object
    error: BaseException
    elapsed: float


@pytest.fixture
def run_bounded():
    """Run fn() in a thread; fail the test (instead of hanging) if it exceeds `limit`."""

    def run(fn, limit):
        outcome = {}

        def target():
            started = time.monotonic()
            try:
                outcome["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                outcome["error"] = exc
            outcome["elapsed"] = time.monotonic() - started

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(limit)
        if thread.is_alive():
            pytest.fail(f"call did not return within {limit}s (hang)")
        return Outcome(outcome.get("result"), outcome.get("error"), outcome["elapsed"])

    return run
