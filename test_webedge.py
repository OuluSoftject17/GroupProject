import base64
import contextlib
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.asymmetric import mlkem, padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization

import webedge


LARGE_BODY = b"x" * 1000


class TargetHandler(BaseHTTPRequestHandler):

    last_path = None

    def do_GET(self):
        TargetHandler.last_path = self.path

        if self.path == "/large":
            body = LARGE_BODY
        else:
            body = b"hello from target"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class AttackerHandler(BaseHTTPRequestHandler):
    """Pretends to be WebEdge and serves the attacker's ML-KEM key."""

    public_key = None

    def do_GET(self):
        body = base64.b64encode(AttackerHandler.public_key.public_bytes_raw())

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
        cls.webedge_server.server_close()
        cls.target_server.server_close()

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
            f"http://127.0.0.1:{self.webedge_port}/",
            data=self.encrypt_request(path),
            method="GET",
        )
        request.add_header(
            "X-Client-Public-Key",
            self.client_public_key_header,
        )
        return request

    # ML-KEM client side. This is written independently of webedge.py so
    # that the tests check both sides agree on the protocol.

    def fetch_kem_public_key(self, port=None):
        port = port or self.webedge_port
        url = f"http://127.0.0.1:{port}/kem-public-key"

        with urlopen(url) as response:
            key_data = base64.b64decode(response.read())

        return mlkem.MLKEM768PublicKey.from_public_bytes(key_data)

    def fetch_pinned_kem_public_key(self, fingerprint, port=None):
        public_key = self.fetch_kem_public_key(port)
        actual = hashlib.sha256(public_key.public_bytes_raw()).hexdigest()

        if actual != fingerprint:
            raise ValueError(
                "WebEdge public key does not match the pinned fingerprint"
            )

        return public_key

    def derive_key(self, shared_secret):
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"webedge-mlkem-v1",
        ).derive(shared_secret)

    def mlkem_encrypt(self, data, key):
        nonce = os.urandom(12)
        return nonce + AESGCM(key).encrypt(nonce, data, None)

    def mlkem_decrypt(self, data, key):
        return AESGCM(key).decrypt(data[:12], data[12:], None)

    def make_mlkem_request(
        self,
        path,
        kem_ciphertext_header=None,
        body=None,
        public_key=None,
        port=None,
    ):
        port = port or self.webedge_port
        public_key = public_key or self.fetch_kem_public_key(port)
        shared_secret, kem_ciphertext = public_key.encapsulate()
        key = self.derive_key(shared_secret)

        if kem_ciphertext_header is None:
            kem_ciphertext_header = base64.b64encode(kem_ciphertext).decode()

        if body is None:
            body = self.mlkem_encrypt(path.encode(), key)

        request = Request(
            f"http://127.0.0.1:{port}/",
            data=body,
            method="GET",
        )
        request.add_header("X-Key-Exchange", "mlkem")

        if kem_ciphertext_header is not False:
            request.add_header("X-KEM-Ciphertext", kem_ciphertext_header)

        return request, key, kem_ciphertext

    def assert_status(self, request, status):
        with self.assertRaises(HTTPError) as error:
            urlopen(request)

        error.exception.close()
        self.assertEqual(error.exception.code, status)

    def fetch_metrics(self):
        url = f"http://127.0.0.1:{self.webedge_port}/metrics"

        with urlopen(url) as response:
            return json.loads(response.read())

    def start_server(self, handler):
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        return server.server_address[1]

    # Key files

    def keep_current_keys(self):
        # load_keys() replaces the module keys. Put them back after the
        # test so that the other tests keep working.
        patcher = mock.patch.multiple(
            webedge,
            WEBEDGE_PRIVATE_KEY=webedge.WEBEDGE_PRIVATE_KEY,
            WEBEDGE_PUBLIC_KEY=webedge.WEBEDGE_PUBLIC_KEY,
            WEBEDGE_KEM_PRIVATE_KEY=webedge.WEBEDGE_KEM_PRIVATE_KEY,
            WEBEDGE_KEM_PUBLIC_KEY=webedge.WEBEDGE_KEM_PUBLIC_KEY,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_temporary_directory(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return directory.name

    def make_key_directory(self):
        directory = self.make_temporary_directory()
        fingerprint = webedge.generate_keys(directory)
        return directory, fingerprint

    def key_env(self, directory):
        return {
            "WEBEDGE_RSA_KEY_FILE": os.path.join(
                directory,
                webedge.RSA_PRIVATE_KEY_FILE,
            ),
            "WEBEDGE_KEM_KEY_FILE": os.path.join(
                directory,
                webedge.KEM_PRIVATE_KEY_FILE,
            ),
        }

    def load_public_key_file(self, directory, name):
        with open(os.path.join(directory, name), "rb") as key_file:
            return serialization.load_pem_public_key(key_file.read())

    # RSA (legacy)

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
            f"http://127.0.0.1:{self.webedge_port}/",
            method="GET",
        )

        self.assert_status(request, 400)

    def test_invalid_public_key_is_rejected(self):
        request = Request(
            f"http://127.0.0.1:{self.webedge_port}/",
            data=self.encrypt_request("/test"),
            method="GET",
        )
        request.add_header(
            "X-Client-Public-Key",
            base64.b64encode(b"invalid public key").decode(),
        )

        self.assert_status(request, 400)

    def test_explicit_rsa_header_is_accepted(self):
        request = self.make_request("/test")
        request.add_header("X-Key-Exchange", "rsa")

        with urlopen(request) as response:
            body = self.decrypt_response(response.read())

        self.assertEqual(body, b"hello from target")

    def test_rsa_response_too_large_is_rejected(self):
        self.assert_status(self.make_request("/large"), 500)

    def test_rsa_request_rejected_when_disabled(self):
        with mock.patch.object(webedge, "ALLOW_RSA", False):
            self.assert_status(self.make_request("/test"), 403)

            request, key, _ = self.make_mlkem_request("/test")

            with urlopen(request) as response:
                body = self.mlkem_decrypt(response.read(), key)

        self.assertEqual(body, b"hello from target")

    # ML-KEM

    def test_kem_public_key_is_served(self):
        public_key = self.fetch_kem_public_key()

        self.assertEqual(
            public_key.public_bytes_raw(),
            webedge.WEBEDGE_KEM_PUBLIC_KEY.public_bytes_raw(),
        )

    def test_mlkem_forwards_request_and_preserves_path(self):
        request, key, _ = self.make_mlkem_request("/test")

        with urlopen(request) as response:
            body = self.mlkem_decrypt(response.read(), key)

        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"hello from target")
        self.assertEqual(TargetHandler.last_path, "/test")

    def test_mlkem_large_response(self):
        request, key, _ = self.make_mlkem_request("/large")

        with urlopen(request) as response:
            body = self.mlkem_decrypt(response.read(), key)

        self.assertEqual(body, LARGE_BODY)

    def test_mlkem_tampered_body_is_rejected(self):
        request, _, _ = self.make_mlkem_request("/test")
        tampered = bytearray(request.data)
        tampered[-1] ^= 1
        request.data = bytes(tampered)

        self.assert_status(request, 400)

    def test_mlkem_tampered_kem_ciphertext_is_rejected(self):
        request, _, kem_ciphertext = self.make_mlkem_request("/test")
        tampered = bytearray(kem_ciphertext)
        tampered[0] ^= 1
        request.add_header(
            "X-KEM-Ciphertext",
            base64.b64encode(bytes(tampered)).decode(),
        )

        self.assert_status(request, 400)

    def test_mlkem_wrong_length_kem_ciphertext_is_rejected(self):
        request, _, _ = self.make_mlkem_request(
            "/test",
            kem_ciphertext_header=base64.b64encode(b"short").decode(),
        )

        self.assert_status(request, 400)

    def test_mlkem_missing_kem_ciphertext_is_rejected(self):
        request, _, _ = self.make_mlkem_request(
            "/test",
            kem_ciphertext_header=False,
        )

        self.assert_status(request, 400)

    def test_mlkem_invalid_base64_kem_ciphertext_is_rejected(self):
        request, _, _ = self.make_mlkem_request(
            "/test",
            kem_ciphertext_header="not base64!",
        )

        self.assert_status(request, 400)

    def test_mlkem_path_without_leading_slash_is_rejected(self):
        request, _, _ = self.make_mlkem_request("@example.com/")

        self.assert_status(request, 400)

    def test_unknown_key_exchange_is_rejected(self):
        request = self.make_request("/test")
        request.add_header("X-Key-Exchange", "x25519")

        self.assert_status(request, 400)

    # Persistent keys and pinning

    def test_generated_key_files_load_back(self):
        self.keep_current_keys()
        directory, fingerprint = self.make_key_directory()

        with mock.patch.dict(os.environ, self.key_env(directory)):
            webedge.load_keys()

        kem_public_key = self.load_public_key_file(
            directory,
            webedge.KEM_PUBLIC_KEY_FILE,
        )
        rsa_public_key = self.load_public_key_file(
            directory,
            webedge.RSA_PUBLIC_KEY_FILE,
        )

        self.assertEqual(
            kem_public_key.public_bytes_raw(),
            webedge.WEBEDGE_KEM_PUBLIC_KEY.public_bytes_raw(),
        )
        self.assertEqual(
            rsa_public_key.public_numbers(),
            webedge.WEBEDGE_PUBLIC_KEY.public_numbers(),
        )
        self.assertEqual(
            hashlib.sha256(kem_public_key.public_bytes_raw()).hexdigest(),
            fingerprint,
        )

    def test_pinned_key_still_works_after_restart(self):
        self.keep_current_keys()
        directory, _ = self.make_key_directory()
        pinned_public_key = self.load_public_key_file(
            directory,
            webedge.KEM_PUBLIC_KEY_FILE,
        )

        with mock.patch.dict(os.environ, self.key_env(directory)):
            webedge.load_keys()

            request, key, _ = self.make_mlkem_request(
                "/test",
                public_key=pinned_public_key,
            )

            with urlopen(request) as response:
                body = self.mlkem_decrypt(response.read(), key)

            self.assertEqual(body, b"hello from target")

            # Simulated restart: load the keys from the files again and
            # start a new server. The client keeps its pinned key.
            webedge.load_keys()
            port = self.start_server(webedge.WebEdgeHandler)

            request, key, _ = self.make_mlkem_request(
                "/test",
                public_key=pinned_public_key,
                port=port,
            )

            with urlopen(request) as response:
                body = self.mlkem_decrypt(response.read(), key)

        self.assertEqual(body, b"hello from target")

    def test_missing_key_file_fails(self):
        self.keep_current_keys()
        directory = self.make_temporary_directory()
        current_key = webedge.WEBEDGE_KEM_PRIVATE_KEY
        env = {
            "WEBEDGE_RSA_KEY_FILE": os.path.join(directory, "missing.pem"),
            "WEBEDGE_KEM_KEY_FILE": os.path.join(directory, "missing.pem"),
        }

        with mock.patch.dict(os.environ, env):
            with self.assertRaises(webedge.KeyLoadError):
                webedge.load_keys()

        self.assertIs(webedge.WEBEDGE_KEM_PRIVATE_KEY, current_key)

    def test_corrupt_key_file_fails(self):
        self.keep_current_keys()
        directory, _ = self.make_key_directory()
        env = self.key_env(directory)
        current_key = webedge.WEBEDGE_KEM_PRIVATE_KEY

        with open(env["WEBEDGE_KEM_KEY_FILE"], "wb") as key_file:
            key_file.write(
                b"-----BEGIN PRIVATE KEY-----\n"
                b"AAAA\n"
                b"-----END PRIVATE KEY-----\n"
            )

        with mock.patch.dict(os.environ, env):
            with self.assertRaises(webedge.KeyLoadError):
                webedge.load_keys()

        self.assertIs(webedge.WEBEDGE_KEM_PRIVATE_KEY, current_key)

    def test_wrong_key_type_fails(self):
        self.keep_current_keys()
        directory, _ = self.make_key_directory()
        env = self.key_env(directory)
        env["WEBEDGE_KEM_KEY_FILE"] = env["WEBEDGE_RSA_KEY_FILE"]

        with mock.patch.dict(os.environ, env):
            with self.assertRaises(webedge.KeyLoadError):
                webedge.load_keys()

    def test_webedge_refuses_to_start_with_bad_key_file(self):
        self.keep_current_keys()
        directory = self.make_temporary_directory()
        env = {
            "WEBEDGE_RSA_KEY_FILE": os.path.join(directory, "missing.pem"),
            "WEBEDGE_KEM_KEY_FILE": os.path.join(directory, "missing.pem"),
        }

        with mock.patch.dict(os.environ, env), \
                mock.patch("logging.basicConfig"):
            with self.assertRaises(SystemExit):
                webedge.run(host="127.0.0.1", port=0)

    def test_temporary_key_is_logged_as_warning(self):
        self.keep_current_keys()
        current_key = webedge.WEBEDGE_KEM_PRIVATE_KEY

        with mock.patch.dict(os.environ):
            os.environ.pop("WEBEDGE_RSA_KEY_FILE", None)
            os.environ.pop("WEBEDGE_KEM_KEY_FILE", None)

            with self.assertLogs("webedge", level="WARNING") as logs:
                webedge.load_keys()

        output = "\n".join(logs.output)

        self.assertIn("WEBEDGE_KEM_KEY_FILE is not set", output)
        self.assertIn("WEBEDGE_RSA_KEY_FILE is not set", output)
        self.assertIs(webedge.WEBEDGE_KEM_PRIVATE_KEY, current_key)

    def test_fingerprint_is_logged_without_private_key(self):
        self.keep_current_keys()
        directory, fingerprint = self.make_key_directory()

        with mock.patch.dict(os.environ, self.key_env(directory)):
            with self.assertLogs("webedge", level="INFO") as logs:
                webedge.load_keys()

        output = "\n".join(logs.output)

        self.assertIn(fingerprint, output)
        self.assertNotIn("PRIVATE KEY", output)

    def test_generate_keys_refuses_to_overwrite(self):
        directory, _ = self.make_key_directory()
        key_path = os.path.join(directory, webedge.KEM_PRIVATE_KEY_FILE)

        with open(key_path, "rb") as key_file:
            original = key_file.read()

        with self.assertRaises(FileExistsError):
            webedge.generate_keys(directory)

        with open(key_path, "rb") as key_file:
            self.assertEqual(key_file.read(), original)

    def test_generate_keys_command(self):
        directory = self.make_temporary_directory()
        argv = ["webedge.py", "generate-keys", directory]

        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(webedge.main(argv), 0)

        self.assertIn("fingerprint", output.getvalue())

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(webedge.main(argv), 1)

    @unittest.skipIf(os.name == "nt", "POSIX file permissions only")
    def test_private_key_files_are_owner_only(self):
        directory, _ = self.make_key_directory()

        for name in (
            webedge.RSA_PRIVATE_KEY_FILE,
            webedge.KEM_PRIVATE_KEY_FILE,
        ):
            mode = os.stat(os.path.join(directory, name)).st_mode
            self.assertEqual(mode & 0o777, 0o600)

    def test_pinned_client_rejects_swapped_key(self):
        fingerprint = hashlib.sha256(
            webedge.WEBEDGE_KEM_PUBLIC_KEY.public_bytes_raw()
        ).hexdigest()

        AttackerHandler.public_key = (
            mlkem.MLKEM768PrivateKey.generate().public_key()
        )
        attacker_port = self.start_server(AttackerHandler)

        # The real WebEdge key passes the check...
        self.fetch_pinned_kem_public_key(fingerprint)

        # ...but the key served by the man-in-the-middle does not.
        with self.assertRaises(ValueError):
            self.fetch_pinned_kem_public_key(fingerprint, port=attacker_port)

    def test_request_for_attacker_key_is_rejected(self):
        attacker_public_key = mlkem.MLKEM768PrivateKey.generate().public_key()

        request, _, _ = self.make_mlkem_request(
            "/test",
            public_key=attacker_public_key,
        )

        self.assert_status(request, 400)

    # Observability

    def test_metrics_count_requests_per_method(self):
        with mock.patch.dict(webedge.METRICS, clear=True):
            urlopen(self.make_request("/test")).close()

            request, _, _ = self.make_mlkem_request("/test")
            urlopen(request).close()
            urlopen(self.make_mlkem_request("/test")[0]).close()

            request, _, _ = self.make_mlkem_request(
                "/test",
                kem_ciphertext_header=False,
            )
            self.assert_status(request, 400)

            metrics = self.fetch_metrics()

        self.assertEqual(
            metrics,
            {
                "rsa": {"200": 1},
                "mlkem": {"200": 2, "400": 1},
            },
        )

    def test_request_is_logged_without_secrets(self):
        request, key, kem_ciphertext = self.make_mlkem_request("/test")

        with self.assertLogs("webedge", level="INFO") as logs:
            urlopen(request).close()

        output = "\n".join(logs.output)

        self.assertIn("key_exchange=mlkem", output)
        self.assertIn("status=200", output)
        self.assertIn("path='/test'", output)
        self.assertNotIn(base64.b64encode(key).decode(), output)
        self.assertNotIn(key.hex(), output)
        self.assertNotIn(
            base64.b64encode(kem_ciphertext).decode()[:32],
            output,
        )


if __name__ == "__main__":
    unittest.main()
