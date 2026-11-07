import json
from pathlib import Path
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serve import FrontendHandler, validate_upstream

class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                calls.append({'body': self.rfile.read(int(self.headers.get('Content-Length', '0'))), 'key': self.headers.get('Idempotency-Key'), 'cookie': self.headers.get('Cookie')})
                if self.path.endswith('/lost'):
                    self.close_connection = True
                    return
                data = json.dumps({'error': {'code': 'EVALUATION_FAILED', 'message': 'fixture'}}).encode()
                self.send_response(409)
                self.send_header('Content-Type', 'application/json')
                self.send_header('X-Execution-ID', 'execution-fixture')
                self.send_header('Retry-After', '60')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        upstream_url = f'http://127.0.0.1:{self.upstream.server_port}'
        class Proxy(FrontendHandler):
            upstream = upstream_url
            def log_message(self, *args): pass
        self.proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        self.url = f'http://127.0.0.1:{self.proxy.server_port}'
        for server in (self.upstream, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.proxy, self.upstream):
            server.shutdown()
            server.server_close()

    def test_exact_post_key_error_and_diagnostics_forwarded(self):
        body = b'{"conversation_id":"fixture"}'
        request = Request(self.url+'/api/v1/evaluations', data=body, headers={'Content-Type':'application/json','Idempotency-Key':'same-key','Cookie':'local-cookie-not-for-upstream'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        response = caught.exception
        self.assertEqual(response.code, 409)
        self.assertEqual(response.headers['X-Execution-ID'], 'execution-fixture')
        self.assertEqual(response.headers['Retry-After'], '60')
        self.assertEqual(json.load(response)['error']['code'], 'EVALUATION_FAILED')
        response.close()
        self.assertEqual(self.calls, [{'body':body,'key':'same-key','cookie':None}])

    def test_lost_upstream_response_does_not_repeat_post(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(self.url+'/api/v1/lost', data=b'{}'), timeout=5)
        self.assertEqual(caught.exception.code, 502)
        self.assertEqual(json.load(caught.exception)['error']['code'], 'UPSTREAM_UNAVAILABLE')
        caught.exception.close()
        self.assertEqual(len(self.calls), 1)

    def test_static_post_is_not_proxied(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(self.url+'/index.html', data=b'{}'), timeout=5)
        self.assertEqual(caught.exception.code, 405)
        caught.exception.close()
        self.assertEqual(self.calls, [])

class UpstreamValidationTests(unittest.TestCase):
    def test_self_proxy_and_remote_http_are_rejected(self):
        for address in ('http://localhost:5173','http://127.0.0.1:5173','http://[::1]:5173','http://remote.example'):
            with self.assertRaises(ValueError): validate_upstream(address, 5173)
        self.assertEqual(validate_upstream('http://127.0.0.1:8000/',5173),'http://127.0.0.1:8000')
        self.assertEqual(validate_upstream('https://replimetr.alwaysdata.net',5173),'https://replimetr.alwaysdata.net')

if __name__ == '__main__': unittest.main()
