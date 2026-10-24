#!/usr/bin/env python3
"""Diagnose public REST connectivity without credentials, trading or synthetic fallback.

The report contains bounded metadata/counts only, never environment variables, proxy URLs,
HTTP headers or raw responses. A completed probe is not proof that every venue is reachable.
"""
from argparse import ArgumentParser
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'python'))
from aimt_market.data import Instrument, MarketDataError, PublicMarketClient  # noqa: E402


CASES = (
    ('Binance', 'spot', 'BTCUSDT', 'BTC', 'USDT'),
    ('Binance', 'perpetual', 'BTCUSDT', 'BTC', 'USDT'),
    ('Bybit', 'spot', 'BTCUSDT', 'BTC', 'USDT'),
    ('Bybit', 'perpetual', 'BTCUSDT', 'BTC', 'USDT'),
    ('OKX', 'spot', 'BTC-USDT', 'BTC', 'USDT'),
    ('OKX', 'perpetual', 'BTC-USDT-SWAP', 'BTC', 'USDT'),
    ('Kraken', 'spot', 'XBTUSD', 'XBT', 'USD'),
)


class AuditedSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.observations = []

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if method.upper() != 'GET' or parsed.scheme != 'https' or parsed.username or parsed.password:
            raise ValueError('The public probe permits unauthenticated HTTPS GET only.')
        if kwargs.get('verify', True) is not True:
            raise ValueError('TLS verification must remain enabled.')
        # The client does not use credentials. Keep platform-injected networking untouched.
        if kwargs.get('auth') or self.auth:
            raise ValueError('The probe must not use API credentials.')
        event = {'method': 'GET', 'host': parsed.hostname, 'path': parsed.path}
        started = time.monotonic()
        try:
            response = super().request(method, url, **kwargs)
            event.update(http_status=response.status_code, outcome='http_response')
            return response
        except requests.RequestException as error:
            event.update(outcome='transport_error', error_type=type(error).__name__)
            # Record an observed tunnel status, without copying proxy addresses or exceptions.
            if isinstance(error, requests.exceptions.ProxyError):
                match = re.search(r'\b(403|407|429|451|502|503|504)\b', str(error))
                if match:
                    event['proxy_tunnel_status'] = int(match.group(1))
            raise
        finally:
            event['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            self.observations.append(event)


def failed(error):
    status = getattr(error, 'status_code', None)
    cause = getattr(error, '__cause__', None)
    kind = ('http_access_restricted' if status in (401, 403, 451) else
            'rate_limited' if status in (418, 429) else 'operation_unavailable')
    if isinstance(cause, requests.exceptions.ProxyError):
        kind = 'network_proxy_blocked' if re.search(r'\b(403|407)\b', str(cause)) else 'network_proxy_unavailable'
    elif isinstance(cause, requests.exceptions.ConnectionError):
        kind = 'network_connection_unavailable'
    return {'outcome': kind, 'error_type': type(error).__name__,
            'transport_error_type': type(cause).__name__ if cause is not None else None,
            'http_status': status, 'retry_after_seconds': getattr(error, 'retry_after', None),
            'message': str(error)[:240] if isinstance(error, MarketDataError)
                       else 'Public operation did not complete; inspect transport observations.'}


def run_case(provider, market, symbol, base, quote, *, limit):
    session = AuditedSession()
    client = PublicMarketClient(session=session)
    result = {'provider': provider, 'market': market, 'credentials_used': False,
              'tls_verification': True, 'mode': 'public_live_probe', 'synthetic_fallback': False}
    chosen = Instrument(provider, market, symbol, base, quote)
    try:
        try:
            catalog = client.list_instruments(provider, market)
            result['catalog'] = {'outcome': 'received', 'count': len(catalog),
                                 'forex_count': sum(item.kind == 'forex' for item in catalog),
                                 'sample_keys': [item.key for item in catalog[:8]],
                                 'warnings': list(client.catalog_warnings)[:8]}
            candidate = next((item for item in catalog if item.symbol == symbol), None)
            if candidate is None:
                candidate = next((item for item in catalog if item.base in ('BTC', 'XBT')
                                  and item.quote in ('USD', 'USDT')), None)
            if candidate is not None:
                chosen = candidate
                result['selection'] = 'discovered_in_catalog'
            else:
                result['selection'] = 'canonical_probe_symbol_not_discovered'
        except Exception as error:
            result['catalog'] = failed(error)
            # Still test a canonical public pair, not demo data, after catalog access fails.
            result['selection'] = 'canonical_probe_symbol_catalog_unavailable'
        result['instrument'] = chosen.as_dict()
        try:
            snapshot = client.fetch_snapshot(chosen, 'M1', limit=limit, include_orderflow=True)
            closed = [bar for bar in snapshot.candles if bar.get('closed')]
            result['snapshot'] = {
                'outcome': 'received', 'timeframe': snapshot.timeframe,
                'candle_count': len(snapshot.candles), 'closed_candle_count': len(closed),
                'last_closed_open_time_utc_seconds': closed[-1]['time'] if closed else None,
                'fetched_at_ms': snapshot.fetched_at_ms,
                'trade_count': len(snapshot.trades),
                'book_bid_levels': len(snapshot.book.get('bids', ())),
                'book_ask_levels': len(snapshot.book.get('asks', ())),
                'book_timestamp_ms': snapshot.book.get('timestamp_ms'),
                'derivative_fields': sorted(key for key, value in snapshot.derivatives.items()
                                            if value is not None and not key.startswith('_')),
                'section_errors': list(snapshot.errors)[:12],
                'provenance': snapshot.provenance,
            }
        except Exception as error:
            result['snapshot'] = failed(error)
    finally:
        result['requests'] = session.observations
        client.close()
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='artifacts/public-market-probe.json')
    parser.add_argument('--provider', choices=('all', 'Binance', 'Bybit', 'OKX', 'Kraken'), default='all')
    parser.add_argument('--spot-only', action='store_true')
    parser.add_argument('--limit', type=int, default=60)
    parser.add_argument('--require-live', action='store_true',
                        help='Return status 2 if any selected snapshot could not be retrieved.')
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error('--limit must be 1–1000')
    cases = [case for case in CASES if (args.provider == 'all' or case[0] == args.provider)
             and (not args.spot_only or case[1] == 'spot')]
    results = []
    for case in cases:
        result = run_case(*case, limit=args.limit)
        results.append(result)
        print(f"{case[0]}/{case[1]}: catalog={result['catalog']['outcome']}; "
              f"snapshot={result['snapshot']['outcome']}", flush=True)
    count = sum(result['snapshot']['outcome'] == 'received' for result in results)
    report = {'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'purpose': 'Unauthenticated public REST diagnostic; not account or execution validation.',
              'network_policy_changed': False, 'cases': results,
              'summary': {'selected_cases': len(results), 'snapshots_received': count,
                          'snapshots_unavailable': len(results) - count,
                          'all_selected_snapshots_received': count == len(results)}}
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(f'Report: {output}; actual snapshots received {count}/{len(results)}.')
    return 2 if args.require_live and count != len(results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
