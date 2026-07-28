import os
import time
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


def _seed_idea_types():
    """One-time: populate idea_types from existing idea_type text values."""
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            'SELECT DISTINCT idea_type FROM ideas '
            f'WHERE idea_type IS NOT NULL AND idea_type_id IS NULL'
        )
        names = [r['idea_type'] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
        for name in names:
            if db.IS_PG:
                cur.execute(
                    f'INSERT INTO idea_types (name) VALUES ({db.PH}) '
                    f'ON CONFLICT (name) DO NOTHING',
                    (name,),
                )
                cur.execute(f'SELECT id FROM idea_types WHERE name = {db.PH}', (name,))
            else:
                cur.execute(f'INSERT OR IGNORE INTO idea_types (name) VALUES ({db.PH})', (name,))
                cur.execute(f'SELECT id FROM idea_types WHERE name = {db.PH}', (name,))
            row = db.to_dict(cur.fetchone())
            if row:
                cur.execute(
                    f'UPDATE ideas SET idea_type_id = {db.PH} '
                    f'WHERE idea_type = {db.PH} AND idea_type_id IS NULL',
                    (row['id'], name),
                )


_seed_idea_types()


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


@app.route('/api/idea-types', methods=['GET'])
def get_idea_types():
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute('SELECT id, name FROM idea_types ORDER BY name')
        return jsonify(db.to_dicts(cur.fetchall()))


@app.route('/api/idea-types', methods=['POST'])
def create_idea_type():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        if db.IS_PG:
            cur.execute(
                f'INSERT INTO idea_types (name) VALUES ({db.PH}) '
                f'ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id, name',
                (name,),
            )
            row = db.to_dict(cur.fetchone())
        else:
            cur.execute(f'INSERT OR IGNORE INTO idea_types (name) VALUES ({db.PH})', (name,))
            cur.execute(f'SELECT id, name FROM idea_types WHERE name = {db.PH}', (name,))
            row = db.to_dict(cur.fetchone())
    return jsonify(row), 201


@app.route('/api/idea-types/<int:type_id>', methods=['DELETE'])
def delete_idea_type(type_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'DELETE FROM idea_types WHERE id = {db.PH}', (type_id,))
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
            'i.idea_type_id, it.name AS idea_type_name, '
            'i.subtype_id, st.name AS subtype_name '
            'FROM ideas i '
            'LEFT JOIN sources s    ON i.source_id   = s.id '
            'LEFT JOIN sources ht   ON i.hat_tip_id  = ht.id '
            'LEFT JOIN idea_types it ON i.idea_type_id = it.id '
            'LEFT JOIN subtypes st  ON i.subtype_id  = st.id '
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
    rating        = float(data['rating'])      if data.get('rating')      else None
    subtype_id    = int(data['subtype_id'])    if data.get('subtype_id')  else None
    idea_type_id  = int(data['idea_type_id'])  if data.get('idea_type_id') else None

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
        None,         # idea_type (legacy text, no longer written)
        subtype_id,
        idea_type_id,
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
        rating       = float(data['rating'])     if data.get('rating')      else None
        subtype_id   = int(data['subtype_id'])   if data.get('subtype_id')  else None
        idea_type_id = int(data['idea_type_id']) if data.get('idea_type_id') else None

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
                subtype_id    = {db.PH},
                idea_type_id  = {db.PH}
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
                subtype_id,
                idea_type_id,
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
            f'i.idea_type_id, it.name AS idea_type_name, '
            f'i.subtype_id, st.name AS subtype_name '
            f'FROM ideas i '
            f'LEFT JOIN sources s    ON i.source_id   = s.id '
            f'LEFT JOIN sources ht   ON i.hat_tip_id  = ht.id '
            f'LEFT JOIN idea_types it ON i.idea_type_id = it.id '
            f'LEFT JOIN subtypes st  ON i.subtype_id  = st.id '
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
            f'i.idea_type_id, it.name AS idea_type_name, '
            f'i.subtype_id, st.name AS subtype_name '
            f'FROM ideas i '
            f'LEFT JOIN sources s    ON i.source_id   = s.id '
            f'LEFT JOIN sources ht   ON i.hat_tip_id  = ht.id '
            f'LEFT JOIN idea_types it ON i.idea_type_id = it.id '
            f'LEFT JOIN subtypes st  ON i.subtype_id  = st.id '
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


# ── Projects ─────────────────────────────────────────────────────────────────
# Your own work-in-progress theses, tracked through a pipeline. Distinct from
# the collected ideas above, which are things you're monitoring from others.

STAGES          = ['Initial View', 'Diligence', 'Conviction', 'Invested', 'Passed']
TERMINAL_STAGES = ['Invested', 'Passed']

_PROJECT_SELECT = (
    'SELECT p.id, p.name, p.ticker, p.direction, p.stage, p.thesis, p.rating, '
    'p.current_price, p.origin_idea_id, p.created_at, p.updated_at, '
    'p.attachment_url, p.attachment_name, '
    'p.idea_type_id, it.name AS idea_type_name, '
    'p.subtype_id,   st.name AS subtype_name, '
    'p.source_id,    s.name  AS source_name, '
    'p.hat_tip_id,   ht.name AS hat_tip_name, '
    'oi.ticker AS origin_idea_ticker '
    'FROM projects p '
    'LEFT JOIN idea_types it ON p.idea_type_id   = it.id '
    'LEFT JOIN subtypes st   ON p.subtype_id     = st.id '
    'LEFT JOIN sources s     ON p.source_id      = s.id '
    'LEFT JOIN sources ht    ON p.hat_tip_id     = ht.id '
    'LEFT JOIN ideas oi      ON p.origin_idea_id = oi.id '
)


def _project_form():
    """Pull the common project fields off a JSON or multipart request."""
    if request.content_type and 'multipart' in request.content_type:
        data, file_obj = request.form, request.files.get('file')
    else:
        data, file_obj = request.get_json(force=True), None
    return data, file_obj


def _opt_int(data, key):
    return int(data[key]) if data.get(key) else None


@app.route('/projects')
def projects_page():
    return render_template('projects.html', stages=STAGES, terminal=TERMINAL_STAGES)


@app.route('/api/projects', methods=['GET'])
def get_projects():
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(_PROJECT_SELECT + 'ORDER BY p.updated_at DESC')
        return jsonify(db.to_dicts(cur.fetchall()))


@app.route('/api/projects', methods=['POST'])
def create_project():
    data, file_obj = _project_form()

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400

    stage = data.get('stage') or STAGES[0]
    if stage not in STAGES:
        return jsonify({'error': f'invalid stage: {stage}'}), 400

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

    ticker = (data.get('ticker') or '').strip().upper() or None
    now    = datetime.now().isoformat()

    values = (
        name,
        ticker,
        data.get('direction') or None,
        stage,
        data.get('thesis') or None,
        float(data['rating']) if data.get('rating') else None,
        _opt_int(data, 'idea_type_id'),
        _opt_int(data, 'subtype_id'),
        _opt_int(data, 'source_id'),
        _opt_int(data, 'hat_tip_id'),
        _opt_int(data, 'origin_idea_id'),
        fetch_current_price(ticker) if ticker else None,
        now,
        now,
        attachment_url,
        attachment_name,
        attachment_data,
        attachment_key,
    )
    with db.get_conn() as conn:
        row = db.insert_project(conn, values)
    return jsonify(row), 201


@app.route('/api/projects/<int:project_id>', methods=['PUT'])
def update_project(project_id):
    data, file_obj = _project_form()

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400

    stage = data.get('stage') or STAGES[0]
    if stage not in STAGES:
        return jsonify({'error': f'invalid stage: {stage}'}), 400

    ticker = (data.get('ticker') or '').strip().upper() or None

    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'''UPDATE projects SET
                name         = {db.PH},
                ticker       = {db.PH},
                direction    = {db.PH},
                stage        = {db.PH},
                thesis       = {db.PH},
                rating       = {db.PH},
                idea_type_id = {db.PH},
                subtype_id   = {db.PH},
                source_id    = {db.PH},
                hat_tip_id   = {db.PH},
                updated_at   = {db.PH}
            WHERE id = {db.PH}''',
            (
                name,
                ticker,
                data.get('direction') or None,
                stage,
                data.get('thesis') or None,
                float(data['rating']) if data.get('rating') else None,
                _opt_int(data, 'idea_type_id'),
                _opt_int(data, 'subtype_id'),
                _opt_int(data, 'source_id'),
                _opt_int(data, 'hat_tip_id'),
                datetime.now().isoformat(),
                project_id,
            ),
        )

        cur.execute(f'SELECT attachment_key FROM projects WHERE id = {db.PH}', (project_id,))
        old_key = (db.to_dict(cur.fetchone()) or {}).get('attachment_key')

        _set_att = (
            f'UPDATE projects SET attachment_url={db.PH}, attachment_name={db.PH}, '
            f'attachment_data={db.PH}, attachment_key={db.PH} WHERE id={db.PH}'
        )
        if data.get('clear_attachment') == 'true':
            if old_key:
                storage.delete(old_key)
            cur.execute(_set_att, (None, None, None, None, project_id))
        elif file_obj and file_obj.filename:
            raw = file_obj.read()
            if storage.ENABLED:
                try:
                    if old_key:
                        storage.delete(old_key)
                    new_key = storage.upload(raw, file_obj.filename)
                except Exception as exc:
                    return jsonify({'error': f'Bucket upload failed: {exc}'}), 502
                cur.execute(_set_att, (None, file_obj.filename, None, new_key, project_id))
            else:
                blob = db._pg_binary(raw) if db.IS_PG else raw
                cur.execute(_set_att, (None, file_obj.filename, blob, None, project_id))
        elif 'attachment_url' in data:
            if old_key:
                storage.delete(old_key)
            cur.execute(_set_att, (data.get('attachment_url') or None, None, None, None, project_id))

        cur.execute(_PROJECT_SELECT + f'WHERE p.id = {db.PH}', (project_id,))
        row = db.to_dict(cur.fetchone())

    return jsonify(row)


