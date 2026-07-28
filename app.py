import os
from flask import Flask, request, jsonify, render_template
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import db
import storage

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20 MB upload limit
db.init()
db.migrate()


# ── Price helpers ────────────────────────────────────────────────────────────

def _flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_historical_price(ticker, date_str):
    target = datetime.strptime(date_str, '%Y-%m-%d')
    # end is exclusive in yfinance, so +1 day to include the target date.
    # If target is a weekend/holiday, iloc[-1] gives the most recent prior close.
    start = (target - timedelta(days=7)).strftime('%Y-%m-%d')
    end   = target.strftime('%Y-%m-%d')  # exclusive — gives prior trading day's close
    df = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
    if df.empty or 'Close' not in df.columns:
        return None
    return round(float(df['Close'].iloc[-1]), 4)


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


@app.route('/api/sources', methods=['GET'])
def get_sources():
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute('SELECT id, name FROM sources ORDER BY name')
        return jsonify(db.to_dicts(cur.fetchall()))


@app.route('/api/sources', methods=['POST'])
def create_source():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        if db.IS_PG:
            cur.execute(
                f'INSERT INTO sources (name) VALUES ({db.PH}) '
                f'ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id, name',
                (name,),
            )
            row = db.to_dict(cur.fetchone())
        else:
            cur.execute(f'INSERT OR IGNORE INTO sources (name) VALUES ({db.PH})', (name,))
            cur.execute(f'SELECT id, name FROM sources WHERE name = {db.PH}', (name,))
            row = db.to_dict(cur.fetchone())
    return jsonify(row), 201


@app.route('/api/sources/<int:source_id>', methods=['DELETE'])
def delete_source(source_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'DELETE FROM sources WHERE id = {db.PH}', (source_id,))
    return jsonify({'ok': True})


@app.route('/api/subtypes', methods=['GET'])
def get_subtypes():
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute('SELECT id, name FROM subtypes ORDER BY name')
        return jsonify(db.to_dicts(cur.fetchall()))


@app.route('/api/subtypes', methods=['POST'])
def create_subtype():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        if db.IS_PG:
            cur.execute(
                f'INSERT INTO subtypes (name) VALUES ({db.PH}) '
                f'ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id, name',
                (name,),
            )
            row = db.to_dict(cur.fetchone())
        else:
            cur.execute(f'INSERT OR IGNORE INTO subtypes (name) VALUES ({db.PH})', (name,))
            cur.execute(f'SELECT id, name FROM subtypes WHERE name = {db.PH}', (name,))
            row = db.to_dict(cur.fetchone())
    return jsonify(row), 201


@app.route('/api/subtypes/<int:subtype_id>', methods=['DELETE'])
def delete_subtype(subtype_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'DELETE FROM subtypes WHERE id = {db.PH}', (subtype_id,))
    return jsonify({'ok': True})


@app.route('/api/ideas', methods=['GET'])
def get_ideas():
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            'SELECT i.id, i.ticker, i.idea_date, i.idea_price, i.initial_date, i.initial_price, '
            'i.current_price, i.thesis, i.direction, i.asset_class, i.created_at, '
            'i.attachment_url, i.attachment_name, i.source_id, s.name AS source_name, '
            'i.hat_tip_id, ht.name AS hat_tip_name, i.rating, i.idea_type, '
            'i.subtype_id, st.name AS subtype_name '
            'FROM ideas i '
            'LEFT JOIN sources s   ON i.source_id  = s.id '
            'LEFT JOIN sources ht  ON i.hat_tip_id = ht.id '
            'LEFT JOIN subtypes st ON i.subtype_id = st.id '
            'ORDER BY i.created_at DESC'
        )
        return jsonify(db.to_dicts(cur.fetchall()))


