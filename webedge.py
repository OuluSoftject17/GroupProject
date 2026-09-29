from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import os


TARGET_HOST = os.getenv("WEBEDGE_TARGET_HOST", "localhost")
TARGET_PORT = int(os.getenv("WEBEDGE_TARGET_PORT", "8001"))


class WebEdgeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        target_url = f"http://{TARGET_HOST}:{TARGET_PORT}{self.path}"

        try:
            request = Request(target_url, method="GET")

            with urlopen(request) as response:
                body = response.read()

                self.send_response(response.status)

                content_type = response.headers.get("Content-Type")
                if content_type:
                    self.send_header("Content-Type", content_type)

                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        except HTTPError as error:
            body = error.read()

            self.send_response(error.code)

            content_type = error.headers.get("Content-Type")
            if content_type:
                self.send_header("Content-Type", content_type)

            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        except URLError as error:
            self.send_error(502, "Webedge could not reach target")


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