@app.route('/api/projects/<int:project_id>/stage', methods=['PATCH'])
def set_project_stage(project_id):
    """Move a project to a different stage — used by the board's inline control."""
    stage = (request.get_json(force=True).get('stage') or '').strip()
    if stage not in STAGES:
        return jsonify({'error': f'invalid stage: {stage}'}), 400
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE projects SET stage = {db.PH}, updated_at = {db.PH} WHERE id = {db.PH}',
            (stage, datetime.now().isoformat(), project_id),
        )
        cur.execute(_PROJECT_SELECT + f'WHERE p.id = {db.PH}', (project_id,))
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(row)


@app.route('/api/projects/<int:project_id>', methods=['DELETE'])
def delete_project(project_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT attachment_key FROM projects WHERE id = {db.PH}', (project_id,))
        row = db.to_dict(cur.fetchone()) or {}
        if row.get('attachment_key'):
            storage.delete(row['attachment_key'])
        cur.execute(f'DELETE FROM projects WHERE id = {db.PH}', (project_id,))
    return jsonify({'ok': True})


@app.route('/api/projects/<int:project_id>/attachment')
def get_project_attachment(project_id):
    from flask import send_file, redirect
    from io import BytesIO
    import mimetypes

    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT attachment_name, attachment_data, attachment_key '
            f'FROM projects WHERE id = {db.PH}',
            (project_id,),
        )
        row = db.to_dict(cur.fetchone())

    if not row or (not row.get('attachment_key') and not row.get('attachment_data')):
        return jsonify({'error': 'No file attachment'}), 404

    if row.get('attachment_key'):
        return redirect(storage.presigned_url(row['attachment_key']))

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