@app.route('/api/ideas', methods=['POST'])
def create_idea():
    # Accept multipart/form-data (file uploads) or plain JSON
    if request.content_type and 'multipart' in request.content_type:
        data = request.form
        file_obj = request.files.get('file')
    else:
        data = request.get_json(force=True)
        file_obj = None

    for field in ('ticker', 'idea_date', 'initial_date', 'thesis', 'direction', 'asset_class'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    attachment_url  = data.get('attachment_url') or None
    attachment_name = None
    attachment_data = None
    attachment_key  = None
    if file_obj and file_obj.filename:
        attachment_name = file_obj.filename
        raw = file_obj.read()
        if storage.ENABLED:
            try:
                attachment_key = storage.upload(raw, file_obj.filename)
            except Exception as exc:
                return jsonify({'error': f'Bucket upload failed: {exc}'}), 502
        else:
            attachment_data = raw

    source_id   = int(data['source_id'])   if data.get('source_id')   else None
    hat_tip_id  = int(data['hat_tip_id'])  if data.get('hat_tip_id')  else None
    rating      = float(data['rating'])    if data.get('rating')      else None
    idea_type   = data.get('idea_type') or None
    subtype_id  = int(data['subtype_id'])  if data.get('subtype_id')  else None

    values = (
        data['ticker'].upper(),
        data['idea_date'],
        float(data['idea_price'])    if data.get('idea_price')    else None,
        data['initial_date'],
        float(data['initial_price']) if data.get('initial_price') else None,
        float(data['current_price']) if data.get('current_price') else None,
        data['thesis'],
        data['direction'],
        data['asset_class'],
        source_id,
        hat_tip_id,
        rating,
        idea_type,
        subtype_id,
        datetime.now().isoformat(),
        attachment_url,
        attachment_name,
        attachment_data,
        attachment_key,
    )
    with db.get_conn() as conn:
        row = db.insert_idea(conn, values)
    return jsonify(row), 201


@app.route('/api/ideas/<int:idea_id>/attachment')
def get_attachment(idea_id):
    from flask import send_file, redirect
    from io import BytesIO
    import mimetypes

    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT attachment_name, attachment_data, attachment_key FROM ideas WHERE id = {db.PH}',
            (idea_id,),
        )
        row = db.to_dict(cur.fetchone())

    if not row or (not row.get('attachment_key') and not row.get('attachment_data')):
        return jsonify({'error': 'No file attachment'}), 404

    # Bucket storage: redirect to a short-lived presigned URL
    if row.get('attachment_key'):
        url = storage.presigned_url(row['attachment_key'])
        return redirect(url)

    # Legacy: binary stored in DB
    data = row['attachment_data']
    if isinstance(data, memoryview):
        data = bytes(data)
    mime = mimetypes.guess_type(row['attachment_name'])[0] or 'application/octet-stream'
    return send_file(
        BytesIO(data),
        download_name=row['attachment_name'],
        as_attachment=False,
        mimetype=mime,
    )


@app.route('/api/ideas/<int:idea_id>', methods=['PUT'])
def update_idea(idea_id):
    if request.content_type and 'multipart' in request.content_type:
        data     = request.form
        file_obj = request.files.get('file')
    else:
        data     = request.get_json(force=True)
        file_obj = None

    for field in ('ticker', 'idea_date', 'initial_date', 'thesis', 'direction', 'asset_class'):
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    with db.get_conn() as conn:
        cur = db.cursor(conn)

        source_id  = int(data['source_id'])  if data.get('source_id')  else None
        hat_tip_id = int(data['hat_tip_id']) if data.get('hat_tip_id') else None
        rating     = float(data['rating'])   if data.get('rating')     else None
        idea_type  = data.get('idea_type') or None
        subtype_id = int(data['subtype_id']) if data.get('subtype_id') else None

        # Always update core fields
        cur.execute(
            f'''UPDATE ideas SET
                ticker        = {db.PH},
                idea_date     = {db.PH},
                idea_price    = {db.PH},
                initial_date  = {db.PH},
                initial_price = {db.PH},
                current_price = {db.PH},
                thesis        = {db.PH},
                direction     = {db.PH},
                asset_class   = {db.PH},
                source_id     = {db.PH},
                hat_tip_id    = {db.PH},
                rating        = {db.PH},
                idea_type     = {db.PH},
                subtype_id    = {db.PH}
            WHERE id = {db.PH}''',
            (
                data['ticker'].upper(),
                data['idea_date'],
                float(data['idea_price'])    if data.get('idea_price')    else None,
                data['initial_date'],
                float(data['initial_price']) if data.get('initial_price') else None,
                float(data['current_price']) if data.get('current_price') else None,
                data['thesis'],
                data['direction'],
                data['asset_class'],
                source_id,
                hat_tip_id,
                rating,
                idea_type,
                subtype_id,
                idea_id,
            ),
        )

        # Fetch existing attachment_key so we can delete the old bucket object if replaced
        cur.execute(f'SELECT attachment_key FROM ideas WHERE id = {db.PH}', (idea_id,))
        existing = db.to_dict(cur.fetchone()) or {}
        old_key = existing.get('attachment_key')

        # Update attachment only when the client explicitly changed it
        clear = data.get('clear_attachment') == 'true'
        if clear:
            if old_key:
                storage.delete(old_key)
            cur.execute(
                f'UPDATE ideas SET attachment_url={db.PH}, attachment_name={db.PH}, '
                f'attachment_data={db.PH}, attachment_key={db.PH} WHERE id={db.PH}',
                (None, None, None, None, idea_id),
            )
        elif file_obj and file_obj.filename:
            raw = file_obj.read()
            if storage.ENABLED:
                try:
                    if old_key:
                        storage.delete(old_key)
                    new_key = storage.upload(raw, file_obj.filename)
                except Exception as exc:
                    return jsonify({'error': f'Bucket upload failed: {exc}'}), 502
                cur.execute(
                    f'UPDATE ideas SET attachment_url={db.PH}, attachment_name={db.PH}, '
                    f'attachment_data={db.PH}, attachment_key={db.PH} WHERE id={db.PH}',
                    (None, file_obj.filename, None, new_key, idea_id),
                )
            else:
                blob = db._pg_binary(raw) if db.IS_PG else raw
                cur.execute(
                    f'UPDATE ideas SET attachment_url={db.PH}, attachment_name={db.PH}, '
                    f'attachment_data={db.PH}, attachment_key={db.PH} WHERE id={db.PH}',
                    (None, file_obj.filename, blob, None, idea_id),
                )
        elif 'attachment_url' in data:
            if old_key:
                storage.delete(old_key)
            cur.execute(
                f'UPDATE ideas SET attachment_url={db.PH}, attachment_name={db.PH}, '
                f'attachment_data={db.PH}, attachment_key={db.PH} WHERE id={db.PH}',
                (data.get('attachment_url') or None, None, None, None, idea_id),
            )
        # else: attachment untouched

        cur.execute(
            f'SELECT i.id, i.ticker, i.idea_date, i.idea_price, i.initial_date, i.initial_price, '
            f'i.current_price, i.thesis, i.direction, i.asset_class, i.created_at, '
            f'i.attachment_url, i.attachment_name, i.source_id, s.name AS source_name, '
            f'i.hat_tip_id, ht.name AS hat_tip_name, i.rating, i.idea_type, '
            f'i.subtype_id, st.name AS subtype_name '
            f'FROM ideas i '
            f'LEFT JOIN sources s   ON i.source_id  = s.id '
            f'LEFT JOIN sources ht  ON i.hat_tip_id = ht.id '
            f'LEFT JOIN subtypes st ON i.subtype_id = st.id '
            f'WHERE i.id = {db.PH}',
            (idea_id,),
        )
        row = db.to_dict(cur.fetchone())

    return jsonify(row)


