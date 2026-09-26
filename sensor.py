import json
import random
from http.server import BaseHTTPRequestHandler, HTTPServer


class SaunaSensor:
    """
    Simulated sauna temperature and relative humidity sensor.

    This is a simple sensor for the legacy-system project.
    No physical hardware is used. Each reading contains random
    temperature and humidity values within sensible limits.
    """

    MIN_TEMPERATURE = 20.0
    MAX_TEMPERATURE = 100.0

    MIN_HUMIDITY = 10.0
    MAX_HUMIDITY = 80.0

    def __init__(self, seed=None):
        self.random = random.Random(seed)

    def read(self):
        """
        Generate and return a random simulated measurement.
        """

        temperature = self.random.uniform(
            self.MIN_TEMPERATURE,
            self.MAX_TEMPERATURE
        )

        humidity = self.random.uniform(
            self.MIN_HUMIDITY,
            self.MAX_HUMIDITY
        )

        return {
            "temperatureC": round(temperature, 1),
            "humidity%": round(humidity, 1)
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
