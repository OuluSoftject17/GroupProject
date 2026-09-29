import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import urlopen

import webedge


class TargetHandler(BaseHTTPRequestHandler):
    last_path = None

    def do_GET(self):
        TargetHandler.last_path = self.path

        body = b"hello from target"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class WebEdgeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.target_server = HTTPServer(
            ("localhost", 0),
            TargetHandler,
        )

        cls.target_port = cls.target_server.server_address[1]

        cls.target_thread = threading.Thread(
            target=cls.target_server.serve_forever,
            daemon=True,
        )
        cls.target_thread.start()

        webedge.TARGET_HOST = "localhost"
        webedge.TARGET_PORT = cls.target_port

        cls.webedge_server = HTTPServer(
            ("localhost", 0),
            webedge.WebEdgeHandler,
        )

        cls.webedge_port = cls.webedge_server.server_address[1]

        cls.webedge_thread = threading.Thread(
            target=cls.webedge_server.serve_forever,
            daemon=True,
        )
        cls.webedge_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.webedge_server.shutdown()
        cls.target_server.shutdown()

    def test_webedge_forwards_request(self):
        url = f"http://localhost:{self.webedge_port}/"

        with urlopen(url) as response:
            body = response.read()

        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"hello from target")

    def test_webedge_preserves_path(self):
        url = f"http://localhost:{self.webedge_port}/test"

        with urlopen(url) as response:
            body = response.read()

        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"hello from target")
        self.assertEqual(TargetHandler.last_path, "/test")


if __name__ == "__main__":
    unittest.main()