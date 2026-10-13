"""Importer for UBS KeyTrader semicolon-delimited order-enquiry exports.

Completed orders are aggregate executions, not individual fill records.
Other states are retained for review and never turned into confirmed trades.
"""
import argparse
import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from typing import Iterable
from .trades import Trade, TradeSource, TradeStatus

REQUIRED = {'Order number', 'State', 'Order quantity', 'Open quantity',
            'Quantity type', 'Side', 'Instrument', 'ISIN', 'Avg. price',
            'Currency', 'Custody acc.', 'Entered on'}


@dataclass
class ImportResult:
    trades: list[Trade] = field(default_factory=list)
    review: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    rows: int = 0


def _unwrap(value):
    value = value.strip()
    if value.startswith('=("') and value.endswith('")'):
        return value[3:-2].replace('""', '"').strip()
    if value.startswith('='):
        raise ValueError('Unsupported spreadsheet cell expression')
    return value


def _number(value, name):
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f'Invalid {name}') from None
    if not number.is_finite():
        raise ValueError(f'Non-finite {name}')
    return number


def import_keytrader_csv(content: str) -> ImportResult:
    content = content.lstrip('\ufeff')
    lines = content.splitlines()
    if lines and lines[0].strip().lower() == 'sep=;':
        lines = lines[1:]
    reader = csv.reader(StringIO('\n'.join(lines)), delimiter=';', strict=True)
    try:
        parsed_rows = list(reader)
    except csv.Error as exc:
        raise ValueError(f'Malformed CSV near line {reader.line_num}: {exc}') from None
    reader = iter(parsed_rows)
    try:
        headers = [_unwrap(cell) for cell in next(reader)]
    except StopIteration:
        raise ValueError('Empty export') from None
    if len(set(headers)) != len(headers):
        raise ValueError('Duplicate column names')
    missing = REQUIRED - set(headers)
    if missing:
        raise ValueError('Missing columns: ' + ', '.join(sorted(missing)))
    result = ImportResult()
    seen = {}
    for line, cells in enumerate(reader, start=2):
        if not cells:
            continue
        result.rows += 1
        try:
            if len(cells) != len(headers):
                raise ValueError('Column count differs from header')
            row = dict(zip(headers, map(_unwrap, cells)))
            order = row['Order number']
            account = row['Custody acc.']
            if not order or not account:
                raise ValueError('Order number and custody account are required')
            key = (account, order)
            if key in seen:
                if seen[key] != row:
                    raise ValueError('Conflicting repeated order; review source export')
                result.review.append({'row': line, 'reason': 'DUPLICATE_ORDER',
                                      'order': order, 'account': account})
                continue
            seen[key] = row
            side = row['Side'].upper()
            if side not in {'BUY', 'SELL'}:
                raise ValueError('Unrecognized side')
            qty = _number(row['Order quantity'], 'order quantity')
            open_qty = _number(row['Open quantity'], 'open quantity')
            if qty <= 0 or open_qty < 0 or open_qty > qty:
                raise ValueError('Invalid order/open quantity range')
            if row['State'].casefold() != 'completed':
                result.review.append({'row': line, 'state': row['State'],
                                      'reason': 'NON_COMPLETED_ORDER', 'order': order,
                                      'account': account, 'instrument': row['Instrument'],
                                      'order_quantity': str(qty), 'open_quantity': str(open_qty)})
                continue
            if row.get('Sub State', ''):
                raise ValueError('Completed order has a sub-state requiring review')
            if open_qty != 0:
                raise ValueError('Completed order still has open quantity')
            if row['Quantity type'].casefold() != 'units':
                raise ValueError('Unsupported quantity type; instrument conventions required')
            price = _number(row['Avg. price'], 'average price')
            if price <= 0:
                raise ValueError('Completed order requires positive average price')
            if not row['Instrument'] or not row['ISIN'] or not row['Currency']:
                raise ValueError('Instrument, ISIN and currency are required')
            currency = row['Currency'].upper()
            if len(currency) != 3 or not currency.isalpha():
                raise ValueError('Invalid currency code')
            entered = datetime.strptime(row['Entered on'], '%d.%m.%Y %H:%M:%S')
            result.trades.append(Trade(
                trade_id=f'UBS-{account}-{order}', source=TradeSource.UBS_KEYTRADER,
                status=TradeStatus.CONFIRMED, side=side, security=row['ISIN'].upper(),
                quantity=qty, price=price, currency=currency,
                account=account, external_id=order, instrument_name=row['Instrument'],
                order_entered_at=entered, record_kind='order_execution_summary',
                quantity_type=row['Quantity type'], trading_place=row.get('Trading place'),
            ))
        except ValueError as exc:
            result.errors.append({'row': line, 'reason': str(exc)})
    if not result.rows:
        raise ValueError('Export contains no order rows')
    if result.errors:
        result.trades.clear()  # No partial acceptance of an invalid batch.
    return result


def parse_keytrader_csv(content: str) -> Iterable[Trade]:
    result = import_keytrader_csv(content)
    if result.errors:
        raise ValueError('Import rejected: ' + json.dumps(result.errors))
    return iter(result.trades)


def main():
    parser = argparse.ArgumentParser(description='Validate or persist KeyTrader order summaries; never submit trades')
    parser.add_argument('file', type=Path)
    parser.add_argument('--persist', action='store_true', help='Save validated summaries and import review to local storage')
    from .storage import DEFAULT_DB, SnapshotStore
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    raw = args.file.read_bytes()
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw.decode('cp1252')
    try:
        if args.persist:
            report = SnapshotStore(args.db).import_orders_content(content)
            print(json.dumps(report, indent=2))
            return 0 if report['accepted'] else 1
        result = import_keytrader_csv(content)
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps({'rows': result.rows, 'completed_order_summaries': len(result.trades),
                      'review': result.review, 'errors': result.errors,
                      'accepted': not result.errors}, indent=2))
    return 1 if result.errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
