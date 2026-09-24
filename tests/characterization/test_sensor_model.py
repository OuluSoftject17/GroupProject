"""Characterization of sensor.py internals: states, ranges, gradual changes.

The HTTP API cannot change the sauna state, so state behaviour is pinned here
in-process. Many seeds are used and only seed-independent properties are
asserted (derived from the update rule), never exact random values.

Update rule in SaunaSensor.read(), per step:
    dT = clamp(0.15 * (T_target - T) + U(-0.5, 0.5), -5, 5)
    dH =        0.15 * (H_target - H) + U(-1.0, 1.0)          (not clamped)
Once settled, readings stay within target +- noise / 0.15.
"""

import inspect
import random
import socketserver
import threading

import pytest

from harness.client import make_client
from sensor import SaunaRequestHandler, SaunaSensor, SaunaState, create_server

SEEDS = range(25)
TARGETS = {
    SaunaState.COLD: (22.0, 45.0),
    SaunaState.HEATING: (80.0, 40.0),
    SaunaState.HOT: (85.0, 55.0),
    SaunaState.COOLING: (25.0, 45.0),
}
TEMPERATURE_BAND = 0.5 / 0.15 + 0.05   # + rounding to 1 decimal
HUMIDITY_BAND = 1.0 / 0.15 + 0.05
MAX_TEMPERATURE_STEP = 5.0 + 0.1       # clamp + rounding of two readings
MAX_HUMIDITY_STEP = 5.0


def settle(sensor_, state, reads=100):
    sensor_.set_state(state)
    for _ in range(reads):
        sensor_.read()


# -- construction and states -----------------------------------------------------

def test_new_sensor_starts_cold_at_22c_45pct():
    s = SaunaSensor(seed=1)

    assert s.state is SaunaState.COLD
    assert (s.temperature, s.humidity) == (22.0, 45.0)


def test_limits_and_state_values():
    assert (SaunaSensor.MIN_TEMPERATURE, SaunaSensor.MAX_TEMPERATURE) == (20.0, 100.0)
    assert (SaunaSensor.MIN_HUMIDITY, SaunaSensor.MAX_HUMIDITY) == (10.0, 80.0)
    assert {state.value for state in SaunaState} == {"cold", "heating", "hot", "cooling"}


@pytest.mark.parametrize("bad_state", ["hot", "HOT", None, 1, SaunaState.HOT.value])
def test_set_state_accepts_only_enum_members(bad_state):
    s = SaunaSensor(seed=1)

    with pytest.raises(ValueError):
        s.set_state(bad_state)
    assert s.state is SaunaState.COLD


def test_state_never_changes_by_itself():
    # HEATING plateaus around 80 C and stays HEATING forever; there are no
    # automatic transitions (e.g. HEATING -> HOT), only set_state().
    s = SaunaSensor(seed=1)
    settle(s, SaunaState.HEATING, reads=300)

    assert s.state is SaunaState.HEATING


# -- measurement shape -----------------------------------------------------------

@pytest.mark.parametrize("state", list(SaunaState))
def test_read_returns_two_rounded_floats(state):
    s = SaunaSensor(seed=3)
    s.set_state(state)
    for _ in range(20):
        reading = s.read()
        assert set(reading) == {"temperatureC", "humidity%"}
        for value in reading.values():
            assert type(value) is float
            assert value == round(value, 1)


def test_same_seed_gives_same_sequence():
    a, b = SaunaSensor(seed=42), SaunaSensor(seed=42)
    a.set_state(SaunaState.HEATING)
    b.set_state(SaunaState.HEATING)

    assert [a.read() for _ in range(50)] == [b.read() for _ in range(50)]


# -- ranges ------------------------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("state", list(SaunaState))
def test_each_state_settles_within_a_fixed_band_around_its_target(seed, state):
    s = SaunaSensor(seed=seed)
    settle(s, state)
    target_temperature, target_humidity = TARGETS[state]

    for _ in range(50):
        reading = s.read()
        assert abs(reading["temperatureC"] - target_temperature) <= TEMPERATURE_BAND
        assert abs(reading["humidity%"] - target_humidity) <= HUMIDITY_BAND


@pytest.mark.parametrize("seed", SEEDS)
def test_readings_stay_within_class_limits_through_all_states(seed):
    s = SaunaSensor(seed=seed)
    for state in [SaunaState.HEATING, SaunaState.HOT, SaunaState.COOLING, SaunaState.COLD]:
        s.set_state(state)
        for _ in range(60):
            reading = s.read()
            assert s.MIN_TEMPERATURE <= reading["temperatureC"] <= s.MAX_TEMPERATURE
            assert s.MIN_HUMIDITY <= reading["humidity%"] <= s.MAX_HUMIDITY


