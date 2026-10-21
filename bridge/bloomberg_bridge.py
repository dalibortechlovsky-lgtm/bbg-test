"""Read-only Desktop API polling and optional authenticated HTTPS quote upload."""
import argparse
import json
import os
from pathlib import Path
import time
import sys
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def request_quotes(securities):
    import blpapi
    options = blpapi.SessionOptions()
    options.setServerHost('localhost')
    options.setServerPort(8194)
    session = blpapi.Session(options)
    try:
        if not session.start() or not session.openService('//blp/refdata'):
            raise RuntimeError('Bloomberg session or reference-data service unavailable')
        request = session.getService('//blp/refdata').createRequest('ReferenceDataRequest')
        for security in securities:
            request.getElement('securities').appendValue(security)
        for field in ['PX_LAST', 'CRNCY', 'ID_ISIN', 'ID_BB_UNIQUE']:
            request.getElement('fields').appendValue(field)
        session.sendRequest(request)
        rows = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            event = session.nextEvent(1000)
            for message in event:
                if message.hasElement('responseError') or message.messageType() == blpapi.Name('RequestFailure'):
                    raise RuntimeError('Bloomberg rejected the reference-data request')
                if message.hasElement('securityData'):
                    data = message.getElement('securityData')
                    for i in range(data.numValues()):
                        row = data.getValueAsElement(i).toPy()
                        index = row.get('sequenceNumber')
                        if isinstance(index, int) and 0 <= index < len(securities):
                            row['requested_security'] = securities[index]
                        rows.append(row)
            if event.eventType() == blpapi.Event.RESPONSE:
                return rows
        raise TimeoutError('Bloomberg response timed out')
    finally:
        session.stop()