@app.route('/api/ideas/<int:idea_id>', methods=['DELETE'])
def delete_idea(idea_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT attachment_key FROM ideas WHERE id = {db.PH}', (idea_id,))
        row = db.to_dict(cur.fetchone()) or {}
        if row.get('attachment_key'):
            storage.delete(row['attachment_key'])
        cur.execute(f'DELETE FROM ideas WHERE id = {db.PH}', (idea_id,))
    return jsonify({'ok': True})


@app.route('/ideas/<int:idea_id>')
def idea_detail(idea_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT i.id, i.ticker, i.idea_date, i.idea_price, i.initial_date, i.initial_price, '
            f'i.current_price, i.thesis, i.direction, i.asset_class, i.created_at, '
            f'i.attachment_url, i.attachment_name, i.source_id, s.name AS source_name, '
            f'i.hat_tip_id, ht.name AS hat_tip_name, i.rating, i.idea_type, '
            f'i.subtype_id, st.name AS subtype_name '
            f'FROM ideas i '
            f'LEFT JOIN sources s   ON i.source_id  = s.id '
            f'LEFT JOIN sources ht  ON i.hat_tip_id = ht.id '
            f'LEFT JOIN subtypes st ON i.subtype_id = st.id '
            f'WHERE i.id = {db.PH}',
            (idea_id,),
        )
        idea = db.to_dict(cur.fetchone())
    if not idea:
        return 'Idea not found', 404

    def pct(f, t):
        if f and t:
            return ((t - f) / f) * 100
        return None

    idea['change_idea_initial']  = pct(idea['idea_price'],    idea['initial_price'])
    idea['change_initial_today'] = pct(idea['initial_price'], idea['current_price'])
    idea['change_idea_current']  = pct(idea['idea_price'],    idea['current_price'])
    return render_template('idea.html', idea=idea)


@app.route('/api/ideas/<int:idea_id>/refresh-price', methods=['POST'])
def refresh_single_price(idea_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT ticker FROM ideas WHERE id = {db.PH}', (idea_id,))
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    price = fetch_current_price(row['ticker'])
    if price is None:
        return jsonify({'error': f'Could not fetch price for {row["ticker"]}'}), 502
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE ideas SET current_price = {db.PH} WHERE id = {db.PH}',
            (price, idea_id),
        )
    return jsonify({'price': price})


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
