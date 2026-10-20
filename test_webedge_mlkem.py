import base64
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import webedge_mlkem as webedge


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


class WebEdgeMLKEMTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.target_server = HTTPServer(
            ("127.0.0.1", 0),
            TargetHandler,
        )
        cls.target_port = cls.target_server.server_address[1]

        cls.target_thread = threading.Thread(
            target=cls.target_server.serve_forever,
            daemon=True,
        )
        cls.target_thread.start()

        webedge.TARGET_HOST = "127.0.0.1"
        webedge.TARGET_PORT = cls.target_port

        cls.webedge_server = HTTPServer(
            ("127.0.0.1", 0),
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

    def _encrypt_and_encapsulate(self, path: str):
        """Perform ML-KEM encapsulation with the server's public key 
        and encrypt the request path using AES-GCM.
        """
        shared_secret, ciphertext = webedge.WEBEDGE_PUBLIC_KEY.encapsulate()
        encrypted_path = webedge.encrypt(path.encode(), shared_secret)
        ciphertext_b64 = base64.b64encode(ciphertext).decode()
        return encrypted_path, ciphertext_b64, shared_secret

    def make_request(self, path):
        encrypted_path, ciphertext_b64, self._last_shared_secret = self._encrypt_and_encapsulate(path)
        
        request = Request(
            f"http://127.0.0.1:{self.webedge_port}/",
            data=encrypted_path,
            method="GET",
        )
        request.add_header(
            "X-MLKEM-Ciphertext",
            ciphertext_b64,
        )
        return request

    def decrypt_response(self, data):
        return webedge.decrypt(data, self._last_shared_secret)

    def test_mlkem_encapsulation_roundtrip(self):
        shared_secret_sender, ciphertext = webedge.WEBEDGE_PUBLIC_KEY.encapsulate()
        shared_secret_receiver = webedge.WEBEDGE_PRIVATE_KEY.decapsulate(ciphertext)
        self.assertEqual(shared_secret_sender, shared_secret_receiver)

    def test_webedge_forwards_request(self):
        with urlopen(self.make_request("/")) as response:
            body = self.decrypt_response(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"hello from target")

    def test_webedge_preserves_path(self):
        with urlopen(self.make_request("/test")) as response:
            body = self.decrypt_response(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"hello from target")
        self.assertEqual(TargetHandler.last_path, "/test")

    def test_unencrypted_request_is_rejected(self):
        request = Request(
            f"http://127.0.0.1:{self.webedge_port}/",
            method="GET",
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request)

        self.assertEqual(error.exception.code, 400)

    def test_invalid_ciphertext_is_rejected(self):
        encrypted_path, _, _ = self._encrypt_and_encapsulate("/test")
        request = Request(
            f"http://127.0.0.1:{self.webedge_port}/",
            data=encrypted_path,
            method="GET",
        )
        request.add_header(
            "X-MLKEM-Ciphertext",
            base64.b64encode(b"invalid ciphertext").decode(),
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request)

        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()