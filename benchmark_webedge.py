"""Benchmark the key exchange in webedge.py: RSA against ML-KEM.

By default only the cryptographic work of one request is timed, on the
client side and on the webedge side, by calling the functions in
webedge.py. HTTP and the target are left out so that the two methods are
compared on their cryptography alone. --http also times whole requests
through a local webedge and sensor.

Every run prints the report and also saves it as a Markdown file in
benchmark-results/ (or --output-dir), named after the time of the run.

Usage: python benchmark_webedge.py [--http] [--iterations N] [--warmup N]
"""

import argparse
import base64
import datetime
import gc
import json
import os
import platform
import statistics
import sys
import threading
import time
from http.server import HTTPServer
from urllib.request import Request, urlopen

import cryptography
from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import mlkem, rsa

import sensor
import webedge


PATH = "/measurements"
DEFAULT_OUTPUT_DIR = "benchmark-results"


def measure(operation, iterations, warmup):
    for _ in range(warmup):
        operation()

    # A garbage collection pause inside one sample would show up as an
    # outlier that has nothing to do with the cryptography.
    gc_was_enabled = gc.isenabled()
    gc.disable()

    try:
        samples = []

        for _ in range(iterations):
            start = time.perf_counter_ns()
            operation()
            samples.append(time.perf_counter_ns() - start)
    finally:
        if gc_was_enabled:
            gc.enable()

    return samples


def summarize(samples_ns):
    samples = [sample / 1_000_000 for sample in samples_ns]

    return {
        "median": statistics.median(samples),
        "p95": statistics.quantiles(samples, n=100, method="inclusive")[94],
        "mean": statistics.mean(samples),
        "stdev": statistics.stdev(samples),
        "min": min(samples),
    }


def sample_response():
    # A real sensor reading, so the response has a realistic size.
    return json.dumps(sensor.SaunaSensor(seed=1).read()).encode("utf-8")


def rsa_method(response_body):
    # The client side reuses webedge.encrypt and webedge.decrypt, since
    # they are plain RSA-OAEP with the same parameters the client uses.
    client_private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    client_key_header = base64.b64encode(
        client_private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    request_body = webedge.encrypt(PATH.encode(), webedge.WEBEDGE_PUBLIC_KEY)
    response = webedge.encrypt(response_body, client_private_key.public_key())

    def client_request():
        webedge.encrypt(PATH.encode(), webedge.WEBEDGE_PUBLIC_KEY)

    # Same steps as WebEdgeHandler.open_rsa_request.
    def webedge_request():
        client_public_key = serialization.load_der_public_key(
            base64.b64decode(client_key_header, validate=True)
        )
        webedge.decrypt(request_body, webedge.WEBEDGE_PRIVATE_KEY).decode()
        return client_public_key

    def webedge_response():
        webedge.encrypt(response_body, client_private_key.public_key())

    def client_response():
        webedge.decrypt(response, client_private_key)

    def whole_request():
        client_request()
        client_public_key = webedge_request()
        webedge.decrypt(
            webedge.encrypt(response_body, client_public_key),
            client_private_key,
        )

    return {
        "name": "RSA-2048 OAEP",
        "operations": [
            ("Client: encrypt request", client_request),
            ("Webedge: open request", webedge_request),
            ("Webedge: encrypt response", webedge_response),
            ("Client: decrypt response", client_response),
            ("Whole request (all four)", whole_request),
        ],
        "sizes": {
            "Request body": len(request_body),
            "Key header": len(client_key_header),
            "Response body": len(response),
        },
    }


def mlkem_method(response_body):
    public_key = webedge.WEBEDGE_KEM_PUBLIC_KEY
    shared_secret, kem_ciphertext = public_key.encapsulate()
    key = webedge.derive_key(shared_secret)
    kem_header = base64.b64encode(kem_ciphertext)
    request_body = webedge.aead_encrypt(PATH.encode(), key)
    response = webedge.aead_encrypt(response_body, key)

    # The ML-KEM client sends a new ciphertext with every request, so
    # encoding the header is part of the per-request work. The RSA client
    # header is its fixed public key and is encoded once.
    def client_request():
        shared_secret, kem_ciphertext = public_key.encapsulate()
        base64.b64encode(kem_ciphertext)
        key = webedge.derive_key(shared_secret)
        webedge.aead_encrypt(PATH.encode(), key)
        return key

    # Same steps as WebEdgeHandler.open_mlkem_request.
    def webedge_request():
        shared_secret = webedge.WEBEDGE_KEM_PRIVATE_KEY.decapsulate(
            base64.b64decode(kem_header, validate=True)
        )
        key = webedge.derive_key(shared_secret)
        webedge.aead_decrypt(request_body, key).decode()
        return key

    def webedge_response():
        webedge.aead_encrypt(response_body, key)

    def client_response():
        webedge.aead_decrypt(response, key)

    def whole_request():
        client_request()
        key = webedge_request()
        webedge.aead_decrypt(webedge.aead_encrypt(response_body, key), key)

    return {
        "name": "ML-KEM-768",
        "operations": [
            ("Client: encrypt request", client_request),
            ("Webedge: open request", webedge_request),
            ("Webedge: encrypt response", webedge_response),
            ("Client: decrypt response", client_response),
            ("Whole request (all four)", whole_request),
        ],
        "sizes": {
            "Request body": len(request_body),
            "Key header": len(kem_header),
            "Response body": len(response),
        },
    }


def keygen_operations():
    return [
        (
            "RSA-2048",
            lambda: rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            ),
        ),
        ("ML-KEM-768", mlkem.MLKEM768PrivateKey.generate),
    ]


