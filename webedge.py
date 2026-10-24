from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen
import base64
import binascii
import hashlib
import json
import logging
import os
import sys
import threading

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import mlkem, padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization


TARGET_HOST = os.getenv("WEBEDGE_TARGET_HOST", "localhost")
TARGET_PORT = int(os.getenv("WEBEDGE_TARGET_PORT", "8001"))
ALLOW_RSA = os.getenv("WEBEDGE_ALLOW_RSA", "true").lower() in ("1", "true", "yes")

RSA_PRIVATE_KEY_FILE = "webedge-rsa-private.pem"
RSA_PUBLIC_KEY_FILE = "webedge-rsa-public.pem"
KEM_PRIVATE_KEY_FILE = "webedge-kem-private.pem"
KEM_PUBLIC_KEY_FILE = "webedge-kem-public.pem"

# Temporary keys. run() replaces them with the keys from
# WEBEDGE_RSA_KEY_FILE and WEBEDGE_KEM_KEY_FILE when those are set.
WEBEDGE_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
)
WEBEDGE_PUBLIC_KEY = WEBEDGE_PRIVATE_KEY.public_key()

WEBEDGE_KEM_PRIVATE_KEY = mlkem.MLKEM768PrivateKey.generate()
WEBEDGE_KEM_PUBLIC_KEY = WEBEDGE_KEM_PRIVATE_KEY.public_key()

NONCE_SIZE = 12
HKDF_INFO = b"webedge-mlkem-v1"

logger = logging.getLogger("webedge")

METRICS = {}
METRICS_LOCK = threading.Lock()


class RequestError(Exception):

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class KeyLoadError(Exception):
    pass


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


def derive_key(shared_secret):
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=HKDF_INFO,
    ).derive(shared_secret)


def aead_encrypt(data, key):
    nonce = os.urandom(NONCE_SIZE)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def aead_decrypt(data, key):
    if len(data) <= NONCE_SIZE:
        raise ValueError("Encrypted data too short")

    return AESGCM(key).decrypt(data[:NONCE_SIZE], data[NONCE_SIZE:], None)


def forward(path):
    # Without this check a path such as "@evil.example/" would turn the
    # target URL into http://localhost:8001@evil.example/.
    if not path.startswith("/"):
        raise RequestError(400, "Invalid encrypted request")

    target_url = f"http://{TARGET_HOST}:{TARGET_PORT}{path}"

    try:
        request = Request(target_url, method="GET")

        with urlopen(request) as response:
            return (
                response.status,
                response.headers.get("Content-Type"),
                response.read(),
            )
    except URLError:
        raise RequestError(502, "Webedge could not reach target")
    except ValueError:
        raise RequestError(400, "Invalid encrypted request")


def record_request(method, status, path):
    with METRICS_LOCK:
        counts = METRICS.setdefault(method, {})
        counts[str(status)] = counts.get(str(status), 0) + 1

    logger.info(
        "key_exchange=%s status=%s path=%r",
        method,
        status,
        path,
    )


class WebEdgeHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/kem-public-key":
            self.send_body(
                200,
                "text/plain",
                base64.b64encode(WEBEDGE_KEM_PUBLIC_KEY.public_bytes_raw()),
            )
            return

        if self.path == "/metrics":
            with METRICS_LOCK:
                body = json.dumps(METRICS).encode()

            self.send_body(200, "application/json", body)
            return

        # Legacy clients do not send the header, so they default to RSA.
        method = self.headers.get("X-Key-Exchange", "rsa").strip().lower()
        path = None

        try:
            if method == "mlkem":
                path, encrypt_response = self.open_mlkem_request()
            elif method == "rsa":
                if not ALLOW_RSA:
                    raise RequestError(
                        403,
                        "RSA key exchange is disabled, use ML-KEM",
                    )
                path, encrypt_response = self.open_rsa_request()
            else:
                method = "unknown"
                raise RequestError(400, "Unknown key exchange")

            status, content_type, body = forward(path)

            try:
                encrypted_response = encrypt_response(body)
            except ValueError:
                raise RequestError(500, "Response could not be encrypted")

        except RequestError as error:
            record_request(method, error.status, path)
            self.send_error(error.status, error.message)
            return

        record_request(method, status, path)
        self.send_body(status, content_type, encrypted_response)

    def read_encrypted_request(self):
        length = int(self.headers.get("Content-Length", 0))

        if length <= 0:
            raise RequestError(400, "Encrypted request required")

        return self.rfile.read(length)

    def open_mlkem_request(self):
        try:
            encrypted_request = self.read_encrypted_request()

            kem_ciphertext = base64.b64decode(
                self.headers["X-KEM-Ciphertext"],
                validate=True,
            )
            shared_secret = WEBEDGE_KEM_PRIVATE_KEY.decapsulate(
                kem_ciphertext
            )
            key = derive_key(shared_secret)

            # A tampered KEM ciphertext still decapsulates (ML-KEM uses
            # implicit rejection), but to a different secret, so it fails
            # here as an InvalidTag.
            path = aead_decrypt(encrypted_request, key).decode()

        except (
            KeyError,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            binascii.Error,
            InvalidTag,
        ):
            raise RequestError(400, "Invalid encrypted request")

        return path, lambda body: aead_encrypt(body, key)

    def open_rsa_request(self):
        try:
            encrypted_request = self.read_encrypted_request()

            client_key_data = base64.b64decode(
                self.headers["X-Client-Public-Key"],
                validate=True,
            )
            client_public_key = serialization.load_der_public_key(
                client_key_data
            )

            if not isinstance(client_public_key, rsa.RSAPublicKey):
                raise ValueError("Client public key is not an RSA key")

            path = decrypt(
                encrypted_request,
                WEBEDGE_PRIVATE_KEY,
            ).decode()

        except (
            KeyError,
            ValueError,
            TypeError,
            UnicodeDecodeError,
            binascii.Error,
            UnsupportedAlgorithm,
        ):
            raise RequestError(400, "Invalid encrypted request")

        return path, lambda body: encrypt(body, client_public_key)

    def send_body(self, status, content_type, body):
        self.send_response(status)

        if content_type:
            self.send_header("Content-Type", content_type)

        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def kem_fingerprint(public_key):
    return hashlib.sha256(public_key.public_bytes_raw()).hexdigest()


