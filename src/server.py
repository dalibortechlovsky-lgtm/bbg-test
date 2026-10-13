"""Local development dashboard with CSV imports. Never expose directly to the Internet."""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .storage import SnapshotStore, DEFAULT_DB
from .quotes import QuoteStore

ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    store = None
    public_origin = None
    quote_store = None

    def reply(self, data, content_type='application/json', status=200):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/health':
            if self.store:
                self.store.latest()
                self.store.orders()
            self.reply({'status': 'ok', 'execution_enabled': False})
        elif self.path == '/api/status':
            bloomberg = self.quote_store.view() if self.quote_store else {'status': 'Not connected', 'quotes': [], 'devices': []}
            snapshot = self.store.latest() if self.store else None
            public = None if snapshot is None else {key: value for key, value in snapshot.items() if key != 'positions'}
            positions = [] if snapshot is None else [
                {key: value for key, value in row.items() if key != 'raw'} for row in snapshot['positions']]
            self.reply({
                'mode': 'development',
                'asset_classes': ['Equities', 'Equity derivatives', 'Bonds', 'FX'],
                'bloomberg': bloomberg['status'],
                'bloomberg_data': bloomberg,
                'bridge_tokens': self.quote_store.devices() if self.quote_store else [],
                'ubs': f"Snapshot as of {snapshot['as_of']} · Not live" if snapshot else 'No snapshot imported',
                'execution_enabled': False,
                'snapshot': public,
                'positions': positions,
                'trades': self.store.orders() if self.store else [],
                'order_imports': self.store.order_imports() if self.store else [],
            })
        elif self.path == '/api/bridge/watchlist' and self.quote_store:
            if not self.quote_store.authenticate(self.headers.get('Authorization')):
                self.reply({'error': 'Valid bridge token required'}, status=401)
            else:
                self.reply(self.quote_store.watchlist())
        elif self.path == '/bridge/bloomberg_bridge.py':
            self.reply((ROOT / 'bridge' / 'bloomberg_bridge.py').read_bytes(), 'text/x-python; charset=utf-8')
        elif self.path in ('/', '/index.html'):
            self.reply((ROOT / 'web' / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        else:
            self.send_error(404)
            return

    def do_POST(self):
        if self.path.startswith('/api/bridge/') and self.quote_store:
            self.bridge_post()
            return
        formats = {'/api/import/positions': 'ubs-positions', '/api/import/orders': 'ubs-orders'}
        if self.path not in formats or not self.store:
            self.send_error(404)
            return
        # Custom header and no CORS prevent a cross-origin browser form from importing.
        origin = self.headers.get('Origin')
        host = self.headers.get('Host')
        if (self.headers.get('X-Import-Format') != formats[self.path] or
                (origin is not None and origin != (self.public_origin or f'http://{host}'))):
            self.reply({'error': 'Import must originate from the local dashboard'}, status=403)
            return
        if self.headers.get('Content-Type', '').split(';')[0] != 'text/csv':
            self.reply({'error': 'Expected text/csv'}, status=415)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2 * 1024 * 1024:
                self.reply({'error': 'CSV must be between 1 byte and 2 MiB'}, status=413)
                return
            content = self.rfile.read(length).decode('utf-8-sig')
            result = (self.store.import_content(content) if self.path == '/api/import/positions'
                      else self.store.import_orders_content(content))
        except (UnicodeError, ValueError) as exc:
            self.reply({'error': str(exc)}, status=400)
            return
        if self.path == '/api/import/orders':
            self.reply(result, status=200 if result['accepted'] else 409)
        else:
            self.reply({key: result[key] for key in ['count', 'as_of', 'already_imported']})

    def read_json(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('Expected application/json')
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 1024 * 1024:
            raise ValueError('JSON body must be between 1 byte and 1 MiB')
        def nonfinite(value):
            raise ValueError('Non-finite JSON number')
        return json.loads(self.rfile.read(length).decode('utf-8'), parse_constant=nonfinite)

    def bridge_post(self):
        try:
            if self.path == '/api/bridge/quotes':
                device = self.quote_store.authenticate(self.headers.get('Authorization'))
                if not device:
                    self.reply({'error': 'Valid bridge token required'}, status=401)
                    return
                if self.headers.get('Origin') is not None:
                    self.reply({'error': 'Bridge uploads are for the Windows worker'}, status=403)
                    return
                self.reply(self.quote_store.ingest(device, self.read_json()))
                return
            if self.path not in {'/api/bridge/token', '/api/bridge/revoke'}:
                self.send_error(404)
                return
            origin = self.headers.get('Origin')
            expected = self.public_origin or 'http://' + self.headers.get('Host', '')
            if self.headers.get('X-Bridge-Action') != 'manage' or (origin is not None and origin != expected):
                self.reply({'error': 'Use bridge management in the dashboard'}, status=403)
                return
            payload = self.read_json()
            if not isinstance(payload, dict):
                raise ValueError('Expected a JSON object')
            if self.path == '/api/bridge/revoke':
                identifier = payload.get('id')
                if not isinstance(identifier, str):
                    raise ValueError('Device ID required')
                self.reply({'revoked': self.quote_store.revoke(identifier)})
            else:
                device = self.quote_store.create_token(payload.get('name', 'Windows Terminal'))
                self.reply({'id': device['id'], 'expires_at': device['expires_at'], 'config': {
                    'device_id': device['id'],
                    'endpoint': expected + '/api/bridge/quotes',
                    'watchlist_url': expected + '/api/bridge/watchlist', 'token': device['token']}})
        except PermissionError:
            self.reply({'error': 'Bridge token is no longer valid'}, status=401)
        except (ValueError, UnicodeError) as exc:
            self.reply({'error': str(exc)}, status=400)


def create_server(port=8000, db=DEFAULT_DB, host='127.0.0.1', public_origin=None):
    if public_origin:
        origin = urlsplit(public_origin)
        if (origin.scheme != 'https' or not origin.hostname or origin.username or origin.password
                or origin.path or origin.query or origin.fragment):
            raise ValueError('Public origin must be an HTTPS origin without path or credentials')
    if host not in ('127.0.0.1', 'localhost', '::1') and not public_origin:
        raise ValueError('Non-loopback binding requires PUBLIC_ORIGIN and a private reverse-proxy network')
    class ConfiguredHandler(Handler):
        store = SnapshotStore(db)
        quote_store = QuoteStore(store.path)
    ConfiguredHandler.public_origin = public_origin
    return ThreadingHTTPServer((host, port), ConfiguredHandler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--public-origin', default=os.environ.get('PUBLIC_ORIGIN'))
    args = parser.parse_args()
    create_server(args.port, args.db, args.host, args.public_origin).serve_forever()


if __name__ == '__main__':
    main()
