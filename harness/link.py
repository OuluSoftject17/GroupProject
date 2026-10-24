"""
The link: the only way to reach the device from outside.

It listens on localhost and forwards only GET /health, / and /measurements to
the device.
"""

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED_PATHS = ("/health", "/", "/measurements")


class LinkHandler(BaseHTTPRequestHandler):
    # Any method other than GET has no do_* method, so the base class
    # answers 501 Not Implemented.

    def do_GET(self):
        link = self.server.link
        request_size = len(self.requestline) + len(str(self.headers))

        if self.path not in ALLOWED_PATHS:
            return self.reply(404, b"Blocked by link")
        if request_size > link["max_request_bytes"] or self.headers.get("Content-Length"):
            return self.reply(413, b"Request too large for link")

        try:
            status, content_type, body = self.forward(link)
        except OSError:
            return self.reply(504, b"Device did not answer")
        if len(body) > link["max_response_bytes"]:
            return self.reply(502, b"Device response too large for link")
        self.reply(status, body, content_type)

    def forward(self, link):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.device_port, timeout=link["device_timeout_s"])
        try:
            connection.request("GET", self.path)
            response = connection.getresponse()
            body = response.read(link["max_response_bytes"] + 1)
            return response.status, response.getheader("Content-Type", "text/plain"), body
        finally:
            connection.close()

    def reply(self, status, body, content_type="text/plain"):
        reason = self.responses.get(status, ("",))[0]
        head = (f"HTTP/1.0 {status} {reason}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\n\r\n")
        self.close_connection = True
        self.wfile.write(head.encode("ascii") + body)

    def log_message(self, format, *args):
        pass


def start_link(link_profile, device_port, port=0):
    """Start the link in a background thread. Returns the server."""
    server = ThreadingHTTPServer(("127.0.0.1", port), LinkHandler)
    server.daemon_threads = True
    server.link = link_profile
    server.device_port = device_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