def request_history(requests):
    """Read requested daily FX or ISIN prices; no account information needed."""
    import blpapi
    if not isinstance(requests,list) or len(requests)>500:raise ValueError('Invalid historical price request list')
    wanted={r['security']:set(r['dates']) for r in requests}
    if not wanted:return []
    from datetime import date
    import re
    option=re.compile(r'[A-Z0-9]{1,8} [A-Z]{2} \d{2}/\d{2}/\d{2} [CP]\d+(?:\.\d+)? (?:Equity|Index)')
    for security,days in wanted.items():
        supported=(re.fullmatch('[A-Z]{3}CZK Curncy',security) or
                   re.fullmatch('/isin/[A-Z0-9]{12}',security) or option.fullmatch(security))
        if not supported or not 1<=len(days)<=500:raise ValueError('Invalid historical security or dates')
        for day in days:
            if date.fromisoformat(day).isoformat()!=day:raise ValueError('Invalid history date')
    options=blpapi.SessionOptions();options.setServerHost('localhost');options.setServerPort(8194)
    session=blpapi.Session(options)
    try:
        if not session.start() or not session.openService('//blp/refdata'):raise RuntimeError('Bloomberg historical service unavailable')
        request=session.getService('//blp/refdata').createRequest('HistoricalDataRequest')
        for security in wanted:request.getElement('securities').appendValue(security)
        request.getElement('fields').appendValue('PX_LAST')
        request.getElement('fields').appendValue('YAS_BOND_ACCRUED')
        request.getElement('fields').appendValue('INT_ACC')
        days=[day for values in wanted.values() for day in values]
        request.set('startDate',min(days).replace('-',''));request.set('endDate',max(days).replace('-',''))
        request.set('periodicitySelection','DAILY')
        request.set('nonTradingDayFillOption','ALL_CALENDAR_DAYS')
        request.set('nonTradingDayFillMethod','PREVIOUS_VALUE')
        session.sendRequest(request);result=[];deadline=time.monotonic()+30;complete=False
        while time.monotonic()<deadline:
            event=session.nextEvent(1000)
            for message in event:
                if message.hasElement('responseError') or message.messageType()==blpapi.Name('RequestFailure'):
                    raise RuntimeError('Bloomberg rejected historical price request')
                if not message.hasElement('securityData'):continue
                row=message.getElement('securityData').toPy()
                sequence=row.get('sequenceNumber')
                security=(list(wanted)[sequence] if isinstance(sequence,int) and 0<=sequence<len(wanted)
                          else row.get('security'))
                if security not in wanted or row.get('securityError'):continue
                for field in row.get('fieldData',[]):
                    day=str(field.get('date'))[:10]
                    if day not in wanted[security]:continue
                    try:value=Decimal(str(field.get('PX_LAST')))
                    except (InvalidOperation,ValueError,TypeError):continue
                    if value.is_finite() and value>0:
                        clean={'security':security,'date':day,'price':str(value)}
                        try:accrued=Decimal(str(field.get('YAS_BOND_ACCRUED',field.get('INT_ACC'))))
                        except (InvalidOperation,ValueError,TypeError):accrued=None
                        if accrued is not None and accrued.is_finite():clean['accrued']=str(accrued)
                        result.append(clean)
            if event.eventType()==blpapi.Event.RESPONSE:
                complete=True;break
        if not complete:raise TimeoutError('Bloomberg historical price request timed out')
        # INT_ACC is often reference-only. Ask Bloomberg for the same dated
        # settlement value and merge it without changing the historical price.
        missing={(r['security'],r['date']) for r in result
                 if r['security'].startswith('/isin/') and 'accrued' not in r}
        for day in sorted({d for _,d in missing}):
            securities=sorted(s for s,d in missing if d==day)
            if not securities:continue
            try:
                accrued_request=session.getService('//blp/refdata').createRequest('ReferenceDataRequest')
                for security in securities:accrued_request.getElement('securities').appendValue(security)
                accrued_request.getElement('fields').appendValue('INT_ACC')
                override=accrued_request.getElement('overrides').appendElement()
                override.setElement('fieldId','SETTLE_DT');override.setElement('value',day.replace('-',''))
                session.sendRequest(accrued_request);deadline=time.monotonic()+20;values={}
                while time.monotonic()<deadline:
                    event=session.nextEvent(1000)
                    for message in event:
                        if not message.hasElement('securityData'):continue
                        data=message.getElement('securityData')
                        for i in range(data.numValues()):
                            row=data.getValueAsElement(i).toPy();sequence=row.get('sequenceNumber')
                            security=(securities[sequence] if isinstance(sequence,int) and 0<=sequence<len(securities)
                                      else row.get('security'))
                            try:value=Decimal(str(row.get('fieldData',{}).get('INT_ACC')))
                            except (InvalidOperation,ValueError,TypeError):continue
                            if security in securities and value.is_finite():values[security]=value
                    if event.eventType()==blpapi.Event.RESPONSE:break
                for row in result:
                    if row['date']==day and row['security'] in values:row['accrued']=str(values[row['security']])
            except Exception:
                pass  # PX_LAST remains usable; the dashboard keeps accrued flagged for review.
        return result
    finally:session.stop()


def cycle(state, securities, fetch=request_quotes, now=utc_now):
    """Retain last good quotes on failure; never relabel them as newly received."""
    updated = dict(state, checked_at=now())
    try:
        rows = fetch(securities)
        valid = []
        for row in rows:
            requested = row.get('requested_security') or row.get('security')
            errors = row.get('fieldExceptions') or []
            if (requested not in securities or row.get('securityError') or
                    any(item.get('fieldId') == 'PX_LAST' for item in errors)):
                continue
            try:
                price = Decimal(str(row.get('fieldData', {}).get('PX_LAST')))
                if not price.is_finite():
                    continue
            except InvalidOperation:
                continue
            clean = dict(row, requested_security=requested, observed_at=now())
            clean['fieldData'] = dict(row['fieldData'], PX_LAST=str(price))
            valid.append(clean)
        reported = {row['requested_security'] for row in valid}
        prior = {row.get('requested_security') or row.get('security'): row for row in state.get('quotes', [])}
        prior.update({row['requested_security']: row for row in valid})
        complete = set(securities).issubset(reported)
        updated.update(status='connected' if complete else 'degraded',
                       error=None if complete else 'Missing or unentitled security/price',
                       failed_response=[] if complete else rows)
        if valid:
            updated.update(quotes=[row for key, row in prior.items() if key in securities], received_at=now())
    except Exception as exc:
        updated.update(status='disconnected', error=type(exc).__name__ + ': ' + str(exc))
    return updated


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward device credentials to a redirected host.