def load_private_key(path, key_type):
    try:
        with open(path, "rb") as key_file:
            private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=None,
            )
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm) as error:
        raise KeyLoadError(f"Could not load key file {path}: {error}")

    if not isinstance(private_key, key_type):
        raise KeyLoadError(
            f"Key file {path} does not contain a {key_type.__name__}"
        )

    return private_key


def load_key_from_env(env_var, key_type):
    path = os.getenv(env_var)

    if not path:
        logger.warning(
            "%s is not set: using a temporary key that changes on every "
            "restart, so clients cannot pin it",
            env_var,
        )
        return None

    return load_private_key(path, key_type)


def load_keys():
    global WEBEDGE_PRIVATE_KEY, WEBEDGE_PUBLIC_KEY
    global WEBEDGE_KEM_PRIVATE_KEY, WEBEDGE_KEM_PUBLIC_KEY

    # Load both keys before changing anything, so a failure leaves the
    # current keys in place.
    rsa_key = load_key_from_env("WEBEDGE_RSA_KEY_FILE", rsa.RSAPrivateKey)
    kem_key = load_key_from_env(
        "WEBEDGE_KEM_KEY_FILE",
        mlkem.MLKEM768PrivateKey,
    )

    if rsa_key is not None:
        WEBEDGE_PRIVATE_KEY = rsa_key
        WEBEDGE_PUBLIC_KEY = rsa_key.public_key()

    if kem_key is not None:
        WEBEDGE_KEM_PRIVATE_KEY = kem_key
        WEBEDGE_KEM_PUBLIC_KEY = kem_key.public_key()

    logger.info(
        "ML-KEM public key fingerprint (SHA-256): %s",
        kem_fingerprint(WEBEDGE_KEM_PUBLIC_KEY),
    )


def write_new_file(path, data, mode):
    # O_EXCL makes the open fail if the file already exists, and mode
    # sets the permissions on POSIX. Windows ignores most of mode.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    file_descriptor = os.open(path, flags, mode)

    with os.fdopen(file_descriptor, "wb") as key_file:
        key_file.write(data)


def generate_keys(directory):
    rsa_private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    kem_private_key = mlkem.MLKEM768PrivateKey.generate()

    private_format = (
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_format = (
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    files = [
        (
            RSA_PRIVATE_KEY_FILE,
            rsa_private_key.private_bytes(*private_format),
            0o600,
        ),
        (
            RSA_PUBLIC_KEY_FILE,
            rsa_private_key.public_key().public_bytes(*public_format),
            0o644,
        ),
        (
            KEM_PRIVATE_KEY_FILE,
            kem_private_key.private_bytes(*private_format),
            0o600,
        ),
        (
            KEM_PUBLIC_KEY_FILE,
            kem_private_key.public_key().public_bytes(*public_format),
            0o644,
        ),
    ]

    os.makedirs(directory, exist_ok=True)

    existing = [
        name for name, _, _ in files
        if os.path.exists(os.path.join(directory, name))
    ]

    if existing:
        raise FileExistsError(
            f"Refusing to overwrite existing key files: {', '.join(existing)}"
        )

    for name, data, mode in files:
        write_new_file(os.path.join(directory, name), data, mode)

    return kem_fingerprint(kem_private_key.public_key())


def run(host="localhost", port=8000):
    logging.basicConfig(level=logging.INFO)

    try:
        load_keys()
    except KeyLoadError as error:
        raise SystemExit(f"Webedge refusing to start: {error}")

    server = HTTPServer((host, port), WebEdgeHandler)

    print(f"Webedge listening on http://{host}:{port}")
    print(f"Forwarding requests to http://{TARGET_HOST}:{TARGET_PORT}")
    print(f"RSA key exchange allowed: {ALLOW_RSA}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping webedge...")
    finally:
        server.server_close()


def main(argv):
    if len(argv) == 3 and argv[1] == "generate-keys":
        try:
            fingerprint = generate_keys(argv[2])
        except FileExistsError as error:
            print(error, file=sys.stderr)
            return 1

        print(f"Keys written to {argv[2]}")
        print(f"ML-KEM public key fingerprint (SHA-256): {fingerprint}")
        return 0

    if len(argv) != 1:
        print(
            "Usage: python webedge.py [generate-keys <directory>]",
            file=sys.stderr,
        )
        return 2

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
