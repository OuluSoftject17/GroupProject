import json
import threading
import unittest
from urllib.request import urlopen

from sensor import SaunaSensor, create_server


class TestSaunaSensor(unittest.TestCase):

    def test_sensor_creation(self):
        sensor = SaunaSensor(seed=1)

        self.assertIsInstance(sensor, SaunaSensor)

    def test_read_returns_measurement_structure(self):
        sensor = SaunaSensor(seed=1)

        reading = sensor.read()

        self.assertIsInstance(reading, dict)
        self.assertIn("temperatureC", reading)
        self.assertIn("humidity%", reading)

    def test_temperature_is_within_range(self):
        sensor = SaunaSensor(seed=1)

        for _ in range(100):
            reading = sensor.read()

            self.assertGreaterEqual(reading["temperatureC"], 20.0)
            self.assertLessEqual(reading["temperatureC"], 100.0)

    def test_humidity_is_within_range(self):
        sensor = SaunaSensor(seed=1)

        for _ in range(100):
            reading = sensor.read()

            self.assertGreaterEqual(reading["humidity%"], 10.0)
            self.assertLessEqual(reading["humidity%"], 80.0)

    def test_measurements_are_random(self):
        sensor = SaunaSensor(seed=1)

        first_reading = sensor.read()
        second_reading = sensor.read()

        self.assertNotEqual(first_reading, second_reading)


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