def test_upper_limits_are_unreachable_in_normal_operation():
    # The hottest target is 85 C (+3.3 noise band), so the 100 C clamp is
    # never exercised by the simulation itself.
    s = SaunaSensor(seed=7)
    settle(s, SaunaState.HOT, reads=1000)
    assert s.temperature < 90.0


# -- gradual changes ---------------------------------------------------------------

@pytest.mark.parametrize("seed", SEEDS)
def test_first_heating_steps_are_clamped_to_plus_5c(seed):
    # From 22 C the unclamped step would be ~8.7 C; the clamp makes the first
    # steps exactly +5.0 until the gap to 80 C drops below ~36.7 C.
    s = SaunaSensor(seed=seed)
    previous = 22.0
    s.set_state(SaunaState.HEATING)
    for _ in range(4):
        current = s.read()["temperatureC"]
        assert current - previous == pytest.approx(5.0, abs=0.11)
        previous = current


@pytest.mark.parametrize("seed", SEEDS)
def test_steps_are_gradual_across_random_state_changes(seed):
    s = SaunaSensor(seed=seed)
    chooser = random.Random(seed)
    previous = s.read()
    for _ in range(400):
        if chooser.random() < 0.05:
            s.set_state(chooser.choice(list(SaunaState)))
        current = s.read()
        assert abs(current["temperatureC"] - previous["temperatureC"]) <= MAX_TEMPERATURE_STEP
        assert abs(current["humidity%"] - previous["humidity%"]) <= MAX_HUMIDITY_STEP
        previous = current


@pytest.mark.parametrize("state, direction", [
    (SaunaState.HEATING, +1), (SaunaState.HOT, +1), (SaunaState.COOLING, -1),
])
def test_state_changes_move_temperature_in_the_expected_direction(state, direction):
    for seed in SEEDS:
        s = SaunaSensor(seed=seed)
        if direction < 0:
            settle(s, SaunaState.HOT)
        start = s.read()["temperatureC"]
        s.set_state(state)
        for _ in range(10):
            end = s.read()["temperatureC"]
        assert (end - start) * direction > 10.0


def test_humidity_steps_are_not_clamped():
    # CHARACTERIZATION: possibly a bug - only temperature changes are limited
    # to 5 per step; humidity jumps are unbounded. It is invisible in normal
    # operation only because humidity never strays far from its targets.
    s = SaunaSensor(seed=1)
    s.set_state(SaunaState.HOT)       # humidity target 55 %
    s.humidity = 10.0                 # far from target (only possible by poking state)

    assert s.read()["humidity%"] - 10.0 > 5.0


# -- HTTP wiring (in-process) -------------------------------------------------------

def test_all_servers_share_one_class_level_unseeded_sensor():
    # CHARACTERIZATION: possibly a bug - `sensor` is a class attribute of the
    # handler, created at import time. Every server in the process shares it,
    # so state leaks between servers (and between tests that create servers).
    first, second = create_server("127.0.0.1", 0), create_server("127.0.0.1", 0)
    try:
        assert first.RequestHandlerClass.sensor is second.RequestHandlerClass.sensor
        assert isinstance(SaunaRequestHandler.sensor, SaunaSensor)
    finally:
        first.server_close()
        second.server_close()


def test_only_measurement_requests_advance_the_simulation(monkeypatch):
    reads = []

    class CountingSensor:
        def read(self):
            reads.append(1)
            return {"temperatureC": 22.0, "humidity%": 45.0}

    monkeypatch.setattr(SaunaRequestHandler, "sensor", CountingSensor())
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = make_client(f"http://127.0.0.1:{server.server_address[1]}")
        for _ in range(3):
            client.health()
        client.get("/nope")
        assert reads == []
        client.measurement("/")
        client.measurement("/measurements")
        assert len(reads) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_server_is_single_threaded_http_server():
    server = create_server("127.0.0.1", 0)
    try:
        assert not isinstance(server, socketserver.ThreadingMixIn)
        assert SaunaRequestHandler.timeout is None  # no per-connection timeout
    finally:
        server.server_close()


def test_create_server_defaults_to_localhost_port_8000():
    # CHARACTERIZATION: possibly a bug - `python sensor.py` always binds
    # localhost:8000 with no CLI/env override, so two instances (or anything
    # else on 8000) collide. The harness works around it by calling
    # create_server(port=0) instead of running the script.
    defaults = inspect.signature(create_server).parameters

    assert defaults["host"].default == "localhost"
    assert defaults["port"].default == 8000
