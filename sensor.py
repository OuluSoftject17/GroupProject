import json
import random
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer


class SaunaState(Enum):
    COLD = "cold"
    HEATING = "heating"
    HOT = "hot"
    COOLING = "cooling"


class SaunaSensor:
    """
    Simulated sauna temperature and relative humidity sensor.

    This is a fake sensor for the legacy-system project.
    No physical hardware is used.
    """

    MIN_TEMPERATURE = 20.0
    MAX_TEMPERATURE = 100.0

    MIN_HUMIDITY = 10.0
    MAX_HUMIDITY = 80.0

    def __init__(self, seed=None):
        self.random = random.Random(seed)

        self.state = SaunaState.COLD
        self.temperature = 22.0
        self.humidity = 45.0

    def set_state(self, state):
        """Change the simulated sauna state."""
        if not isinstance(state, SaunaState):
            raise ValueError("state must be a SaunaState")

        self.state = state

    def read(self):
        """
        Generate and return the current simulated measurement.
        """

        if self.state == SaunaState.COLD:
            target_temperature = 22.0
            target_humidity = 45.0

        elif self.state == SaunaState.HEATING:
            target_temperature = 80.0
            target_humidity = 40.0

        elif self.state == SaunaState.HOT:
            target_temperature = 85.0
            target_humidity = 55.0

        elif self.state == SaunaState.COOLING:
            target_temperature = 25.0
            target_humidity = 45.0

        else:
            target_temperature = 22.0
            target_humidity = 45.0

        # Move gradually towards the target.
        temperature_change = (target_temperature - self.temperature) * 0.15
        humidity_change = (target_humidity - self.humidity) * 0.15

        # Add small random sensor variation.
        temperature_change += self.random.uniform(-0.5, 0.5)
        humidity_change += self.random.uniform(-1.0, 1.0)

        # Limit temperature changes to keep readings gradual.
        temperature_change = max(-5.0, min(5.0, temperature_change))    

        self.temperature += temperature_change
        self.humidity += humidity_change

        # Keep values inside sensible physical ranges.
        self.temperature = max(
            self.MIN_TEMPERATURE,
            min(self.MAX_TEMPERATURE, self.temperature)
        )

        self.humidity = max(
            self.MIN_HUMIDITY,
            min(self.MAX_HUMIDITY, self.humidity)
        )

        return {
            "temperatureC": round(self.temperature, 1),
            "humidity%": round(self.humidity, 1)
        }


class SaunaRequestHandler(BaseHTTPRequestHandler):
    """
    HTTP interface for the legacy sauna sensor.
    """

    sensor = SaunaSensor()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            return

        if self.path in ("/", "/measurements"):
            measurement = self.sensor.read()

            response = json.dumps(measurement).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        """Keep the simulator output simple by disabling HTTP request logs."""
        pass


def create_server(host="localhost", port=8000):
    """
    Create the legacy HTTP server.
    """
    return HTTPServer(
        (host, port),
        SaunaRequestHandler
    )


if __name__ == "__main__":
    server = create_server()

    print("Sauna sensor server running on http://localhost:8000")
    print("Available endpoints:")
    print("  GET /health")
    print("  GET /")
    print("  GET /measurements")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
