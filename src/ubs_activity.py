"""UBS account exports: immutable cash postings and auditable balance proofs.

This source contains settlements, not a certified whole-portfolio P/L ledger.
Trade, booking and value dates remain distinct; export closing balances can
include postings booked after the export's transaction-date filter.
"""
import csv
import base64
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from io import StringIO
from pathlib import Path

from .performance import dec
from .positions import parse_positions
from .storage import has_table

HEADERS = ['Trade date', 'Trade time', 'Booking date', 'Value date', 'Currency',
           'Debit', 'Credit', 'Individual amount', 'Balance', 'Transaction no.',
           'Description1', 'Description2', 'Description3', 'Footnotes']
METADATA = ['Account number', 'IBAN', 'From', 'Until', 'Opening balance',
            'Closing balance', 'Valued in', 'Numbers of transactions in this period']
TRADES = {'Purchase Spot', 'Sale Spot', 'Purchase from issue', 'Buy to open',
          'Buy to close', 'Sell to open', 'Sell to close'}
DEPOSITS = {'New Investment Money Market', 'Increase Money Market',
            'Reduction Money Market', 'Repayment Money Market'}
INCOME = {'Dividend', 'Interest', 'Compensatory payments', 'Securities Lending',
          'Interest Pay. Money Market', 'CREDIT DUE TO CSDR'}
