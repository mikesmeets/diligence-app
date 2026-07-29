"""
Alpha Vantage client.

Credentials follow the same rule as the Anthropic and FMP keys: the
ALPHAVANTAGE_API_KEY environment variable wins if set, and the settings table is
the fallback.

The free tier allows 25 requests *per day* — not per minute. That budget is
small enough to design around: every response is cached for hours, and calls are
counted so the Admin page can show how much of the day is left. Without that, a
page left open on a refresh loop would quietly exhaust the day's quota and the
tearsheet would start falling back to Yahoo for no visible reason.

Everything hangs off one endpoint, /query, switched by a `function` parameter.
"""
import os
import threading
from datetime import datetime, timezone

import requests

import db

BASE = 'https://www.alphavantage.co/query'
TIMEOUT = 20

# Responses change slowly — estimates revise daily at most — and the daily quota
# is 25 calls, so a long TTL costs nothing and buys a lot of headroom.
CACHE_TTL = 6 * 60 * 60

FREE_TIER_DAILY = 25


class NotConfigured(Exception):
    """No Alpha Vantage key available from either the environment or settings."""


class AlphaVantageError(Exception):
    """The API refused or failed the request."""


class RateLimited(AlphaVantageError):
    """The daily (or per-minute) request budget is spent."""


_lock = threading.Lock()
_cache = {}          # (function, symbol) -> (expires_at, payload)
_calls = {'date': None, 'count': 0}


def api_key():
    return os.environ.get('ALPHAVANTAGE_API_KEY') or db.get_setting('alphavantage_api_key') or ''


def key_source():
    """Where the active key comes from — surfaced in Admin, never the key."""
    if os.environ.get('ALPHAVANTAGE_API_KEY'):
        return 'env'
    if db.get_setting('alphavantage_api_key'):
        return 'settings'
    return None


def enabled():
    return bool(api_key())


def _today():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _count_call():
    with _lock:
        if _calls['date'] != _today():
            _calls.update(date=_today(), count=0)
        _calls['count'] += 1
        return _calls['count']


def usage():
    """Calls made today, for the Admin page. Resets at UTC midnight.

    In-process only: a redeploy restarts the count, so treat it as a floor
    rather than an audit. Alpha Vantage does not publish a quota endpoint.
    """
    with _lock:
        used = _calls['count'] if _calls['date'] == _today() else 0
    return {'used': used, 'limit': FREE_TIER_DAILY, 'day': _today()}


def clear_cache():
    with _lock:
        _cache.clear()


