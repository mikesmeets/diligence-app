"""
Financial Modeling Prep client.

Credentials follow the same rule as the Anthropic key: the FMP_API_KEY
environment variable wins if set, and the settings table is the fallback. That
way a key provisioned on the host can't be overridden from the admin page.

FMP moved its current endpoints to /stable/; /api/v3/ is the legacy prefix and
several endpoints only exist there, so both are reachable.
"""
import logging
import os

import requests

import db

BASE_STABLE = 'https://financialmodelingprep.com/stable'
BASE_LEGACY = 'https://financialmodelingprep.com/api/v3'

TIMEOUT = 20


class NotConfigured(Exception):
    """No FMP key available from either the environment or settings."""


class FMPError(Exception):
    """The API refused or failed the request."""


def api_key():
    return os.environ.get('FMP_API_KEY') or db.get_setting('fmp_api_key') or ''


def key_source():
    """Where the active key comes from — surfaced in Admin, never the key."""
    if os.environ.get('FMP_API_KEY'):
        return 'env'
    if db.get_setting('fmp_api_key'):
        return 'settings'
    return None


def enabled():
    return bool(api_key())


def get(path, legacy=False, **params):
    """Call one endpoint and return the decoded body.

    Raises NotConfigured when there's no key, FMPError for anything the API
    rejects — including the 200-with-an-error-message shape it sometimes uses.
    """
    key = api_key()
    if not key:
        raise NotConfigured(
            'No FMP API key. Set the FMP_API_KEY environment variable, or add a '
            'key on the Admin page.'
        )

    base = BASE_LEGACY if legacy else BASE_STABLE
    url = f'{base}/{path.lstrip("/")}'
    try:
        resp = requests.get(url, params={**params, 'apikey': key}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise FMPError(f'Could not reach FMP: {exc}') from exc

    if resp.status_code in (401, 403):
        raise FMPError('FMP rejected the API key (401/403). Check it on the Admin page.')
    if resp.status_code == 429:
        raise FMPError('FMP rate limit reached — wait a moment and try again.')
    if resp.status_code >= 400:
        raise FMPError(f'FMP returned {resp.status_code}: {resp.text[:200]}')

    try:
        data = resp.json()
    except ValueError:
        raise FMPError(f'FMP returned something that is not JSON: {resp.text[:200]}')

    # FMP reports some failures with a 200 and an error body.
    if isinstance(data, dict):
        message = data.get('Error Message') or data.get('error')
        if message:
            raise FMPError(str(message))
    return data


def test():
    """Validate the key with the cheapest call available."""
    data = get('profile', symbol='AAPL')
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict) or not (row.get('symbol') or row.get('companyName')):
        raise FMPError('The key worked but the response was not what was expected.')
    return {
        'symbol': row.get('symbol'),
        'name': row.get('companyName'),
        'price': row.get('price'),
        'currency': row.get('currency'),
        'exchange': row.get('exchange') or row.get('exchangeShortName'),
    }
