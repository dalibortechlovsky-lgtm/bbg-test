"""Read-only Bloomberg Desktop API reference-data probe, run on Terminal Windows host.

Install Bloomberg's official Python SDK before use. This probe does not connect
ChatGPT or UBS and does not expose a network service.
"""
import argparse
import json
import sys
import time


def fetch(securities, fields, timeout=30):
    import blpapi
    options = blpapi.SessionOptions()
    options.setServerHost('localhost')
    options.setServerPort(8194)
    session = blpapi.Session(options)
    try:
        if not session.start():
            raise RuntimeError('Desktop API session failed; check Terminal login and API availability')
        if not session.openService('//blp/refdata'):
            raise RuntimeError('Bloomberg reference-data service unavailable')
        request = session.getService('//blp/refdata').createRequest('ReferenceDataRequest')
        for security in securities:
            request.getElement('securities').appendValue(security)
        for field in fields:
            request.getElement('fields').appendValue(field)
        session.sendRequest(request)
        results = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = session.nextEvent(1000)
            for message in event:
                if message.hasElement('responseError'):
                    raise RuntimeError(str(message.getElement('responseError')))
                if message.messageType() == blpapi.Name('RequestFailure'):
                    raise RuntimeError(str(message))
                if message.hasElement('securityData'):
                    data = message.getElement('securityData')
                    for i in range(data.numValues()):
                        item = data.getValueAsElement(i)
                        results.append(item.toPy())
            if event.eventType() == blpapi.Event.RESPONSE:
                return results
        raise TimeoutError('Bloomberg request timed out')
    finally:
        session.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('securities', nargs='+', help='Exact Bloomberg security identifiers')
    parser.add_argument('--fields', nargs='+', default=['PX_LAST', 'CRNCY'])
    args = parser.parse_args()
    try:
        print(json.dumps(fetch(args.securities, args.fields), default=str, indent=2))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