@app.route('/api/projects/<int:project_id>/refresh-price', methods=['POST'])
def refresh_project_price(project_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT ticker FROM projects WHERE id = {db.PH}', (project_id,))
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    if not row['ticker']:
        return jsonify({'error': 'Project has no ticker'}), 400
    price = fetch_current_price(row['ticker'])
    if price is None:
        return jsonify({'error': f'Could not fetch price for {row["ticker"]}'}), 502
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE projects SET current_price = {db.PH} WHERE id = {db.PH}',
            (price, project_id),
        )
    return jsonify({'price': price})


@app.route('/api/ideas/<int:idea_id>/promote', methods=['POST'])
def promote_idea(idea_id):
    """Spin a collected idea into a project you're actively working on."""
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT ticker, direction, thesis, rating, idea_type_id, subtype_id, '
            f'source_id, hat_tip_id, current_price FROM ideas WHERE id = {db.PH}',
            (idea_id,),
        )
        idea = db.to_dict(cur.fetchone())
    if not idea:
        return jsonify({'error': 'Idea not found'}), 404

    now = datetime.now().isoformat()
    values = (
        idea['ticker'],
        idea['ticker'],
        idea['direction'],
        STAGES[0],
        idea['thesis'],
        idea['rating'],
        idea['idea_type_id'],
        idea['subtype_id'],
        idea['source_id'],
        idea['hat_tip_id'],
        idea_id,
        idea['current_price'],
        now,
        now,
        None, None, None, None,
    )
    with db.get_conn() as conn:
        row = db.insert_project(conn, values)
    return jsonify(row), 201


