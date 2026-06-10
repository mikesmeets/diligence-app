import os
from flask import Flask, request, jsonify, render_template
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import db

app = Flask(__name__)
db.init()


# ── Price helpers ────────────────────────────────────────────────────────────

def _flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_historical_price(ticker, date_str):
    target = datetime.strptime(date_str, '%Y-%m-%d')
    start  = (target - timedelta(days=7)).strftime('%Y-%m-%d')
    end    = (target + timedelta(days=4)).strftime('%Y-%m-%d')
    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
    df = _flatten(df)
    if df.empty or 'Close' not in df.columns:
        return None
    df.index = df.index.tz_localize(None)
    idx = (df.index - target).abs().argmin()
    return round(float(df['Close'].iloc[idx]), 4)


def fetch_current_price(ticker):
    try:
        info  = yf.Ticker(ticker).fast_info
        price = info.get('lastPrice') or info.get('regularMarketPrice')
        if price:
            return round(float(price), 4)
    except Exception:
        pass
    start = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    df    = yf.download(ticker, start=start, progress=False, auto_adjust=True)
    df    = _flatten(df)
    if not df.empty and 'Close' in df.columns:
        return round(float(df['Close'].iloc[-1]), 4)
    return None


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/ideas', methods=['GET'])
def get_ideas():
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute('SELECT * FROM ideas ORDER BY created_at DESC')
        return jsonify(db.to_dicts(cur.fetchall()))


@app.route('/api/ideas', methods=['POST'])
def create_idea():
    data = request.get_json(force=True)
    for field in ('ticker', 'idea_date', 'initial_date', 'thesis', 'direction', 'asset_class'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    values = (
        data['ticker'].upper(),
        data['idea_date'],
        data.get('idea_price'),
        data['initial_date'],
        data.get('initial_price'),
        data.get('current_price'),
        data['thesis'],
        data['direction'],
        data['asset_class'],
        datetime.now().isoformat(),
    )
    with db.get_conn() as conn:
        row = db.insert_idea(conn, values)
    return jsonify(row), 201


@app.route('/api/ideas/<int:idea_id>', methods=['DELETE'])
def delete_idea(idea_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'DELETE FROM ideas WHERE id = {db.PH}', (idea_id,))
    return jsonify({'ok': True})


@app.route('/api/stock-price')
def stock_price():
    ticker = request.args.get('ticker', '').strip().upper()
    date   = request.args.get('date', 'today').strip()
    if not ticker:
        return jsonify({'error': 'ticker is required'}), 400
    try:
        price = (
            fetch_current_price(ticker)
            if date == 'today'
            else fetch_historical_price(ticker, date)
        )
        if price is None:
            return jsonify({'error': f'No price data found for {ticker}'}), 404
        return jsonify({'price': price, 'ticker': ticker})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ideas/refresh-prices', methods=['POST'])
def refresh_prices():
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute("SELECT id, ticker FROM ideas WHERE asset_class = 'public_equity'")
        rows = db.to_dicts(cur.fetchall())

    updated = 0
    for row in rows:
        price = fetch_current_price(row['ticker'])
        if price is not None:
            with db.get_conn() as conn:
                cur = db.cursor(conn)
                cur.execute(
                    f'UPDATE ideas SET current_price = {db.PH} WHERE id = {db.PH}',
                    (price, row['id']),
                )
            updated += 1

    return jsonify({'updated': updated})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
