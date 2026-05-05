import http.server
import json
import os
import socketserver
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

PORT = int(os.environ.get("PORT", 4404))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

REQUEST_TIMEOUT_SECONDS = int(os.environ.get("PROXY_TIMEOUT_SECONDS", 30))
MAX_PROXY_BODY_BYTES = int(os.environ.get("MAX_PROXY_BODY_BYTES", 1024 * 1024))
MAX_PROXY_RESPONSE_BYTES = int(os.environ.get("MAX_PROXY_RESPONSE_BYTES", 5 * 1024 * 1024))
ALLOWED_PROXY_HOSTS = tuple(
    host.strip().lower()
    for host in os.environ.get("ALLOWED_PROXY_HOSTS", "mijia.tech,api.io.mi.com").split(",")
    if host.strip()
)

BLOCKED_PROXY_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "accept-encoding",
}

ALLOWED_PROXY_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


class ProxyRequestError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def send_json(handler, status_code, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def is_allowed_proxy_host(hostname):
    if not hostname:
        return False

    hostname = hostname.rstrip(".").lower()
    return any(
        hostname == allowed_host or hostname.endswith(f".{allowed_host}")
        for allowed_host in ALLOWED_PROXY_HOSTS
    )


def validate_proxy_url(target_url):
    if not isinstance(target_url, str) or not target_url:
        raise ProxyRequestError(400, "Missing proxy target URL")

    parsed_url = urlsplit(target_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ProxyRequestError(400, "Only absolute http/https URLs are allowed")

    if not is_allowed_proxy_host(parsed_url.hostname):
        allowed_hosts = ", ".join(ALLOWED_PROXY_HOSTS)
        raise ProxyRequestError(403, f"Proxy target host is not allowed. Allowed hosts: {allowed_hosts}")

    return parsed_url._replace(fragment="").geturl()


def sanitize_proxy_headers(headers):
    if not isinstance(headers, dict):
        return {}

    sanitized_headers = {}
    for name, value in headers.items():
        if not isinstance(name, str):
            continue

        normalized_name = name.strip()
        lower_name = normalized_name.lower()
        if (
            not normalized_name
            or lower_name in BLOCKED_PROXY_HEADERS
            or ":" in normalized_name
            or "\r" in normalized_name
            or "\n" in normalized_name
        ):
            continue

        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            normalized_value = ", ".join(str(item) for item in value)
        else:
            normalized_value = str(value)

        if "\r" in normalized_value or "\n" in normalized_value:
            continue

        sanitized_headers[normalized_name] = normalized_value

    return sanitized_headers


def read_limited_response(response):
    data = response.read(MAX_PROXY_RESPONSE_BYTES + 1)
    if len(data) > MAX_PROXY_RESPONSE_BYTES:
        raise ProxyRequestError(502, "Proxy response is too large")
    return data


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.path = "/mijia-geek.html"
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/proxy":
            self.send_error(404, "Not Found")
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            send_json(self, 400, {"error": "Invalid Content-Length"})
            return

        if content_length > MAX_PROXY_BODY_BYTES:
            send_json(self, 413, {"error": "Proxy request body is too large"})
            return

        post_data = self.rfile.read(content_length)

        try:
            req_json = json.loads(post_data.decode("utf-8"))
            target_url = validate_proxy_url(req_json.get("url"))
            method = str(req_json.get("method", "POST")).upper()
            if method not in ALLOWED_PROXY_METHODS:
                raise ProxyRequestError(400, f"HTTP method is not allowed: {method}")

            headers = sanitize_proxy_headers(req_json.get("headers", {}))
            data = req_json.get("data", "")
            request_body = data.encode("utf-8") if data and method != "GET" else None
            req = urllib.request.Request(
                target_url,
                data=request_body,
                headers=headers,
                method=method,
            )

            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    res_data = read_limited_response(response)
                    res_headers = dict(response.headers)
                    status = response.status
            except urllib.error.HTTPError as e:
                res_data = read_limited_response(e)
                res_headers = dict(e.headers)
                status = e.code
            except ProxyRequestError:
                raise
            except Exception as e:
                send_json(self, 502, {"error": str(e)})
                return

            send_json(
                self,
                200,
                {
                    "statusCode": status,
                    "headers": res_headers,
                    "data": res_data.decode("utf-8", errors="ignore"),
                },
            )
        except ProxyRequestError as e:
            send_json(self, e.status_code, {"error": e.message})
        except Exception as e:
            send_json(self, 400, {"error": "Invalid JSON or proxy failure: " + str(e)})


with ThreadingHTTPServer(("", PORT), ProxyHandler) as httpd:
    print("Serving at port", PORT)
    sys.stdout.flush()
    httpd.serve_forever()