def get(function, symbol, use_cache=True):
    """Call one function and return the decoded body.

    Alpha Vantage answers failures with HTTP 200 and an explanatory key, so the
    status code tells you almost nothing — the body has to be inspected.
    """
    key = api_key()
    if not key:
        raise NotConfigured(
            'No Alpha Vantage API key. Set the ALPHAVANTAGE_API_KEY environment '
            'variable, or add a key on the Admin page.'
        )

    symbol = (symbol or '').strip().upper()
    slot = (function, symbol)
    now = datetime.now(timezone.utc).timestamp()

    if use_cache:
        with _lock:
            hit = _cache.get(slot)
        if hit and hit[0] > now:
            return hit[1]

    try:
        resp = requests.get(
            BASE, params={'function': function, 'symbol': symbol, 'apikey': key},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise AlphaVantageError(f'Could not reach Alpha Vantage: {exc}') from exc

    _count_call()

    if resp.status_code >= 400:
        raise AlphaVantageError(f'Alpha Vantage returned {resp.status_code}: {resp.text[:200]}')

    try:
        data = resp.json()
    except ValueError:
        raise AlphaVantageError(
            f'Alpha Vantage returned something that is not JSON: {resp.text[:200]}')

    if isinstance(data, dict):
        # "Information" carries two very different meanings — quota exhausted, or
        # endpoint not on your plan. Reading the text is the only way to tell.
        info = data.get('Information') or data.get('Note') or ''
        if info:
            lowered = info.lower()
            if 'rate limit' in lowered or 'per day' in lowered or 'frequency' in lowered:
                raise RateLimited(
                    f'Alpha Vantage daily request limit reached (free tier allows '
                    f'{FREE_TIER_DAILY}/day). It resets at UTC midnight.'
                )
            if 'premium' in lowered or 'subscribe' in lowered:
                raise AlphaVantageError(
                    f'"{function}" is not included in your Alpha Vantage plan.')
            raise AlphaVantageError(info[:250])

        if data.get('Error Message'):
            raise AlphaVantageError(
                f'Alpha Vantage rejected the request for {symbol}: '
                f'{str(data["Error Message"])[:200]}'
            )

        # An unknown symbol comes back as an empty object rather than an error.
        if not data:
            raise AlphaVantageError(f'Alpha Vantage has no data for {symbol}.')

    with _lock:
        _cache[slot] = (now + CACHE_TTL, data)
    return data


def _num(v):
    """Alpha Vantage sends numbers as strings, and 'None' as a literal string."""
    if v is None or v in ('None', 'NA', '-', ''):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def overview(symbol):
    """Company snapshot: trailing multiples, analyst target, rating spread."""
    d = get('OVERVIEW', symbol)
    if not isinstance(d, dict) or not d.get('Symbol'):
        raise AlphaVantageError(f'Alpha Vantage has no company overview for {symbol}.')

    ratings = {
        'strong_buy':  _num(d.get('AnalystRatingStrongBuy')),
        'buy':         _num(d.get('AnalystRatingBuy')),
        'hold':        _num(d.get('AnalystRatingHold')),
        'sell':        _num(d.get('AnalystRatingSell')),
        'strong_sell': _num(d.get('AnalystRatingStrongSell')),
    }
    return {
        'symbol':        d.get('Symbol'),
        'name':          d.get('Name'),
        'exchange':      d.get('Exchange'),
        'currency':      d.get('Currency'),
        'sector':        d.get('Sector'),
        'industry':      d.get('Industry'),
        'description':   d.get('Description'),
        'target_price':  _num(d.get('AnalystTargetPrice')),
        'ratings':       ratings,
        'ratings_total': sum(v for v in ratings.values() if v) or None,
        'forward_pe':    _num(d.get('ForwardPE')),
        'trailing_pe':   _num(d.get('TrailingPE')),
        'peg':           _num(d.get('PEGRatio')),
        'ev_ebitda':     _num(d.get('EVToEBITDA')),
        'ev_revenue':    _num(d.get('EVToRevenue')),
        'price_book':    _num(d.get('PriceToBookRatio')),
        'beta':          _num(d.get('Beta')),
    }


def annual_estimates(symbol):
    """Forward fiscal-year consensus, newest year last.

    Alpha Vantage carries EPS and revenue only — no EBITDA or EBIT — so this
    does not close the forward-multiples gap FMP would have. What it adds over
    Yahoo is dispersion (high/low/analyst count) and revision momentum: where
    the estimate sat 7, 30, 60 and 90 days ago, and how many analysts have moved
    up or down. That drift is usually the more interesting signal anyway.
    """
    d = get('EARNINGS_ESTIMATES', symbol)
    rows = d.get('estimates') if isinstance(d, dict) else None
    if not isinstance(rows, list):
        return []

    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get('horizon') != 'fiscal year':
            continue
        date = str(row.get('date') or '')[:10]
        if not date:
            continue
        out.append({
            'date':      date,
            'label':     f'FY{date[2:4]}',
            'eps':       _num(row.get('eps_estimate_average')),
            'eps_high':  _num(row.get('eps_estimate_high')),
            'eps_low':   _num(row.get('eps_estimate_low')),
            'revenue':      _num(row.get('revenue_estimate_average')),
            'revenue_high': _num(row.get('revenue_estimate_high')),
            'revenue_low':  _num(row.get('revenue_estimate_low')),
            'analysts':     _num(row.get('eps_estimate_analyst_count')),
            'rev_analysts': _num(row.get('revenue_estimate_analyst_count')),
            'eps_30d_ago':  _num(row.get('eps_estimate_average_30_days_ago')),
            'eps_90d_ago':  _num(row.get('eps_estimate_average_90_days_ago')),
            'revisions_up':   (_num(row.get('eps_estimate_revision_up_trailing_30_days')) or 0),
            'revisions_down': (_num(row.get('eps_estimate_revision_down_trailing_30_days')) or 0),
        })

    out.sort(key=lambda r: r['date'])
    return out


def test(symbol='IBM'):
    """Check the key, and whether the plan covers forward estimates.

    These fail independently: OVERVIEW is on every tier, EARNINGS_ESTIMATES is
    the one the tearsheet actually needs and can be gated separately.
    """
    symbol = (symbol or 'IBM').strip().upper()
    ov = overview(symbol)

    profile = {
        'symbol':   ov['symbol'],
        'name':     ov['name'],
        'exchange': ov['exchange'],
        'currency': ov['currency'],
    }

    try:
        rows = annual_estimates(symbol)
    except AlphaVantageError as exc:
        return {'symbol': symbol, 'profile': profile, 'usage': usage(),
                'estimates': {'ok': False, 'detail': str(exc)}}

    if not rows:
        return {'symbol': symbol, 'profile': profile, 'usage': usage(),
                'estimates': {'ok': False,
                              'detail': f'The endpoint answered but published no annual '
                                        f'estimates for {symbol}.'}}

    years = ', '.join(r['label'] for r in rows)
    return {
        'symbol': symbol,
        'profile': profile,
        'usage': usage(),
        'estimates': {
            'ok': True,
            'years': len(rows),
            'detail': f'{len(rows)} year(s) — {years} — with EPS, revenue, '
                      f'high/low dispersion and 30/90-day revision trends.',
        },
    }