def load_config(path):
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    urls = [urlsplit(config.get(key, '')) for key in ['endpoint', 'watchlist_url']]
    for url in urls:
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('Bridge configuration must use HTTPS URLs without embedded credentials')
    if urls[0].netloc != urls[1].netloc:
        raise ValueError('Watchlist and upload must use the same host')
    if urls[0].path!='/api/bridge/quotes' or urls[1].path!='/api/bridge/watchlist':
        raise ValueError('Use the dashboard-generated bridge endpoint paths')
    token = config.get('token')
    if not isinstance(token, str) or not token.startswith('bbg_') or not 20 <= len(token) <= 256:
        raise ValueError('Invalid bridge token configuration')
    return config


def cloud_request(config, key, payload=None):
    body = json.dumps(payload, default=str, allow_nan=False).encode() if payload is not None else None
    request = urllib.request.Request(config[key], data=body,
        headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
    # Standard proxy and CA settings remain in effect; certificate verification is enabled.
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        return json.load(response)


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(state, default=str, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('securities', nargs='*')
    parser.add_argument('--config', type=Path, help='Private JSON configuration downloaded from your dashboard')
    parser.add_argument('--once', action='store_true', help='Perform one poll/upload and exit')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'data' / 'bloomberg.json')
    parser.add_argument('--interval', type=int, default=15)
    args = parser.parse_args()
    if args.interval < 5:
        parser.error('Polling interval must be at least five seconds')
    try:
        config = load_config(args.config) if args.config else None
    except (OSError, ValueError) as exc:
        print('Configuration error: ' + str(exc), file=sys.stderr)
        return 1
    securities = args.securities or ['IBM US Equity']
    next_watchlist = 0
    state = {'schema_version': 1, 'status': 'starting', 'quotes': [], 'received_at': None}
    delay = args.interval
    try:
        while True:
            if config and not args.securities and time.monotonic() >= next_watchlist:
                try:
                    watchlist = cloud_request(config, 'watchlist_url')
                    candidates = watchlist.get('securities')
                    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 500 or any(not isinstance(s, str) for s in candidates):
                        raise ValueError('Invalid watchlist')
                    securities = candidates
                    print('Watchlist:', len(securities), 'securities;', watchlist.get('unmapped_count', 0), 'holdings need mapping', flush=True)
                    history=watchlist.get('history_requests',[])
                    if history:
                        try:
                            quotes=request_history(history)
                            if quotes:
                                history_config=dict(config,history_endpoint=config['endpoint'].removesuffix('/quotes')+'/history')
                                accepted=existing=0
                                for offset in range(0,len(quotes),500):
                                    result=cloud_request(history_config,'history_endpoint',{'quotes':quotes[offset:offset+500]})
                                    accepted+=result['accepted'];existing+=result['existing']
                                print('Historical prices:',accepted,'new values;',existing,'already stored',flush=True)
                        except Exception as exc:print('Historical prices pending:',type(exc).__name__,flush=True)
                except urllib.error.HTTPError as exc:
                    print('Watchlist HTTP', exc.code, flush=True)
                except Exception as exc:
                    print('Watchlist unavailable:', type(exc).__name__, flush=True)
                next_watchlist = time.monotonic() + 60
            state = cycle(state, securities)
            uploaded = config is None
            if config:
                try:
                    result = cloud_request(config, 'endpoint', state)
                    print('Cloud accepted', result['accepted'], 'new quotes;', result['ignored'], 'unchanged', flush=True)
                    uploaded = True
                except urllib.error.HTTPError as exc:
                    print('Upload failed: HTTP', exc.code, flush=True)
                except Exception as exc:
                    print('Upload failed:', type(exc).__name__, flush=True)
            save(args.output, state)
            print(state['checked_at'], state['status'], flush=True)
            if args.once:
                return 0 if uploaded and state['status'] in {'connected', 'degraded'} and state.get('quotes') else 1
            delay = args.interval if state['status'] == 'connected' else min(max(delay * 2, 5), 60)
            time.sleep(delay)
    except KeyboardInterrupt:
        save(args.output, dict(state, status='stopped', checked_at=utc_now()))
        print('Bridge stopped; prior quotes retain their receipt timestamp.', flush=True)
        if config:
            try:
                cloud_request(config, 'endpoint', dict(state, status='stopped'))
            except Exception:
                pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