def start_server(address, handler):
    server = HTTPServer(address, handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def http_operations():
    client_private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    client_key_header = base64.b64encode(
        client_private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode()

    target = start_server(("127.0.0.1", 0), sensor.SaunaRequestHandler)
    original_target = (webedge.TARGET_HOST, webedge.TARGET_PORT)
    webedge.TARGET_HOST = "127.0.0.1"
    webedge.TARGET_PORT = target.server_address[1]
    edge = start_server(("127.0.0.1", 0), webedge.WebEdgeHandler)
    url = f"http://127.0.0.1:{edge.server_address[1]}/"

    def rsa_request():
        request = Request(
            url,
            data=webedge.encrypt(PATH.encode(), webedge.WEBEDGE_PUBLIC_KEY),
            method="GET",
        )
        request.add_header("X-Client-Public-Key", client_key_header)

        with urlopen(request) as response:
            webedge.decrypt(response.read(), client_private_key)

    def mlkem_request():
        shared_secret, kem_ciphertext = (
            webedge.WEBEDGE_KEM_PUBLIC_KEY.encapsulate()
        )
        key = webedge.derive_key(shared_secret)
        request = Request(
            url,
            data=webedge.aead_encrypt(PATH.encode(), key),
            method="GET",
        )
        request.add_header("X-Key-Exchange", "mlkem")
        request.add_header(
            "X-KEM-Ciphertext",
            base64.b64encode(kem_ciphertext).decode(),
        )

        with urlopen(request) as response:
            webedge.aead_decrypt(response.read(), key)

    def stop():
        for server in (edge, target):
            server.shutdown()
            server.server_close()

        webedge.TARGET_HOST, webedge.TARGET_PORT = original_target

    return [("RSA-2048", rsa_request), ("ML-KEM-768", mlkem_request)], stop


def stats_table(title, rows):
    lines = [
        f"### {title}",
        "",
        "| Operation | Method | Median (ms) | p95 (ms) | Mean (ms) "
        "| Stdev (ms) | Min (ms) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]

    for operation, method, stats in rows:
        lines.append(
            f"| {operation} | {method} | {stats['median']:.4f} "
            f"| {stats['p95']:.4f} | {stats['mean']:.4f} "
            f"| {stats['stdev']:.4f} | {stats['min']:.4f} |"
        )

    return "\n".join(lines)


def sizes_table(methods):
    rsa_sizes, mlkem_sizes = (method["sizes"] for method in methods)
    lines = [
        "### Bytes on the wire per request",
        "",
        "Base64 header values and raw bodies. HTTP framing is not counted.",
        "",
        f"| Item | {methods[0]['name']} | {methods[1]['name']} | Ratio |",
        "| --- | ---: | ---: | ---: |",
    ]

    for item in list(rsa_sizes) + ["Total"]:
        if item == "Total":
            rsa_size = sum(rsa_sizes.values())
            mlkem_size = sum(mlkem_sizes.values())
        else:
            rsa_size = rsa_sizes[item]
            mlkem_size = mlkem_sizes[item]

        lines.append(
            f"| {item} | {rsa_size} | {mlkem_size} "
            f"| {mlkem_size / rsa_size:.2f} |"
        )

    return "\n".join(lines)


def environment():
    return "\n".join([
        "### Environment",
        "",
        f"- Python: {platform.python_version()} "
        f"({platform.python_implementation()})",
        f"- cryptography: {cryptography.__version__}",
        f"- Backend: {backend.openssl_version_text()}",
        f"- OS: {platform.platform()}",
        f"- CPU: {platform.processor() or platform.machine()}",
    ])


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Benchmark RSA against ML-KEM in webedge.py.",
    )
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument(
        "--keygen-iterations",
        type=int,
        default=50,
        help="RSA key generation is slow, so it gets fewer samples",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="also time whole HTTP requests through a local webedge",
    )
    parser.add_argument("--http-iterations", type=int, default=500)
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"where the report is saved (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args(argv)

    for name in ("iterations", "keygen_iterations", "http_iterations"):
        if getattr(args, name) < 2:
            parser.error(f"--{name.replace('_', '-')} must be at least 2")

    return args


def save_report(report, directory, started):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(
        directory,
        f"webedge-benchmark-{started:%Y-%m-%d_%H-%M-%S}.md",
    )

    # "x" refuses to overwrite, so two runs never silently share a file.
    with open(path, "x", encoding="utf-8") as report_file:
        report_file.write(report + "\n")

    return path


def main(argv):
    args = parse_args(argv)
    started = datetime.datetime.now().astimezone()
    response_body = sample_response()
    methods = [rsa_method(response_body), mlkem_method(response_body)]

    request_rows = []

    for index in range(len(methods[0]["operations"])):
        for method in methods:
            operation_name, operation = method["operations"][index]
            samples = measure(operation, args.iterations, args.warmup)
            request_rows.append(
                (operation_name, method["name"], summarize(samples))
            )

    keygen_rows = [
        ("Key generation", name, summarize(
            measure(operation, args.keygen_iterations, 1)
        ))
        for name, operation in keygen_operations()
    ]

    sections = [
        "## Webedge key exchange benchmark",
        "",
        f"Run on {started:%Y-%m-%d %H:%M:%S} (UTC{started:%z}).",
        "",
        f"{args.iterations} timed runs after {args.warmup} warm-up runs per "
        f"operation; key generation {args.keygen_iterations} runs. "
        f"Path {PATH!r}, response {len(response_body)} bytes.",
        "",
        environment(),
        "",
        stats_table("Cryptography per request", request_rows),
        "",
        stats_table("Key generation (once per key)", keygen_rows),
        "",
        sizes_table(methods),
    ]

    if args.http:
        operations, stop = http_operations()

        try:
            http_rows = [
                ("Whole HTTP request", name, summarize(
                    measure(operation, args.http_iterations, args.warmup)
                ))
                for name, operation in operations
            ]
        finally:
            stop()

        sections += [
            "",
            stats_table(
                "Whole HTTP request through webedge and sensor (localhost)",
                http_rows,
            ),
        ]

    report = "\n".join(sections)
    print(report)

    path = save_report(report, args.output_dir, started)
    print(f"\nReport saved to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
