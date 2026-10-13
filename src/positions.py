"""UBS sectioned positions export: preserve broker values without repricing."""
import argparse
import csv
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path

COMMON = {'Banking relationship', 'Portfolio', 'Group of products', 'Product',
          'Ccy.', 'Number/Amt.', 'Intraday number/amount', 'Valor', 'ISIN',
          'Description', 'Market value', 'Accrued interest'}


def number(value, optional=False):
    if not value and optional:
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError('Invalid numeric field') from None
    if not result.is_finite():
        raise ValueError('Non-finite numeric field')
    return str(result)


def quote(value):
    if not value:
        return None
    percent = value.endswith('%')
    return {'value': number(value[:-1] if percent else value),
            'convention': 'percent' if percent else 'amount'}


def parse_positions(content):
    headers = None
    rows, metadata, fx, identities = [], {}, {}, set()
    footer = False
    portfolio = None
    reader = csv.reader(StringIO(content.lstrip('\ufeff')), delimiter=';', strict=True)
    try:
        for line, cells in enumerate(reader, start=1):
            cells = [cell.strip() for cell in cells]
            if not any(cells):
                continue
            first = cells[0]
            if first == 'Banking relationship':
                if footer:
                    raise ValueError('Position section after footer')
                while cells and not cells[-1]:
                    cells.pop()
                if any(not cell for cell in cells) or len(set(cells)) != len(cells):
                    raise ValueError('Blank or duplicate section columns')
                if not COMMON.issubset(cells):
                    raise ValueError('Missing required position columns')
                headers = cells
                continue
            if first == 'Exchange rates':
                footer = True
                continue
            if ':' in first and first.split(':', 1)[0] in {
                'Portfolio number', 'Valued as of', 'Valued in',
                'Export created on', 'Number of positions',
            }:
                footer = True
                key, value = first.split(':', 1)
                if any(cells[1:]) or key in metadata:
                    raise ValueError('Invalid or duplicate footer metadata')
                metadata[key] = value.strip()
                continue
            if footer:
                if len(cells) < 4 or any(cells[4:]):
                    raise ValueError('Unrecognized footer row')
                for pair, rate in zip(cells[:4:2], cells[1:4:2]):
                    parts = pair.split('/')
                    if len(parts) != 2 or any(len(p) != 3 or not p.isalpha() or not p.isupper() for p in parts):
                        raise ValueError('Invalid exchange-rate pair')
                    if pair in fx or Decimal(number(rate)) <= 0:
                        raise ValueError('Duplicate or invalid exchange rate')
                    fx[pair] = number(rate)
                continue
            if headers is None:
                raise ValueError('Missing section header')
            if len(cells) < len(headers) or any(cells[len(headers):]):
                raise ValueError('Position column count differs from section header')
            row = dict(zip(headers, cells))
            for key in ['Banking relationship', 'Portfolio', 'Group of products',
                        'Product', 'Ccy.', 'Description']:
                if not row[key]:
                    raise ValueError('Required position identity missing')
            if len(row['Ccy.']) != 3 or not row['Ccy.'].isalpha() or not row['Ccy.'].isupper():
                raise ValueError('Invalid position currency')
            portfolio = portfolio or row['Portfolio']
            if row['Portfolio'] != portfolio:
                raise ValueError('Multiple portfolios in a single export are unsupported')
            group = row['Group of products']
            kind = 'cash_account' if group == 'Liquidity - Accounts' else (
                'call_deposit' if group == 'Liquidity - Call deposits' else 'security')
            if kind == 'security' and not (row['ISIN'] or row['Valor']):
                raise ValueError('Security requires ISIN or Valor')
            # Deposits share a custody product: serial description distinguishes them.
            identifier = row['ISIN'] or row['Valor'] or row.get('Description 2') or row['Product']
            identity = (portfolio, row['Product'], kind, row['Ccy.'], identifier)
            if identity in identities:
                raise ValueError('Duplicate position identity')
            identities.add(identity)
            price_date = row.get('Date', '')
            if price_date:
                price_date = datetime.strptime(price_date, '%d.%m.%Y').date().isoformat()
            pnl = row.get('Unrealized P/L', '')
            if pnl and not pnl.endswith('%'):
                raise ValueError('Unsupported unrealized P/L convention')
            rows.append({
                'id': hashlib.sha256('|'.join(identity).encode()).hexdigest(),
                'portfolio': portfolio, 'product': row['Product'], 'kind': kind,
                'group': group, 'description': row['Description'],
                'isin': row['ISIN'] or None, 'valor': row['Valor'] or None,
                'currency': row['Ccy.'], 'quantity_or_balance': number(row['Number/Amt.']),
                'intraday_quantity_or_balance': number(row['Intraday number/amount'], True),
                'market_value': number(row['Market value']),
                'accrued_interest': number(row['Accrued interest'], True),
                'cost_value': number(row.get('Cost value', ''), True),
                'cost_price': quote(row.get('Cost price', '')),
                'market_price': quote(row.get('Market price', '')),
                'price_date': price_date or None,
                'unrealized_pnl_percent': number(pnl[:-1]) if pnl else None,
                'raw': row,
            })
    except (csv.Error, ValueError) as exc:
        raise ValueError(f'Positions import rejected near CSV line {reader.line_num}: {exc}') from None
    required_metadata = {'Portfolio number', 'Valued as of', 'Valued in',
                         'Export created on', 'Number of positions'}
    if not required_metadata.issubset(metadata):
        raise ValueError('Missing valuation metadata')
    if not rows or metadata['Portfolio number'] != portfolio:
        raise ValueError('Empty snapshot or portfolio/footer mismatch')
    try:
        count = int(metadata['Number of positions'])
        as_of = datetime.strptime(metadata['Valued as of'], '%d.%m.%Y').date().isoformat()
        exported = datetime.strptime(metadata['Export created on'], '%d.%m.%Y %H:%M').isoformat()
    except ValueError:
        raise ValueError('Invalid snapshot date or position count') from None
    if count != len(rows):
        raise ValueError(f'Position count mismatch: expected {count}, parsed {len(rows)}')
    base = metadata['Valued in']
    if len(base) != 3 or not base.isalpha() or not base.isupper():
        raise ValueError('Invalid valuation currency')
    for row in rows:
        row['valuation_currency'] = base
    groups = {}
    cash = {}
    for row in rows:
        group = groups.setdefault(row['group'], {'count': 0, 'market_value': Decimal(0)})
        group['count'] += 1
        group['market_value'] += Decimal(row['market_value'])
        if row['kind'] == 'cash_account':
            ccy = row['currency']
            cash[ccy] = cash.get(ccy, Decimal(0)) + Decimal(row['quantity_or_balance'])
    return {
        'source': 'ubs_positions_export', 'portfolio': portfolio,
        'as_of': as_of, 'exported_at': exported, 'timestamp_timezone': 'unspecified',
        'valuation_currency': base, 'count': count, 'positions': rows,
        'exchange_rates': fx,
        'summary': {
            'market_value': str(sum((Decimal(row['market_value']) for row in rows), Decimal(0))),
            'accrued_interest': str(sum((Decimal(row['accrued_interest'] or '0') for row in rows), Decimal(0))),
            'cash_balances': {ccy: str(value) for ccy, value in sorted(cash.items())},
            'groups': {name: {'count': item['count'], 'market_value': str(item['market_value'])}
                       for name, item in groups.items()},
        },
        'warnings': [
            'Dated UBS snapshot, not live holdings or prices.',
            'Market values are broker-reported in valuation currency; accrued interest is shown separately.',
            'Cash balances and intraday balances are distinct; deposits are not available cash.',
            'Unrealized P/L fields are broker percentages, not daily or realized P/L.',
            'Instrument groups are UBS classifications; terms and multipliers are not independently verified.',
        ],
    }


def main():
    from .storage import SnapshotStore, DEFAULT_DB
    parser = argparse.ArgumentParser(description='Validate and persist a dated UBS positions snapshot')
    parser.add_argument('file', type=Path)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    raw = args.file.read_bytes()
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw.decode('cp1252')
    try:
        result = SnapshotStore(args.db).import_content(content)
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps({key: result[key] for key in
                      ['id', 'as_of', 'count', 'valuation_currency', 'already_imported']}, indent=2))


if __name__ == '__main__':
    main()
