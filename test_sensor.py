import json
import threading
import unittest
from urllib.request import urlopen

from sensor import SaunaSensor, SaunaState, create_server


class TestSaunaSensor(unittest.TestCase):

    def test_sensor_creation(self):
        sensor = SaunaSensor(seed=1)

        self.assertIsInstance(sensor, SaunaSensor)
        self.assertEqual(sensor.state, SaunaState.COLD)

    def test_read_returns_measurement_structure(self):
        sensor = SaunaSensor(seed=1)

        reading = sensor.read()

        self.assertIsInstance(reading, dict)
        self.assertIn("temperatureC", reading)
        self.assertIn("humidity%", reading)

    def test_temperature_is_within_range(self):
        sensor = SaunaSensor(seed=1)
        sensor.set_state(SaunaState.HOT)

        for _ in range(100):
            reading = sensor.read()

            self.assertGreaterEqual(reading["temperatureC"], 20.0)
            self.assertLessEqual(reading["temperatureC"], 100.0)

    def test_humidity_is_within_range(self):
        sensor = SaunaSensor(seed=1)
        sensor.set_state(SaunaState.HOT)

        for _ in range(100):
            reading = sensor.read()

            self.assertGreaterEqual(reading["humidity%"], 10.0)
            self.assertLessEqual(reading["humidity%"], 80.0)

    def test_heating_state_increases_temperature(self):
        sensor = SaunaSensor(seed=1)

        sensor.set_state(SaunaState.COLD)
        sensor.read()

        sensor.set_state(SaunaState.HEATING)

        temperatures = []

        for _ in range(10):
            reading = sensor.read()
            temperatures.append(reading["temperatureC"])

        self.assertGreater(
            temperatures[-1],
            temperatures[0]
        )

    def test_cooling_state_decreases_temperature(self):
        sensor = SaunaSensor(seed=1)

        sensor.set_state(SaunaState.HOT)

        for _ in range(20):
            sensor.read()

        initial_temperature = sensor.read()["temperatureC"]

        sensor.set_state(SaunaState.COOLING)

        for _ in range(10):
            final_temperature = sensor.read()["temperatureC"]

        self.assertLess(
            final_temperature,
            initial_temperature
        )

    def test_measurements_change_gradually(self):
        sensor = SaunaSensor(seed=1)
        sensor.set_state(SaunaState.HEATING)

        previous = sensor.read()

        for _ in range(20):
            current = sensor.read()

            temperature_difference = abs(
                current["temperatureC"] - previous["temperatureC"]
            )

            humidity_difference = abs(
                current["humidity%"] - previous["humidity%"]
            )

            self.assertLessEqual(temperature_difference, 5.0)
            self.assertLessEqual(humidity_difference, 5.0)

            previous = current


class TestHTTPAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = create_server(
            host="localhost",
            port=8765
        )

        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True
        )

        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def test_health_endpoint(self):
        with urlopen("http://localhost:8765/health") as response:
            body = response.read().decode("utf-8")

            self.assertEqual(response.status, 200)
            self.assertEqual(body, "OK")

    def test_root_endpoint_returns_measurement(self):
        with urlopen("http://localhost:8765/") as response:
            body = response.read().decode("utf-8")

            self.assertEqual(response.status, 200)

            measurement = json.loads(body)

            self.assertIn("temperatureC", measurement)
            self.assertIn("humidity%", measurement)

    def test_measurements_endpoint_returns_measurement(self):
        with urlopen("http://localhost:8765/measurements") as response:
            body = response.read().decode("utf-8")

            self.assertEqual(response.status, 200)

            measurement = json.loads(body)

            self.assertIn("temperatureC", measurement)
            self.assertIn("humidity%", measurement)


if __name__ == "__main__":
    unittest.main()