@app.route('/projects/<int:project_id>')
def project_detail(project_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(_PROJECT_SELECT + f'WHERE p.id = {db.PH}', (project_id,))
        project = db.to_dict(cur.fetchone())
    if not project:
        return 'Project not found', 404
    return render_template(
        'project.html', project=project, stages=STAGES, terminal=TERMINAL_STAGES
    )


# ── Market data: price chart + tearsheet ─────────────────────────────────────
# Yahoo calls are slow (1–3s), so results are cached in-process for a while.
# Note on coverage: Yahoo supplies forward estimates for EPS and revenue only,
# and only two periods out. Forward EBITDA/EBIT/FCF — and therefore forward
# EV/EBITDA, FCF yield, and forward margins — do not exist in the feed, so the
# margin and capex figures below are trailing.

BENCHMARK       = '^GSPC'
BENCHMARK_LABEL = 'S&P 500'
CHART_PERIODS   = ('1mo', '3mo', 'ytd', '1y', '3y', '5y')

_cache = {}


def _cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def _num(v):
    """Coerce to a JSON-safe float, treating NaN/None/non-numerics as null."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None   # NaN != NaN


def _first(d, *keys):
    for k in keys:
        v = _num(d.get(k))
        if v is not None:
            return v
    return None


def _closes(ticker, period):
    """Date -> close for a ticker over one of CHART_PERIODS."""
    kwargs = {
        'interval':    '1wk' if period in ('3y', '5y') else '1d',
        'auto_adjust': True,
    }
    # yfinance has no native '3y' period, so anchor it with an explicit start.
    if period == '3y':
        kwargs['start'] = (datetime.now() - timedelta(days=365 * 3)).strftime('%Y-%m-%d')
    else:
        kwargs['period'] = period

    df = yf.Ticker(ticker).history(**kwargs)
    if df.empty or 'Close' not in df.columns:
        return {}
    out = {}
    for dt, close in df['Close'].items():
        v = _num(close)
        if v is not None:
            out[dt.strftime('%Y-%m-%d')] = v
    return out


def _build_history(ticker, period):
    stock = _closes(ticker, period)
    bench = _closes(BENCHMARK, period)
    # Intersect so both lines share an x-axis; US equities and the S&P trade
    # the same sessions, so this drops almost nothing.
    dates = sorted(set(stock) & set(bench))
    return {
        'ticker':          ticker,
        'period':          period,
        'benchmark':       BENCHMARK,
        'benchmark_label': BENCHMARK_LABEL,
        'dates':           dates,
        'stock':           [stock[d] for d in dates],
        'bench':           [bench[d] for d in dates],
    }


@app.route('/api/market/<ticker>/history')
def market_history(ticker):
    period = (request.args.get('period') or '1y').lower()
    if period not in CHART_PERIODS:
        return jsonify({'error': f'invalid period: {period}'}), 400
    ticker = ticker.strip().upper()
    try:
        data = _cached(f'hist:{ticker}:{period}', 900, lambda: _build_history(ticker, period))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502
    if not data['dates']:
        return jsonify({'error': f'No price history for {ticker}'}), 404
    return jsonify(data)


def _trailing_capex(t):
    """Most recent annual capex as a positive number, from the cash-flow statement."""
    try:
        cf = t.cashflow
        if cf is None or cf.empty:
            return None
        for label in ('Capital Expenditure', 'Capital Expenditures'):
            if label in cf.index:
                return abs(_num(cf.loc[label].iloc[0]) or 0) or None
    except Exception:
        pass
    return None


def _consensus(t, price, mcap):
    """Forward EPS/revenue estimates — the only forward data Yahoo provides."""
    rows = []
    try:
        eps_est = t.earnings_estimate
        rev_est = t.revenue_estimate
    except Exception:
        return rows

    for key, label in (('0y', 'Current FY'), ('+1y', 'Next FY')):
        eps = eps_growth = rev = rev_growth = None
        try:
            if eps_est is not None and key in eps_est.index:
                eps        = _num(eps_est.loc[key, 'avg'])
                eps_growth = _num(eps_est.loc[key, 'growth'])
        except Exception:
            pass
        try:
            if rev_est is not None and key in rev_est.index:
                rev        = _num(rev_est.loc[key, 'avg'])
                rev_growth = _num(rev_est.loc[key, 'growth'])
        except Exception:
            pass
        if eps is None and rev is None:
            continue
        rows.append({
            'label':        label,
            'eps':          eps,
            'eps_growth':   eps_growth,
            'revenue':      rev,
            'rev_growth':   rev_growth,
            'pe':           (price / eps) if price and eps else None,
            'price_sales':  (mcap / rev)  if mcap  and rev else None,
        })
    return rows


def _build_tearsheet(ticker):
    t    = yf.Ticker(ticker)
    info = t.info or {}

    price   = _first(info, 'currentPrice', 'regularMarketPrice')
    shares  = _num(info.get('sharesOutstanding'))
    mcap    = _num(info.get('marketCap'))
    debt    = _num(info.get('totalDebt'))
    cash    = _num(info.get('totalCash'))
    ev      = _num(info.get('enterpriseValue'))
    ebitda  = _num(info.get('ebitda'))
    revenue = _num(info.get('totalRevenue'))
    avg_vol = _num(info.get('averageVolume'))

    net_debt = (debt - cash) if debt is not None and cash is not None else None
    # Whatever the EV bridge can't explain with market cap and net debt —
    # minorities, prefs, and the like.
    other = (ev - mcap - net_debt) if None not in (ev, mcap, net_debt) else None
    capex = _trailing_capex(t)

    def ratio(num, den):
        return (num / den) if num is not None and den else None

    return {
        'ticker':  ticker,
        'name':    info.get('shortName') or info.get('longName'),
        'as_of':   datetime.now().isoformat(),
        'capitalization': {
            'price':      price,
            'shares':     shares,
            'market_cap': mcap,
            'debt':       debt,
            'cash':       cash,
            'net_debt':   net_debt,
            'other':      other,
            'ev':         ev,
        },
        'technicals': {
            'high_52w':       _num(info.get('fiftyTwoWeekHigh')),
            'low_52w':        _num(info.get('fiftyTwoWeekLow')),
            'adtv_3m_mm':     (avg_vol * price / 1e6) if avg_vol and price else None,
            'short_pct_float': (lambda v: v * 100 if v is not None else None)(
                                   _num(info.get('shortPercentOfFloat'))),
            'days_to_cover':  _num(info.get('shortRatio')),
            'dividend_yield': _num(info.get('dividendYield')),
        },
        'credit': {
            'gross_leverage': ratio(debt,     ebitda),
            'net_leverage':   ratio(net_debt, ebitda),
            'debt_to_equity': _num(info.get('debtToEquity')),
        },
        'valuation': {
            'pe':          _num(info.get('trailingPE')),
            'forward_pe':  _num(info.get('forwardPE')),
            'ev_ebitda':   _num(info.get('enterpriseToEbitda')),
            'ev_revenue':  _num(info.get('enterpriseToRevenue')),
            'price_sales': _first(info, 'priceToSalesTrailing12Months') or ratio(mcap, revenue),
            'fcf_yield':   (lambda f: (f / mcap * 100) if f is not None and mcap else None)(
                               _num(info.get('freeCashflow'))),
        },
        'margins': {
            'gross':           (lambda v: v * 100 if v is not None else None)(_num(info.get('grossMargins'))),
            'ebitda':          (lambda v: v * 100 if v is not None else None)(_num(info.get('ebitdaMargins'))),
            'operating':       (lambda v: v * 100 if v is not None else None)(_num(info.get('operatingMargins'))),
            'capex_pct_sales': (lambda v: v * 100 if v is not None else None)(ratio(capex, revenue)),
            'capex_pct_ebitda':(lambda v: v * 100 if v is not None else None)(ratio(capex, ebitda)),
        },
        'growth': {
            'revenue':  (lambda v: v * 100 if v is not None else None)(_num(info.get('revenueGrowth'))),
            'earnings': (lambda v: v * 100 if v is not None else None)(_num(info.get('earningsGrowth'))),
        },
        'consensus': _consensus(t, price, mcap),
        'analyst': {
            'target':         _num(info.get('targetMeanPrice')),
            'recommendation': info.get('recommendationKey'),
        },
    }


@app.route('/api/market/<ticker>/tearsheet')
def market_tearsheet(ticker):
    ticker = ticker.strip().upper()
    try:
        data = _cached(f'tear:{ticker}', 900, lambda: _build_tearsheet(ticker))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502
    if data['capitalization']['price'] is None:
        return jsonify({'error': f'No market data for {ticker}'}), 404
    return jsonify(data)


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
