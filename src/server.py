"""Local development dashboard with CSV imports. Never expose directly to the Internet."""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
import csv
from io import StringIO
from .storage import SnapshotStore, DEFAULT_DB
from .quotes import QuoteStore
from .analytics import portfolio_analytics
from .performance import PerformanceStore, calculate_performance, HEADERS as PERFORMANCE_HEADERS
from .risk import RiskStore, derivatives_risk, HEADERS as RISK_HEADERS
from .report import report_data, report_html, report_csv, mars_report_html, mars_report_csv
from .mars import MarsStore, enrich_base_data

ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    store = None
    public_origin = None
    quote_store = None

    def reply(self, data, content_type='application/json', status=200, filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path=urlsplit(self.path).path
        if path in {'/reports/mars','/api/reports/mars.html','/api/reports/mars.csv'}:
            snapshot=MarsStore(self.store.path).latest() if self.store else None
            snapshot=enrich_base_data(snapshot,self.quote_store.view() if self.quote_store else {'quotes':[]})
            if not snapshot:
                self.reply({'error':'Import a MARS workbook before exporting its report'},status=400)
            elif path.endswith('.csv'):
                self.reply(mars_report_csv(snapshot),'text/csv; charset=utf-8',filename='mars-trader-report.csv')
            else:
                self.reply(mars_report_html(snapshot),'text/html; charset=utf-8',filename='mars-trader-report.html' if path.endswith('.html') else None)
            return
        if path in {'/api/templates/performance.csv','/api/templates/risk.csv'}:
            out=StringIO(); writer=csv.writer(out)
            writer.writerow(PERFORMANCE_HEADERS if 'performance' in path else RISK_HEADERS)
            self.reply(out.getvalue().encode('utf-8-sig'),'text/csv; charset=utf-8',filename=path.rsplit('/',1)[1])
            return
        if path in {'/api/reports/trader.html','/api/reports/trader.csv','/reports/trader'}:
            try:
                snapshot=self.store.latest() if self.store else None
                bloomberg=self.quote_store.view() if self.quote_store else {'quotes':[]}
                perf=calculate_performance(PerformanceStore(self.store.path).events() if self.store else [])
                risk=derivatives_risk(snapshot,RiskStore(self.store.path).terms() if self.store else [])
                query=parse_qs(urlsplit(self.path).query)
                data=report_data(snapshot,portfolio_analytics(snapshot,bloomberg),perf,risk,
                    query.get('size_currency',['EUR'])[0],query.get('pnl_currency',['CZK'])[0])
                if path.endswith('.csv'):
                    self.reply(report_csv(data),'text/csv; charset=utf-8',filename='trader-report-'+data['date']+'.csv')
                else:
                    self.reply(report_html(data),'text/html; charset=utf-8',
                        filename='trader-report-'+data['date']+'.html' if path.endswith('.html') else None)
            except ValueError as exc:
                self.reply({'error':str(exc)},status=400)
            return
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
                'analytics': portfolio_analytics(snapshot, bloomberg),
                'performance': calculate_performance(PerformanceStore(self.store.path).events() if self.store else []),
                'risk': derivatives_risk(snapshot,RiskStore(self.store.path).terms() if self.store else []),
                'risk_inputs_count': len(RiskStore(self.store.path).terms()) if self.store else 0,
                'mars': enrich_base_data(MarsStore(self.store.path).latest(),bloomberg) if self.store else None,
                'mars_snapshot_count': MarsStore(self.store.path).count() if self.store else 0,
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
        formats = {'/api/import/positions': 'ubs-positions', '/api/import/orders': 'ubs-orders',
                   '/api/import/performance':'desk-performance','/api/import/risk':'desk-risk','/api/import/mars':'bloomberg-mars'}
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
        expected_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if self.path=='/api/import/mars' else 'text/csv'
        if self.headers.get('Content-Type', '').split(';')[0] != expected_type:
            self.reply({'error': 'Unexpected import content type'}, status=415)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2 * 1024 * 1024:
                self.reply({'error': 'Import must be between 1 byte and 2 MiB'}, status=413)
                return
            raw = self.rfile.read(length)
            if self.path=='/api/import/mars':
                metadata=self.headers.get('X-Mars-Metadata','{}')
                if len(metadata)>2048: raise ValueError('MARS metadata too long')
                result=MarsStore(self.store.path).import_content(raw,json.loads(metadata))
                self.reply({key:result[key] for key in ['id','count','as_of','portfolio','already_imported']})
                return
            content = raw.decode('utf-8-sig')
            if self.path=='/api/import/performance':
                result=PerformanceStore(self.store.path).import_content(content)
            elif self.path=='/api/import/risk':
                result=RiskStore(self.store.path).import_content(content,self.store.latest())
            else:
                result = (self.store.import_content(content) if self.path == '/api/import/positions'
                          else self.store.import_orders_content(content))
        except (UnicodeError, ValueError) as exc:
            self.reply({'error': str(exc)}, status=400)
            return
        if self.path in {'/api/import/performance','/api/import/risk'}:
            self.reply(result)
        elif self.path == '/api/import/orders':
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
