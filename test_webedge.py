import base64
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes, serialization

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
        cls.client_private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.client_public_key = cls.client_private_key.public_key()

        cls.client_public_key_header = base64.b64encode(
            cls.client_public_key.public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).decode()

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

    def encrypt_request(self, path):
        return webedge.WEBEDGE_PUBLIC_KEY.encrypt(
            path.encode(),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

    def decrypt_response(self, data):
        return self.client_private_key.decrypt(
            data,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

    def make_request(self, path):
        request = Request(
            f"http://localhost:{self.webedge_port}/",
            data=self.encrypt_request(path),
            method="GET",
        )
        request.add_header(
            "X-Client-Public-Key",
            self.client_public_key_header,
        )
        return request

    def test_rsa_encryption_and_decryption(self):
        encrypted = self.encrypt_request("/test")

        decrypted = webedge.WEBEDGE_PRIVATE_KEY.decrypt(
            encrypted,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        self.assertEqual(decrypted, b"/test")

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
            f"http://localhost:{self.webedge_port}/",
            method="GET",
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request)

        self.assertEqual(error.exception.code, 400)

    def test_invalid_public_key_is_rejected(self):
        request = Request(
            f"http://localhost:{self.webedge_port}/",
            data=self.encrypt_request("/test"),
            method="GET",
        )
        request.add_header(
            "X-Client-Public-Key",
            base64.b64encode(b"invalid public key").decode(),
        )

        with self.assertRaises(HTTPError) as error:
            urlopen(request)

        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
