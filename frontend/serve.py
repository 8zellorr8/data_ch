"""Local frontend server with a fixed upstream API; no third-party dependencies."""
import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

HOSTED_API = 'https://replimetr.alwaysdata.net'
ROOT = Path(__file__).resolve().parent
MAX_BODY = 8 * 1024 * 1024

def validate_upstream(value, port):
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/') or not parsed.hostname:
        raise ValueError('--api must be an HTTP(S) origin without credentials or path')
    loopback = parsed.hostname in ('localhost', '127.0.0.1', '::1')
    if parsed.scheme == 'http' and not loopback:
        raise ValueError('Remote API requires HTTPS')
    if loopback and (parsed.port or (443 if parsed.scheme == 'https' else 80)) == port:
        raise ValueError('API cannot point back to this frontend server; choose the backend port')
    return value.rstrip('/')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class FrontendHandler(SimpleHTTPRequestHandler):
    upstream = HOSTED_API
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def is_api(self):
        path = urlsplit(self.path).path
        return path == '/health' or path.startswith('/api/v1/')

    def do_GET(self):
        if self.is_api():
            return self.proxy()
        return super().do_GET()

    def do_POST(self):
        if self.is_api():
            return self.proxy()
        self.send_error(405)

    def failure(self, code, message, status=502):
        body = json.dumps({'error': {'code': code, 'message': message}}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def proxy(self):
        # Only relative API paths and two methods are supported. Clients cannot
        # choose a host or inject upstream authentication/cookies.
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or '\\' in self.path:
            return self.failure('INVALID_PATH', 'Недопустимый путь запроса.', 400)
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.failure('INVALID_BODY', 'Неверная длина запроса.', 400)
        if length < 0 or length > MAX_BODY or self.headers.get('Transfer-Encoding'):
            return self.failure('BODY_TOO_LARGE', 'Размер запроса превышает 8 МБ.', 413)
        data = self.rfile.read(length) if self.command == 'POST' else None
        headers = {name: self.headers[name] for name in ('Content-Type', 'Idempotency-Key', 'X-Request-ID') if name in self.headers}
        request = Request(self.upstream + self.path, data=data, headers=headers, method=self.command)
        try:
            try:
                response = build_opener(NoRedirect()).open(request, timeout=190)
            except HTTPError as error:
                response = error
            with response:
                body = response.read()
                self.send_response(response.status)
                for name in ('Content-Type', 'X-Request-ID', 'X-Execution-ID', 'Retry-After'):
                    if response.headers.get(name):
                        self.send_header(name, response.headers[name])
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
        except (URLError, TimeoutError, OSError):
            # Never repeat POST automatically: the server may already have saved it.
            try:
                self.failure('UPSTREAM_UNAVAILABLE', 'Нет ответа сервера команды. При сохранении восстановите запрос с прежним ключом.')
            except (BrokenPipeError, ConnectionResetError):
                pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Replimetr frontend and API relay')
    parser.add_argument('--port', type=int, default=5173)
    parser.add_argument('--api', default=HOSTED_API)
    args = parser.parse_args()
    try:
        FrontendHandler.upstream = validate_upstream(args.api, args.port)
    except ValueError as error:
        parser.error(str(error))
    print(f'Replimetr: http://127.0.0.1:{args.port} | API: {FrontendHandler.upstream}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), FrontendHandler).serve_forever()
