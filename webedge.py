from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import base64
import binascii
import os

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes, serialization


TARGET_HOST = os.getenv("WEBEDGE_TARGET_HOST", "localhost")
TARGET_PORT = int(os.getenv("WEBEDGE_TARGET_PORT", "8001"))

WEBEDGE_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
)
WEBEDGE_PUBLIC_KEY = WEBEDGE_PRIVATE_KEY.public_key()


def encrypt(data, public_key):
    return public_key.encrypt(
        data,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )


def decrypt(data, private_key):
    return private_key.decrypt(
        data,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )


class WebEdgeHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            encrypted_request = self.rfile.read(length)

            if not encrypted_request:
                self.send_error(400, "Encrypted request required")
                return

            client_key_data = base64.b64decode(
                self.headers["X-Client-Public-Key"],
                validate=True,
            )
            client_public_key = serialization.load_der_public_key(
                client_key_data
            )

            path = decrypt(
                encrypted_request,
                WEBEDGE_PRIVATE_KEY,
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
            self.send_error(400, "Invalid RSA request")
            return

        encrypted_response = encrypt(body, client_public_key)

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

    print(f"Webedge listening on http://{host}:{port}")
    print(f"Forwarding requests to http://{TARGET_HOST}:{TARGET_PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping webedge...")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
