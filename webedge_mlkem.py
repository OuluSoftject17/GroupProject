from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen
import base64
import binascii
import os

from cryptography.hazmat.primitives.asymmetric.mlkem import (
    MLKEM768PrivateKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


TARGET_HOST = os.getenv("WEBEDGE_TARGET_HOST", "localhost")
TARGET_PORT = int(os.getenv("WEBEDGE_TARGET_PORT", "8001"))

# Generate Post-Quantum ML-KEM-768 Key Pair
WEBEDGE_PRIVATE_KEY = MLKEM768PrivateKey.generate()
WEBEDGE_PUBLIC_KEY = WEBEDGE_PRIVATE_KEY.public_key()


def encrypt(data: bytes, shared_secret: bytes) -> bytes:
    """Encrypt payload data using AES-GCM with the ML-KEM shared secret."""
    aesgcm = AESGCM(shared_secret)
    nonce = os.urandom(12)  # 12-byte nonce for GCM
    return nonce + aesgcm.encrypt(nonce, data, None)


def decrypt(encrypted_data: bytes, shared_secret: bytes) -> bytes:
    """Decrypt payload data using AES-GCM with the ML-KEM shared secret."""
    aesgcm = AESGCM(shared_secret)
    nonce = encrypted_data[:12]
    ciphertext = encrypted_data[12:]
    return aesgcm.decrypt(nonce, ciphertext, None)


class WebEdgeHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            encrypted_request = self.rfile.read(length)

            if not encrypted_request:
                self.send_error(400, "Encrypted request required")
                return

            # Read client's ML-KEM encapsulation ciphertext from header
            ciphertext = base64.b64decode(
                self.headers["X-MLKEM-Ciphertext"],
                validate=True,
            )

            # Decapsulate to retrieve the shared secret
            shared_secret = WEBEDGE_PRIVATE_KEY.decapsulate(ciphertext)

            # Decrypt request path
            path = decrypt(
                encrypted_request,
                shared_secret,
            ).decode()

            target_url = f"http://{TARGET_HOST}:{TARGET_PORT}{path}"
            request = Request(target_url, method="GET")

            with urlopen(request) as response:
                body = response.read()
                status = response.status
                content_type = response.headers.get("Content-Type")

        except URLError:
            self.send_error(502, "Webedge could not reach target")
            return

        except (
            KeyError,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            binascii.Error,
        ):
            self.send_error(400, "Invalid ML-KEM request")
            return

        # Encrypt response with AES-GCM using the shared secret
        encrypted_response = encrypt(body, shared_secret)

        self.send_response(status)

        if content_type:
            self.send_header("Content-Type", content_type)

        self.send_header(
            "Content-Length",
            str(len(encrypted_response)),
        )
        self.end_headers()
        self.wfile.write(encrypted_response)

    def log_message(self, format, *args):
        pass


def run(host="localhost", port=8000):
    server = HTTPServer((host, port), WebEdgeHandler)

    print(f"Webedge (ML-KEM-768) listening on http://{host}:{port}")
    print(f"Forwarding requests to http://{TARGET_HOST}:{TARGET_PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping webedge...")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()