FEES = {'Balance closing of service prices', 'Custody Price', 'Third-Party Charges'}
PAYMENTS = {'credit', 'SEPA Credit', 'Special payment order'}
FX = {'Purchase FX Spot', 'Sale FX Spot', 'Purchase FX Forward', 'Sale FX Forward'}
NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
REFERENCE_FIELDS = {'valor', 'source_multiplier', 'source_expiry', 'source_strike', 'source_strike_currency'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def numeric(value, field):
    result = dec(value, field)
    text = format(result, 'f')
    return (text.rstrip('0').rstrip('.') if '.' in text else text) if result else '0'


def iso_date(value):
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('Dates must use YYYY-MM-DD')
    return value


def iban(value):
    return re.sub(r'\s', '', value).upper()


def terms(row):
    text = row['Description3']
    quantity = re.search(r'Number/Amt\.\s*(' + NUMBER + r')(?:\s+([A-Z]{3}))?(?=;|\s|$)', text)
    price = re.search(r'Transaction price:\s*(' + NUMBER + r')\s*(%|[A-Z]{3})(?=\s|;|$)', text)
    accrued = re.search(r'Accrued interest:\s*(' + NUMBER + r')\s+([A-Z]{3})(?=;|\s|$)', text)
    identifiers = re.findall(r'\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b', row['Description1'])
    serial = re.search(r'Serial no\.\s*([A-Za-z0-9-]+)', row['Description1'])
    valor = re.search(r';\s*(\d{6,9})(?=\s*;|\s*$)', row['Description1'])
    multiplier = re.search(r'Multiplier:\s*(' + NUMBER + r')(?=\s*;|\s*$)', row['Description1'])
    strike = re.search(r'Strike price:\s*([A-Z]{3})\s+(' + NUMBER + r')(?=\s|,|;|$)', row['Description1'])
    expiry = re.search(r';\s*(\d{4}-\d{2}-\d{2})\s*$', row['Description1'])
    return {
        'isin': identifiers[0] if len(set(identifiers)) == 1 else None,
        'quantity': numeric(quantity[1], 'number/amount') if quantity else None,
        'quantity_currency': quantity[2] if quantity else None,
        'price': numeric(price[1], 'transaction price') if price else None,
        'price_currency': price[2] if price and price[2] != '%' else None,
        'price_convention': ('percent' if price[2] == '%' else 'amount') if price else None,
        'accrued': numeric(accrued[1], 'accrued interest') if accrued else None,
        'accrued_currency': accrued[2] if accrued else None,
        'deposit_serial': serial[1] if serial else None,
        'valor': valor[1] if valor else None,
        'source_multiplier': numeric(multiplier[1], 'source multiplier') if multiplier else None,
        'source_strike': numeric(strike[2], 'source strike') if strike else None,
        'source_strike_currency': strike[1] if strike else None,
        'source_expiry': iso_date(expiry[1]) if expiry else None,
    }


def category(kind):
    if kind in TRADES: return 'security_trade'
    if kind in DEPOSITS: return 'deposit_principal'
    if kind in INCOME: return 'income'
    if kind in FEES: return 'fees'
    if kind in PAYMENTS: return 'transfer_review'
    if kind in FX: return 'fx_conversion'
    if kind == 'Interest calculation balance': return 'account_interest'
    if kind == 'variation margin futures': return 'futures_margin'
    if kind == 'Secur. Repay.': return 'security_redemption'
    if kind == 'OTC FI Rate Refix/Payment': return 'otc_settlement_review'
    return 'unclassified'


def parse_activity(content):
    reader = csv.reader(StringIO(content.lstrip('\ufeff')), delimiter=';', strict=True)
    metadata, rows, seen, headers = {}, [], set(), False
    try:
        for cells in reader:
            cells = [c.strip() for c in cells]
            if not any(cells): continue
            while cells and not cells[-1]: cells.pop()
            if not headers:
                if cells and cells[0] == 'Trade date':
                    if cells != HEADERS: raise ValueError('Unexpected UBS activity columns')
                    if set(metadata) != set(METADATA): raise ValueError('Missing UBS account metadata')
                    headers = True
                    continue
                if len(cells) != 2 or cells[0].rstrip(':') not in METADATA:
                    raise ValueError('Expected a UBS account activity export')
                key = cells[0].rstrip(':')
                if key in metadata: raise ValueError('Duplicate account metadata')
                metadata[key] = cells[1]
                continue
            if len(cells) > len(HEADERS) or len(cells) < 10:
                raise ValueError('Activity column count mismatch')
            cells += [''] * (len(HEADERS) - len(cells))
            raw = dict(zip(HEADERS, cells))
            if any(len(v) > 4096 or any(ord(c) < 32 for c in v) for v in raw.values()):
                raise ValueError('Activity fields must be short plain text')
            for name in ['Trade date', 'Booking date', 'Value date']: iso_date(raw[name])
            if not metadata['From'] <= raw['Trade date'] <= metadata['Until']:
                raise ValueError('Trade date outside export filter')
            if raw['Trade time'] and not re.fullmatch(r'\d{2}:\d{2}:\d{2}', raw['Trade time']):
                raise ValueError('Invalid trade time')
            if raw['Trade time']: datetime.strptime(raw['Trade time'], '%H:%M:%S')
            if raw['Currency'] != metadata['Valued in']:
                raise ValueError('Posting currency differs from account currency')
            if not raw['Transaction no.'] or len(raw['Transaction no.']) > 128:
                raise ValueError('Missing transaction number')
            if raw['Individual amount']:
                raise ValueError('Grouped individual-amount postings require a separate format')
            if not raw['Debit'] and not raw['Credit']:
                raise ValueError('Posting requires debit or credit')
            debit = dec(raw['Debit'] or '0', 'debit')
            credit = dec(raw['Credit'] or '0', 'credit')
            if debit and credit: raise ValueError('Ambiguous debit and credit on one posting')
            # Reversals can be a positive Debit or a negative Credit.
            amount = numeric(raw['Debit'] or raw['Credit'], 'posting amount')
            kind = (raw['Description1'] if raw['Description1'] in FEES | {'Interest calculation balance', 'variation margin futures'}
                    else raw['Description2'] or raw['Description1'])
            reversal = kind.endswith(';Reversal')
            base_type = kind.removesuffix(';Reversal')
            account = iban(metadata['IBAN'])
            row = {'id': digest([account, raw['Currency'], raw['Transaction no.'], kind]),
                   'account': account, 'currency': raw['Currency'],
                   'transaction_no': raw['Transaction no.'], 'event_type': kind,
                   'base_type': base_type, 'reversal': reversal, 'category': category(base_type),
                   'trade_date': raw['Trade date'], 'trade_time': raw['Trade time'] or None,
                   'booking_date': raw['Booking date'], 'value_date': raw['Value date'],
                   'amount': amount, 'description': raw['Description1'],
                   'reported_balance': numeric(raw['Balance'], 'posting balance'),
                   'raw': raw, **terms(raw)}
            if row['id'] in seen:
                raise ValueError('Ambiguous repeated transaction component; no rows imported')
            seen.add(row['id'])
            if row['category'] in {'security_trade', 'deposit_principal', 'security_redemption'}:
                if row['quantity'] is not None and Decimal(row['quantity']) <= 0:
                    raise ValueError('Security/deposit number must be positive')
            rows.append(row)
            if len(rows) > 10000: raise ValueError('Activity file exceeds 10,000 postings')
        if not headers: raise ValueError('Missing activity header')
        for name in ['From', 'Until']: iso_date(metadata[name])
        if metadata['From'] > metadata['Until']: raise ValueError('Reversed export dates')
        if not re.fullmatch(r'[A-Z]{3}', metadata['Valued in']): raise ValueError('Invalid account currency')
        if not metadata['Account number'] or not re.fullmatch(r'[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}', iban(metadata['IBAN'])):
            raise ValueError('Missing or invalid account identity')
        if not metadata['Numbers of transactions in this period'].isdigit(): raise ValueError('Invalid posting count')
        if int(metadata['Numbers of transactions in this period']) != len(rows): raise ValueError('Posting count differs from metadata')
        opening = dec(metadata['Opening balance'], 'opening balance')
        closing = dec(metadata['Closing balance'], 'closing balance')
        with localcontext() as context:
            context.prec = 110
            total = sum((Decimal(r['amount']) for r in rows), Decimal(0))
            if opening + total != closing: raise ValueError('Opening plus cash postings does not equal closing balance')
            def chain(ordered):
                running = opening
                for row in ordered:
                    running += Decimal(row['amount'])
                    if running != Decimal(row['reported_balance']): return False
                return running == closing
            if chain(list(reversed(rows))): order = 'newest_first'
            elif chain(rows): order = 'oldest_first'
            else: raise ValueError('Running cash balance chain does not reconcile')
        return {'id': hashlib.sha256(content.lstrip('\ufeff').encode()).hexdigest(),
                'source': 'ubs_account_activity', 'account': iban(metadata['IBAN']),
                'account_number': metadata['Account number'], 'currency': metadata['Valued in'],
                'from': metadata['From'], 'until': metadata['Until'], 'count': len(rows),
                'opening_balance': numeric(opening, 'opening balance'),
                'closing_balance': numeric(closing, 'closing balance'),
                'posting_total': str(total), 'balance_chain': 'reconciled', 'export_order': order,
                'date_filter': 'trade_date', 'rows': rows}
    except (ValueError, csv.Error) as exc:
        raise ValueError(f'UBS activity rejected near line {reader.line_num}: {exc}') from None


def economic_record(row):
    # Reported balances/wording may change in an overlapping export. Each export
    # preserves those original fields, while economic conflicts are never added.
    return {k: v for k, v in row.items() if k not in {'raw', 'reported_balance'} | REFERENCE_FIELDS}


class ActivityStore:
    def __init__(self, path): self.path = Path(path)

    def import_content(self, content):
        return self.import_batch([content])['files'][0]

    def import_batch(self, contents):
        if not isinstance(contents, list) or not 1 <= len(contents) <= 12:
            raise ValueError('Select between one and twelve UBS CSV files')
        parsed = []
        for content in contents:
            if isinstance(content,dict) and set(content)=={'pdf_base64'}:
                from .baseline import parse_statement
                try: raw=base64.b64decode(content['pdf_base64'],validate=True)
                except (ValueError,TypeError): raise ValueError('Invalid PDF encoding') from None
                parsed.append(('baseline',(parse_statement(raw),raw)))
                continue
            if not isinstance(content, str) or not 0 < len(content.encode()) <= 2 * 1024 * 1024:
                raise ValueError('Each UBS file must be between one byte and 2 MiB')
            if content.lstrip('\ufeff \r\n').startswith('Account number:;'):
                parsed.append(('activity', parse_activity(content)))
            else:
                snapshot = parse_positions(content)
                snapshot['id'] = hashlib.sha256(content.encode()).hexdigest()
                parsed.append(('positions', snapshot))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        results = []
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ubs_activity_records (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ubs_activity_exports (id TEXT PRIMARY KEY, imported_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, as_of TEXT NOT NULL, exported_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS opening_baselines (id TEXT PRIMARY KEY,as_of TEXT NOT NULL,payload TEXT NOT NULL,source_pdf BLOB NOT NULL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            for kind, item in parsed:
                if kind=='baseline':
                    data,raw=item
                    from .baseline import validate_baseline
                    validate_baseline(db,data)
                    cursor=db.execute('INSERT OR IGNORE INTO opening_baselines VALUES (?,?,?,?)',(data['id'],data['as_of'],json.dumps(data),raw))
                    results.append({'format':'baseline','count':len(data['positions']),'as_of':data['as_of'],'already_imported':cursor.rowcount==0})
                    continue
                if kind == 'positions':
                    cursor = db.execute('INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?)',
                                        (item['id'], item['as_of'], item['exported_at'], json.dumps(item)))
                    results.append({'format': kind, 'count': item['count'], 'as_of': item['as_of'],
                                    'already_imported': cursor.rowcount == 0})
                    continue
                added = 0
                for row in item['rows']:
                    record = economic_record(row)
                    prior = db.execute('SELECT payload FROM ubs_activity_records WHERE id=?', (row['id'],)).fetchone()
                    previous = json.loads(prior[0]) if prior else None
                    if previous and {k: v for k, v in previous.items() if k != 'description'} != {k: v for k, v in record.items() if k != 'description'}:
                        raise ValueError('Changed UBS transaction component; batch rejected. Review the broker correction.')
                    if not prior:
                        db.execute('INSERT INTO ubs_activity_records VALUES (?, ?)', (row['id'], json.dumps(record)))
                        added += 1
                cursor = db.execute('INSERT OR IGNORE INTO ubs_activity_exports VALUES (?, ?, ?)',
                                    (item['id'], datetime.now(timezone.utc).isoformat(), json.dumps(item)))
                results.append({'format': kind, 'count': item['count'], 'added': added,
                                'existing': item['count'] - added, 'currency': item['currency'],
                                'from': item['from'], 'until': item['until'],
                                'closing_balance': item['closing_balance'], 'balance_chain': item['balance_chain'],
                                'already_imported': cursor.rowcount == 0})
            from .baseline import portfolio_identity
            portfolios={portfolio_identity(json.loads(r[0])['portfolio']) for r in db.execute('SELECT payload FROM snapshots')}
            portfolios.update(portfolio_identity(json.loads(r[0])['portfolio']) for r in db.execute('SELECT payload FROM opening_baselines'))
            if len(portfolios)>1:raise ValueError('UBS opening statement and positions must belong to one portfolio; batch rejected')
        return {'files': results, 'status': 'Imported for reconciliation; whole-portfolio P/L remains unverified'}

    def state(self):
        if not self.path.exists(): return [], []
        with sqlite3.connect(self.path) as db:
            if not has_table(db, 'ubs_activity_records'): return [], []
            db.execute('BEGIN')
            records = [json.loads(r[0]) for r in db.execute('SELECT payload FROM ubs_activity_records')]
            exports = [json.loads(r[0]) for r in db.execute('SELECT payload FROM ubs_activity_exports ORDER BY rowid')]
        return records, exports

    def view(self, snapshot=None):
        with localcontext() as context:
            context.prec = 110
            return self._view(snapshot)

    def _view(self, snapshot=None):
        records, exports = self.state()
        if not exports: return None
        known_accounts = {e['account'] for e in exports}
        cash_positions = [p for p in (snapshot or {}).get('positions', []) if p['kind'] == 'cash_account']
        known_accounts.update(iban(p.get('raw', {}).get('IBAN', '')) for p in cash_positions)
        source_rows = {r['id']: r for e in exports for r in e['rows']}
        # Additional instrument references are reparsed from retained source
        # exports. They are reference data, not automatic risk/accounting terms.
        records = [r | {'counterparty_reference':source_rows[r['id']]['raw']['Description3'][:4096]} |
                   {k: v for k, v in terms(source_rows[r['id']]['raw']).items() if k in REFERENCE_FIELDS}
                   for r in records]
        latest = {}
        for e in exports:
            key = e['account'], e['currency']
            if key not in latest or (e['until'], e['from']) >= (latest[key]['until'], latest[key]['from']):
                latest[key] = e
        accounts = []
        for e in sorted(latest.values(), key=lambda v: (v['currency'], v['account'])):
            beyond = [r for r in e['rows'] if r['booking_date'] > e['until']]
            positions = [p for p in cash_positions if iban(p.get('raw', {}).get('IBAN', '')) == e['account'] and p['currency'] == e['currency']]
            match = {'status': 'No matching UBS cash snapshot', 'snapshot_as_of': (snapshot or {}).get('as_of')}
            if len(positions) == 1:
                p = positions[0]
                booked = Decimal(p['quantity_or_balance'])
                intraday = p.get('intraday_quantity_or_balance')
                close = Decimal(e['closing_balance'])
                status = 'Balance mismatch'
                if close == booked: status = 'Matches booked balance'
                elif intraday is not None and close == Decimal(intraday): status = 'Matches intraday balance only'
                match.update(status=status, booked_balance=str(booked), intraday_balance=intraday,
                             difference_to_booked=str(close - booked))
                cutoff = snapshot['as_of']
                if e['from'] <= cutoff <= e['until']:
                    later = [r for r in e['rows'] if r['booking_date'] > cutoff]
                    at_cutoff = close - sum((Decimal(r['amount']) for r in later), Decimal(0))
                    match.update(export_cash_excluding_later_bookings=str(at_cutoff),
                                 excluded_later_booking_amount=str(close - at_cutoff),
                                 matches_booked_after_excluding_later_bookings=at_cutoff == booked)
            accounts.append({k: e[k] for k in ['account', 'account_number', 'currency', 'from', 'until', 'count',
                                             'opening_balance', 'closing_balance', 'balance_chain', 'date_filter']} |
                            {'booked_beyond_filter_count': len(beyond), 'snapshot_match': match,
                             'coverage_intervals': sorted({(x['from'],x['until']) for x in exports
                                                          if (x['account'],x['currency'])==(e['account'],e['currency'])}),
                             'coverage_status': 'Transaction-date filter; not a certified booking-date cutoff'})
        pairs, grouped, totals, review, principal = [], defaultdict(list), defaultdict(lambda: defaultdict(Decimal)), [], defaultdict(Decimal)
        for r in records:
            totals[r['currency']][r['category']] += Decimal(r['amount'])
            if r['category'] == 'fx_conversion': grouped[r['transaction_no']].append(r)
            valid_principal = (r['quantity'] and r['deposit_serial'] and r['quantity_currency'] == r['currency']
                               and r['price_convention'] == 'percent' and r['price'] == '100')
            if r['category'] == 'deposit_principal' and valid_principal:
                sign = -1 if r['base_type'] in {'New Investment Money Market', 'Increase Money Market'} else 1
                if r['reversal']: sign *= -1
                principal[(r['account'], r['currency'], r['deposit_serial'])] -= sign * Decimal(r['quantity'])
            if ((r['category'] == 'deposit_principal' and not valid_principal) or
                    (r['category'] == 'security_trade' and (not (r['isin'] or r['valor']) or r['quantity'] is None or r['price'] is None))):
                review.append({'id': r['id'], 'currency': r['currency'], 'booking_date': r['booking_date'],
                               'amount': r['amount'], 'description': r['description'],
                               'classification': r['category'], 'reason': 'Missing instrument or price/quantity conventions; cash posting retained'})
            if r['category'] in {'transfer_review', 'unclassified', 'otc_settlement_review'}:
                target = re.search(r'Account no\. IBAN:\s*([^;]+)', source_rows[r['id']]['raw']['Description3'])
                internal = r['category'] == 'transfer_review' and target and iban(target[1]) in known_accounts
                review.append({'id': r['id'], 'currency': r['currency'], 'booking_date': r['booking_date'],
                               'amount': r['amount'], 'description': r['description'] if 'description' in r else r['event_type'],
                               'reason': 'Internal UBS transfer; matching posting required' if internal else
                               ('Confirm transfer crosses the whole UBS portfolio boundary' if r['category'] == 'transfer_review' else
                                'Instrument/classification review required'),
                               'classification': 'internal_transfer_candidate' if internal else r['category']})
        for number, legs in sorted(grouped.items()):
            valid = (len(legs) == 2 and len({r['currency'] for r in legs}) == 2 and
                     len({(r['trade_date'], r['value_date']) for r in legs}) == 1 and
                     Decimal(legs[0]['amount']) * Decimal(legs[1]['amount']) < 0)
            pairs.append({'transaction_no': number, 'status': 'Matched internal currency legs' if valid else 'Unmatched; review required',
                          'legs': [{k: r[k] for k in ['currency', 'amount', 'trade_date', 'booking_date', 'value_date', 'id']} for r in legs]})
        ledger = sorted(records, key=lambda r: (r['booking_date'], r['trade_date'], r['id']), reverse=True)
        warnings = [
            'Export cash balance proofs do not certify complete portfolio P/L or external-flow coverage.',
            'Deposit principal, trades and matched FX legs are internal asset movements, not portfolio funding.',
            'Trade-date filters, booking dates and value dates differ; settlement receivables/payables remain required.',
            'Futures margin and OTC payments need instrument allocation; contract notionals are not cash investments.',
            'Opening acquisition costs, year-start marks, independent historical FX and closing valuations remain required.',
            'PTD entries are an independent cross-check; they are not added to UBS executions.',
        ]
        if any(a['booked_beyond_filter_count'] for a in accounts):
            warnings.append('An export includes a posting booked after its Until date. Its closing balance cannot be used as that dated booked-cash close.')
        return {'source': 'UBS account activity', 'record_count': len(records), 'export_count': len(exports),
                'accounts': accounts, 'categories': dict(Counter(r['category'] for r in records)),
                'cash_posting_totals': {c: {k: str(v) for k, v in cats.items()} for c, cats in totals.items()},
                'fx_pairs': pairs, 'review': review,
                'deposit_principal_movements': [{'account': a, 'currency': c, 'serial': s, 'net_invested': str(v),
                    'status': 'Net movements only; opening principal not verified'} for (a, c, s), v in sorted(principal.items())],
                'ledger': ledger, 'warnings': warnings, 'portfolio_pnl_verified': False}


def activity_csv(view):
    from .report import spreadsheet
    out = StringIO()
    writer = csv.writer(out)
    columns = ['section', 'account', 'currency', 'trade_date', 'booking_date', 'value_date',
               'transaction_no', 'category', 'amount', 'isin', 'valor', 'source_number_or_amount', 'source_number_currency', 'price',
               'price_currency', 'price_convention', 'accrued', 'accrued_currency',
               'source_multiplier', 'source_expiry', 'source_strike', 'source_strike_currency', 'notes']
    writer.writerow(columns)
    def emit(row): writer.writerow([spreadsheet(row.get(k)) for k in columns])
    for a in view['accounts']:
        emit({'section': 'COVERAGE', 'account': a['account'], 'currency': a['currency'],
              'trade_date': a['from'], 'booking_date': a['until'], 'amount': a['closing_balance'],
              'notes': 'Export closing balance; trade-date filter; ' + a['snapshot_match']['status'] + '; ' + a['coverage_status']})
    for warning in view['warnings']:
        emit({'section': 'BASIS', 'notes': warning})
    for r in view['ledger']:
        emit(r | {'section': 'POSTING', 'source_number_or_amount': r['quantity'],
                  'source_number_currency': r['quantity_currency'],
                  'notes': r['event_type'] + '; ' + r['description']})
    return out.getvalue().encode('utf-8-sig')
