from flask import Flask, request, jsonify, render_template
import sqlite3
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

app = Flask(__name__)
DB_PATH = 'ideas.db'


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ideas (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker      TEXT    NOT NULL,
                idea_date   TEXT    NOT NULL,
                idea_price  REAL,
                initial_date  TEXT  NOT NULL,
                initial_price REAL,
                current_price REAL,
                thesis      TEXT    NOT NULL,
                direction   TEXT    NOT NULL,
                asset_class TEXT    NOT NULL,
                created_at  TEXT    NOT NULL
            )
        ''')
        conn.commit()


init_db()


def _flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_historical_price(ticker, date_str):
    target = datetime.strptime(date_str, '%Y-%m-%d')
    start = (target - timedelta(days=7)).strftime('%Y-%m-%d')
    end = (target + timedelta(days=4)).strftime('%Y-%m-%d')
    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
    df = _flatten(df)
    if df.empty or 'Close' not in df.columns:
        return None
    df.index = df.index.tz_localize(None)
    closest_idx = (df.index - target).abs().argmin()
    return round(float(df['Close'].iloc[closest_idx]), 4)


def fetch_current_price(ticker):
    try:
        info = yf.Ticker(ticker).fast_info
        price = info.get('lastPrice') or info.get('regularMarketPrice')
        if price:
            return round(float(price), 4)
    except Exception:
        pass
    # Fallback: recent download
    start = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
    df = _flatten(df)
    if not df.empty and 'Close' in df.columns:
        return round(float(df['Close'].iloc[-1]), 4)
    return None


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/ideas', methods=['GET'])
def get_ideas():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM ideas ORDER BY created_at DESC').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/ideas', methods=['POST'])
def create_idea():
    data = request.get_json(force=True)
    for field in ('ticker', 'idea_date', 'initial_date', 'thesis', 'direction', 'asset_class'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    created_at = datetime.now().isoformat()
    with get_db() as conn:
        cur = conn.execute(
            '''INSERT INTO ideas
               (ticker, idea_date, idea_price, initial_date, initial_price, current_price,
                thesis, direction, asset_class, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (
                data['ticker'].upper(),
                data['idea_date'],
                data.get('idea_price'),
                data['initial_date'],
                data.get('initial_price'),
                data.get('current_price'),
                data['thesis'],
                data['direction'],
                data['asset_class'],
                created_at,
            ),
        )
        conn.commit()
        row = conn.execute('SELECT * FROM ideas WHERE id = ?', (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route('/api/ideas/<int:idea_id>', methods=['DELETE'])
def delete_idea(idea_id):
    with get_db() as conn:
        conn.execute('DELETE FROM ideas WHERE id = ?', (idea_id,))
        conn.commit()
    return jsonify({'ok': True})


@app.route('/api/stock-price')
def stock_price():
    ticker = request.args.get('ticker', '').strip().upper()
    date = request.args.get('date', 'today').strip()
    if not ticker:
        return jsonify({'error': 'ticker is required'}), 400
    try:
        price = fetch_current_price(ticker) if date == 'today' else fetch_historical_price(ticker, date)
        if price is None:
            return jsonify({'error': f'No price data found for {ticker}'}), 404
        return jsonify({'price': price, 'ticker': ticker})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ideas/refresh-prices', methods=['POST'])
def refresh_prices():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, ticker FROM ideas WHERE asset_class = 'public_equity'"
        ).fetchall()

    updated = 0
    for row in rows:
        price = fetch_current_price(row['ticker'])
        if price is not None:
            with get_db() as conn:
                conn.execute('UPDATE ideas SET current_price = ? WHERE id = ?', (price, row['id']))
                conn.commit()
            updated += 1

    return jsonify({'updated': updated})


if __name__ == '__main__':
    app.run(debug=True, port=5000)
