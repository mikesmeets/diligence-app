import logging
import os
import re
import threading
import time
import traceback
from flask import Flask, request, jsonify, render_template
from werkzeug.exceptions import HTTPException
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import ai
import db
import fmp
import storage
import xlsxmeta

app = Flask(__name__)

# Decks and Excel models run well past the old 20 MB ceiling. The request cap
# sits above the per-file limit so an oversized upload is rejected with a
# readable message rather than a bare 413 from Werkzeug.
MAX_UPLOAD_MB = 50
app.config['MAX_CONTENT_LENGTH'] = (MAX_UPLOAD_MB + 10) * 1024 * 1024
logging.basicConfig(level=logging.INFO)
db.init()
db.migrate()


@app.after_request
def no_stale_pages(response):
    """Keep the browser from running last deploy's JavaScript.

    The page scripts are inline in the templates, so a cached HTML page means
    cached code. When that code and the server disagree about a payload shape,
    things fail in ways that look like data problems rather than staleness.
    """
    ctype = (response.headers.get('Content-Type') or '')
    if ctype.startswith('text/html'):
        response.headers['Cache-Control'] = 'no-store, must-revalidate'
    elif request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-cache'   # revalidate, allow 304
    return response


@app.errorhandler(Exception)
def handle_unexpected(exc):
    """Return the actual failure as JSON on API routes.

    Without this an unhandled exception becomes an HTML 500 page, which the
    client can't parse and reports as a bare status code — the error that
    matters is only in the server log. This is a single-user app, so echoing
    the exception to that user costs nothing and saves a redeploy per bug.
    """
    if isinstance(exc, HTTPException):
        return exc
    app.logger.error('Unhandled error on %s\n%s', request.path, traceback.format_exc())
    if request.path.startswith('/api/'):
        return jsonify({'error': f'{type(exc).__name__}: {exc}'}), 500
    return f'{type(exc).__name__}: {exc}', 500


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


def _remap_note_sources():
    """Move notes off the shared idea-sources list onto their own.

    Note sources were briefly the same table as idea sources. Any note written
    in that window points at a `sources` row; copy the name across so the label
    survives, then repoint it.
    """
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            'SELECT DISTINCT s.name FROM project_notes n JOIN sources s ON n.source_id = s.id '
            'WHERE n.source_id IS NOT NULL AND n.note_source_id IS NULL'
        )
        names = [r['name'] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
        for name in names:
            if db.IS_PG:
                cur.execute(
                    f'INSERT INTO note_sources (name) VALUES ({db.PH}) '
                    f'ON CONFLICT (name) DO NOTHING', (name,),
                )
            else:
                cur.execute(f'INSERT OR IGNORE INTO note_sources (name) VALUES ({db.PH})', (name,))
            cur.execute(f'SELECT id FROM note_sources WHERE name = {db.PH}', (name,))
            row = db.to_dict(cur.fetchone())
            if row:
                cur.execute(
                    f'UPDATE project_notes SET note_source_id = {db.PH} '
                    f'WHERE note_source_id IS NULL AND source_id IN '
                    f'(SELECT id FROM sources WHERE name = {db.PH})',
                    (row['id'], name),
                )


_seed_idea_types()
_remap_note_sources()


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
                attachment_key = storage.upload(
                    raw, file_obj.filename,
                    parts=_idea_folder(data['idea_date'], data['ticker']),
                )
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
                    new_key = storage.upload(
                        raw, file_obj.filename,
                        parts=_idea_folder(data['idea_date'], data['ticker']),
                    )
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

# Long-form write-up panels, edited in place on the project page.
NOTE_FIELDS = (
    'business_description', 'thesis', 'pros', 'cons',
    'bull_case', 'bear_case', 'key_questions',
    'business_description_detail', 'bull_case_detail', 'bear_case_detail',
)

_PROJECT_SELECT = (
    'SELECT p.id, p.name, p.ticker, p.direction, p.stage, p.thesis, p.rating, '
    'p.current_price, p.origin_idea_id, p.created_at, p.updated_at, '
    'p.business_description, p.pros, p.cons, p.bull_case, p.bear_case, '
    'p.business_description_detail, p.bull_case_detail, p.bear_case_detail, '
    'p.business_description_generated_at, p.bull_case_generated_at, '
    'p.bear_case_generated_at, p.key_questions, '
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
                attachment_key = storage.upload(
                    raw, file_obj.filename, parts=_project_folder(data),
                )
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
        data.get('business_description') or None,
        data.get('pros') or None,
        data.get('cons') or None,
        data.get('bull_case') or None,
        data.get('bear_case') or None,
        data.get('key_questions') or None,
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
                    new_key = storage.upload(
                        raw, file_obj.filename, parts=_project_folder(data),
                    )
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


@app.route('/api/projects/<int:project_id>/notes', methods=['PATCH'])
def update_project_notes(project_id):
    """Edit one write-up panel in place.

    Kept out of update_project on purpose: that route rebuilds the whole row
    from the modal, which doesn't carry these fields, so routing them through
    it would blank them on every ordinary save.
    """
    data    = request.get_json(force=True)
    updates = {f: (data[f] or None) for f in NOTE_FIELDS if f in data}
    if not updates:
        return jsonify({'error': 'no recognised note fields'}), 400

    assigns = ', '.join(f'{f} = {db.PH}' for f in updates)
    params  = list(updates.values()) + [datetime.now().isoformat(), project_id]
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE projects SET {assigns}, updated_at = {db.PH} WHERE id = {db.PH}',
            params,
        )
        cur.execute(_PROJECT_SELECT + f'WHERE p.id = {db.PH}', (project_id,))
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(row)


# ── Key questions ────────────────────────────────────────────────────────────
# Each question is its own row so it can carry a body of findings that expands
# under it. Older projects stored these as newline-separated text; that gets
# lifted into rows once, on first read, and the text field cleared.

_Q_BULLET = re.compile(r'^\s*(?:[-•*]|\d+[.)])\s+')


def _questions_for(cur, project_id):
    cur.execute(
        f'SELECT id, question, detail, position FROM project_questions '
        f'WHERE project_id = {db.PH} ORDER BY position, id',
        (project_id,),
    )
    return db.to_dicts(cur.fetchall())


def _lift_legacy_questions(cur, project_id):
    """Move a project's newline-separated key_questions text into rows."""
    cur.execute(f'SELECT key_questions FROM projects WHERE id = {db.PH}', (project_id,))
    row  = db.to_dict(cur.fetchone()) or {}
    text = (row.get('key_questions') or '').strip()
    if not text:
        return
    now = datetime.now().isoformat()
    for i, line in enumerate(text.split('\n')):
        q = _Q_BULLET.sub('', line).strip()
        if q:
            cur.execute(
                f'INSERT INTO project_questions '
                f'(project_id, question, detail, position, created_at, updated_at) '
                f'VALUES ({db.PH}, {db.PH}, {db.PH}, {db.PH}, {db.PH}, {db.PH})',
                (project_id, q, None, i, now, now),
            )
    # Clear the source so this can't run twice.
    cur.execute(
        f'UPDATE projects SET key_questions = NULL WHERE id = {db.PH}', (project_id,)
    )


@app.route('/api/projects/<int:project_id>/questions', methods=['GET'])
def get_questions(project_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        rows = _questions_for(cur, project_id)
        if not rows:
            _lift_legacy_questions(cur, project_id)
            rows = _questions_for(cur, project_id)
        return jsonify(rows)


@app.route('/api/projects/<int:project_id>/questions', methods=['POST'])
def create_question(project_id):
    data = request.get_json(force=True)
    question = (data.get('question') or '').strip()
    if not question:
        return jsonify({'error': 'question is required'}), 400
    now = datetime.now().isoformat()
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT COALESCE(MAX(position), -1) AS p FROM project_questions '
            f'WHERE project_id = {db.PH}',
            (project_id,),
        )
        pos = (db.to_dict(cur.fetchone()) or {}).get('p', -1) + 1
        cols = '(project_id, question, detail, position, created_at, updated_at)'
        vals = (project_id, question, data.get('detail') or None, pos, now, now)
        if db.IS_PG:
            cur.execute(
                f'INSERT INTO project_questions {cols} VALUES '
                f'({db.PH}, {db.PH}, {db.PH}, {db.PH}, {db.PH}, {db.PH}) '
                f'RETURNING id, question, detail, position',
                vals,
            )
            row = db.to_dict(cur.fetchone())
        else:
            cur.execute(
                f'INSERT INTO project_questions {cols} VALUES '
                f'({db.PH}, {db.PH}, {db.PH}, {db.PH}, {db.PH}, {db.PH})',
                vals,
            )
            cur.execute(
                'SELECT id, question, detail, position FROM project_questions WHERE id = ?',
                (cur.lastrowid,),
            )
            row = db.to_dict(cur.fetchone())
    return jsonify(row), 201


@app.route('/api/questions/<int:question_id>', methods=['PATCH'])
def update_question(question_id):
    data    = request.get_json(force=True)
    updates = {}
    if 'question' in data:
        q = (data.get('question') or '').strip()
        if not q:
            return jsonify({'error': 'question cannot be empty'}), 400
        updates['question'] = q
    if 'detail' in data:
        updates['detail'] = data.get('detail') or None
    if not updates:
        return jsonify({'error': 'nothing to update'}), 400

    assigns = ', '.join(f'{k} = {db.PH}' for k in updates)
    params  = list(updates.values()) + [datetime.now().isoformat(), question_id]
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE project_questions SET {assigns}, updated_at = {db.PH} WHERE id = {db.PH}',
            params,
        )
        cur.execute(
            f'SELECT id, question, detail, position FROM project_questions WHERE id = {db.PH}',
            (question_id,),
        )
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(row)


@app.route('/api/questions/<int:question_id>', methods=['DELETE'])
def delete_question(question_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'DELETE FROM project_questions WHERE id = {db.PH}', (question_id,))
    return jsonify({'ok': True})


@app.route('/api/projects/<int:project_id>', methods=['DELETE'])
def delete_project(project_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT attachment_key FROM projects WHERE id = {db.PH}', (project_id,))
        row = db.to_dict(cur.fetchone()) or {}
        if row.get('attachment_key'):
            storage.delete(row['attachment_key'])
        # SQLite doesn't enforce foreign keys by default, so the ON DELETE
        # CASCADE can't be relied on — clear the children explicitly.
        # Collect every bucket object this project owns before dropping the rows,
        # or the files are orphaned in storage with nothing pointing at them.
        keys = []
        for table in ('note_attachments', 'project_documents', 'model_versions'):
            cur.execute(
                f'SELECT object_key FROM {table} WHERE project_id = {db.PH}', (project_id,),
            )
            keys += [r['object_key'] for r in db.to_dicts(cur.fetchall())]
            cur.execute(f'DELETE FROM {table} WHERE project_id = {db.PH}', (project_id,))

        cur.execute(f'DELETE FROM project_notes WHERE project_id = {db.PH}', (project_id,))
        cur.execute(f'DELETE FROM project_questions WHERE project_id = {db.PH}', (project_id,))
        cur.execute(f'DELETE FROM generation_jobs WHERE project_id = {db.PH}', (project_id,))
        cur.execute(f'DELETE FROM projects WHERE id = {db.PH}', (project_id,))
    _delete_keys(keys)
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
        None,   # business_description
        None,   # pros
        None,   # cons
        None,   # bull_case
        None,   # bear_case
        None,   # key_questions
        now,
        now,
        None, None, None, None,   # attachment url / name / data / key
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


def _consensus_from_fmp(ticker, price, mcap, ev, trailing):
    """Forward consensus with the multiples and margins Yahoo can't reach.

    Returns None when FMP has no key or nothing useful, so the caller can fall
    back to Yahoo's thinner EPS/revenue-only view.
    """
    if not fmp.enabled():
        return None
    try:
        rows = fmp.analyst_estimates(ticker, limit=5)
    except Exception as exc:
        app.logger.info('FMP estimates unavailable for %s: %s', ticker, exc)
        return None
    if not rows:
        return None

    def growth(now, before):
        if now is None or not before:
            return None
        return (now / before - 1) * 100

    def ratio(num, den):
        return (num / den) if num is not None and den else None

    periods, previous = [], trailing
    for row in rows:
        rev, ebitda, ebit, eps = row['revenue'], row['ebitda'], row['ebit'], row['eps']
        periods.append({
            'label':     row['label'],
            'date':      row['date'],
            'analysts':  row['analysts'],
            'revenue':   rev,
            'ebitda':    ebitda,
            'ebit':      ebit,
            'eps':       eps,
            # Multiples hold today's price and EV against each forward year.
            'pe':          ratio(price, eps),
            'ev_ebitda':   ratio(ev, ebitda),
            'ev_ebit':     ratio(ev, ebit),
            'price_sales': ratio(mcap, rev),
            'ebitda_margin': (lambda v: v * 100 if v is not None else None)(ratio(ebitda, rev)),
            'ebit_margin':   (lambda v: v * 100 if v is not None else None)(ratio(ebit, rev)),
            # First year grows off trailing actuals; later years off the year before.
            'revenue_growth': growth(rev,    previous.get('revenue')),
            'ebitda_growth':  growth(ebitda, previous.get('ebitda')),
            'ebit_growth':    growth(ebit,   previous.get('ebit')),
            'eps_growth':     growth(eps,    previous.get('eps')),
        })
        previous = {'revenue': rev, 'ebitda': ebitda, 'ebit': ebit, 'eps': eps}

    return {'source': 'FMP', 'periods': periods}


def _consensus(t, price, mcap):
    """Forward EPS/revenue estimates — the only forward data Yahoo provides."""
    rows = []
    try:
        eps_est = t.earnings_estimate
        rev_est = t.revenue_estimate
    except Exception:
        # yfinance throws here often enough to matter. Return the same shape as
        # the success path — a bare list would leave the client with nothing to
        # read `periods` off, and the block would silently render empty.
        return {'source': 'Yahoo', 'periods': []}

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
            'label':          label,
            'eps':            eps,
            'eps_growth':     eps_growth * 100 if eps_growth is not None else None,
            'revenue':        rev,
            'revenue_growth': rev_growth * 100 if rev_growth is not None else None,
            'pe':             (price / eps) if price and eps else None,
            'price_sales':    (mcap / rev)  if mcap  and rev else None,
        })
    return {'source': 'Yahoo', 'periods': rows} if rows else {'source': 'Yahoo', 'periods': []}


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
        'consensus': (
            _consensus_from_fmp(ticker, price, mcap, ev, {
                'revenue': revenue, 'ebitda': ebitda,
                'ebit': _num(info.get('ebit')), 'eps': _num(info.get('trailingEps')),
            })
            or _consensus(t, price, mcap)
        ),
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


# ── Research material: notes, documents, model versions ──────────────────────
# All of this goes to the object store. Unlike the single idea attachment, these
# are decks and Excel models — keeping them as Postgres blobs would bloat the
# database, so the bucket is required here rather than optional.

def _bucket_missing():
    if storage.ENABLED:
        return None
    return jsonify({
        'error': 'File storage is not configured, so uploads are disabled. Set '
                 'AWS_ENDPOINT_URL, AWS_S3_BUCKET_NAME, AWS_ACCESS_KEY_ID and '
                 'AWS_SECRET_ACCESS_KEY on the service.'
    }), 503


def _project_exists(project_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT id, name, ticker FROM projects WHERE id = {db.PH}', (project_id,))
        return db.to_dict(cur.fetchone())


def _check_size(raw, filename):
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        return (jsonify({
            'error': f'"{filename}" exceeds the {MAX_UPLOAD_MB} MB limit '
                     f'({len(raw) / 1024 / 1024:.1f} MB).'
        }), 413)
    return None


def _store_bytes(raw, filename, parts=None, rename=None):
    """Put bytes in the bucket. Returns (info, None) or (None, response)."""
    try:
        key = storage.upload(raw, rename or filename, parts=parts)
    except Exception as exc:
        return None, (jsonify({'error': f'Upload failed: {exc}'}), 502)
    return {'filename': filename, 'object_key': key, 'size_bytes': len(raw)}, None


def _store_upload(file_obj, parts=None, rename=None):
    """Upload one file to the bucket. Returns (info, None) or (None, response).

    `parts`  is the folder chain the object lands under — see storage.build_key.
    `rename` overrides the stored object name; the row keeps the original.
    """
    if not file_obj or not file_obj.filename:
        return None, (jsonify({'error': 'No file supplied'}), 400)
    raw = file_obj.read()
    if oversize := _check_size(raw, file_obj.filename):
        return None, oversize
    return _store_bytes(raw, file_obj.filename, parts=parts, rename=rename)


def _project_folder(project):
    """Folder chain for a project's files: projects/<name>/<section>."""
    return ['projects', project.get('name') or f"project {project.get('id')}"]


def _idea_folder(idea_date, ticker):
    """Ideas live under a dated, tickered folder: ideas/2026-03-14 AAP."""
    stamp = (idea_date or '')[:10]
    label = ' '.join(p for p in (stamp, (ticker or '').upper()) if p) or 'undated'
    return ['ideas', label]


def _redirect_to_file(object_key, filename, force_download=False):
    from flask import redirect
    return redirect(storage.presigned_url(
        object_key, download_as=filename if force_download else None,
    ))


def _delete_keys(keys):
    for key in keys:
        if key:
            storage.delete(key)


# ── Notes ───────────────────────────────────────────────────────────────────

def _notes_for(cur, project_id):
    cur.execute(
        f'SELECT n.id, n.body, n.note_source_id, s.name AS source_name, s.kind AS source_kind, '
        f'n.created_at, n.updated_at FROM project_notes n '
        f'LEFT JOIN note_sources s ON n.note_source_id = s.id '
        f'WHERE n.project_id = {db.PH} ORDER BY n.created_at DESC, n.id DESC',
        (project_id,),
    )
    notes = db.to_dicts(cur.fetchall())
    if not notes:
        return []
    cur.execute(
        f'SELECT id, note_id, filename, size_bytes, created_at FROM note_attachments '
        f'WHERE project_id = {db.PH} ORDER BY id',
        (project_id,),
    )
    by_note = {}
    for att in db.to_dicts(cur.fetchall()):
        by_note.setdefault(att['note_id'], []).append(att)
    for note in notes:
        note['attachments'] = by_note.get(note['id'], [])
    return notes


@app.route('/api/projects/<int:project_id>/notes', methods=['GET'])
def get_notes(project_id):
    with db.get_conn() as conn:
        return jsonify(_notes_for(db.cursor(conn), project_id))


@app.route('/api/projects/<int:project_id>/notes', methods=['POST'])
def create_note(project_id):
    # Accepts multipart (body + any number of files) or plain JSON (body only).
    if request.content_type and 'multipart' in request.content_type:
        data  = request.form
        files = [f for f in request.files.getlist('files') if f and f.filename]
    else:
        data  = request.get_json(force=True)
        files = []
    body      = (data.get('body') or '').strip()
    source_id = _opt_int(data, 'note_source_id')

    if not body and not files:
        return jsonify({'error': 'A note needs some text or a file'}), 400
    if files and (missing := _bucket_missing()):
        return missing

    project = _project_exists(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    stored = []
    for f in files:
        info, err = _store_upload(f, parts=_project_folder(project) + ['notes'])
        if err:
            _delete_keys(s['object_key'] for s in stored)   # don't orphan objects
            return err
        stored.append(info)

    now = datetime.now().isoformat()
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        note_id = db.insert_id(
            cur, 'project_notes',
            ('project_id', 'body', 'note_source_id', 'created_at', 'updated_at'),
            (project_id, body, source_id, now, now),
        )
        for s in stored:
            db.insert_id(
                cur, 'note_attachments',
                ('note_id', 'project_id', 'filename', 'object_key', 'size_bytes', 'created_at'),
                (note_id, project_id, s['filename'], s['object_key'], s['size_bytes'], now),
            )
        notes = _notes_for(cur, project_id)
    return jsonify(next((n for n in notes if n['id'] == note_id), None)), 201


@app.route('/api/notes/<int:note_id>', methods=['PATCH'])
def update_note(note_id):
    data = request.get_json(force=True)
    sets   = [f'body = {db.PH}', f'updated_at = {db.PH}']
    params = [(data.get('body') or '').strip(), datetime.now().isoformat()]
    # Only touch the source when the caller actually sent one, so editing the
    # text alone can't silently clear it.
    if 'note_source_id' in data:
        sets.append(f'note_source_id = {db.PH}')
        params.append(_opt_int(data, 'note_source_id'))

    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE project_notes SET {", ".join(sets)} WHERE id = {db.PH}',
            params + [note_id],
        )
        cur.execute(
            f'SELECT n.id, n.project_id, n.body, n.note_source_id, s.name AS source_name, '
            f's.kind AS source_kind, n.created_at, n.updated_at FROM project_notes n '
            f'LEFT JOIN note_sources s ON n.note_source_id = s.id WHERE n.id = {db.PH}', (note_id,),
        )
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(row)


@app.route('/api/notes/<int:note_id>', methods=['DELETE'])
def delete_note(note_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT object_key FROM note_attachments WHERE note_id = {db.PH}', (note_id,),
        )
        keys = [r['object_key'] for r in db.to_dicts(cur.fetchall())]
        cur.execute(f'DELETE FROM note_attachments WHERE note_id = {db.PH}', (note_id,))
        cur.execute(f'DELETE FROM project_notes WHERE id = {db.PH}', (note_id,))
    _delete_keys(keys)
    return jsonify({'ok': True})


@app.route('/api/notes/<int:note_id>/attachments', methods=['POST'])
def add_note_attachment(note_id):
    if missing := _bucket_missing():
        return missing
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT project_id FROM project_notes WHERE id = {db.PH}', (note_id,))
        note = db.to_dict(cur.fetchone())
    if not note:
        return jsonify({'error': 'Note not found'}), 404

    project = _project_exists(note['project_id'])
    info, err = _store_upload(
        request.files.get('file'), parts=_project_folder(project) + ['notes'],
    )
    if err:
        return err

    now = datetime.now().isoformat()
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        att_id = db.insert_id(
            cur, 'note_attachments',
            ('note_id', 'project_id', 'filename', 'object_key', 'size_bytes', 'created_at'),
            (note_id, note['project_id'], info['filename'], info['object_key'],
             info['size_bytes'], now),
        )
    return jsonify({'id': att_id, 'note_id': note_id, **info, 'created_at': now}), 201


@app.route('/api/attachments/<int:attachment_id>', methods=['DELETE'])
def delete_note_attachment(attachment_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT object_key FROM note_attachments WHERE id = {db.PH}', (attachment_id,),
        )
        row = db.to_dict(cur.fetchone())
        cur.execute(f'DELETE FROM note_attachments WHERE id = {db.PH}', (attachment_id,))
    if row:
        _delete_keys([row['object_key']])
    return jsonify({'ok': True})


@app.route('/api/attachments/<int:attachment_id>/download')
def download_note_attachment(attachment_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT filename, object_key FROM note_attachments WHERE id = {db.PH}',
            (attachment_id,),
        )
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return _redirect_to_file(row['object_key'], row['filename'])


# ── Note sources ────────────────────────────────────────────────────────────
# Who a note came from — a broker, a fund, an expert call. Separate from the
# `sources` list, which records where an *idea* originated.

@app.route('/api/note-sources', methods=['GET'])
def get_note_sources():
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute('SELECT id, name, kind FROM note_sources ORDER BY kind, name')
        return jsonify({'sources': db.to_dicts(cur.fetchall()), 'kinds': db.NOTE_SOURCE_KINDS})


@app.route('/api/note-sources', methods=['POST'])
def create_note_source():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    kind = (data.get('kind') or '').strip() or None
    if not name:
        return jsonify({'error': 'name is required'}), 400

    with db.get_conn() as conn:
        cur = db.cursor(conn)
        if db.IS_PG:
            cur.execute(
                f'INSERT INTO note_sources (name, kind) VALUES ({db.PH}, {db.PH}) '
                f'ON CONFLICT (name) DO UPDATE SET kind = EXCLUDED.kind RETURNING id, name, kind',
                (name, kind),
            )
            row = db.to_dict(cur.fetchone())
        else:
            cur.execute(
                f'INSERT INTO note_sources (name, kind) VALUES ({db.PH}, {db.PH}) '
                f'ON CONFLICT (name) DO UPDATE SET kind = excluded.kind', (name, kind),
            )
            cur.execute(f'SELECT id, name, kind FROM note_sources WHERE name = {db.PH}', (name,))
            row = db.to_dict(cur.fetchone())
    return jsonify(row), 201


@app.route('/api/note-sources/<int:source_id>', methods=['PATCH'])
def update_note_source(source_id):
    data = request.get_json(force=True)
    sets, params = [], []
    if 'name' in data:
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'name cannot be empty'}), 400
        sets.append(f'name = {db.PH}'); params.append(name)
    if 'kind' in data:
        sets.append(f'kind = {db.PH}'); params.append((data.get('kind') or '').strip() or None)
    if not sets:
        return jsonify({'error': 'nothing to update'}), 400

    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE note_sources SET {", ".join(sets)} WHERE id = {db.PH}', params + [source_id],
        )
        cur.execute(f'SELECT id, name, kind FROM note_sources WHERE id = {db.PH}', (source_id,))
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(row)


@app.route('/api/note-sources/<int:source_id>', methods=['DELETE'])
def delete_note_source(source_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'DELETE FROM note_sources WHERE id = {db.PH}', (source_id,))
    return jsonify({'ok': True})


# ── Document types (user-managed, like sources) ─────────────────────────────

@app.route('/api/doc-types', methods=['GET'])
def get_doc_types():
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute('SELECT id, name FROM doc_types ORDER BY name')
        return jsonify(db.to_dicts(cur.fetchall()))


@app.route('/api/doc-types', methods=['POST'])
def create_doc_type():
    name = (request.get_json(force=True).get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        if db.IS_PG:
            cur.execute(
                f'INSERT INTO doc_types (name) VALUES ({db.PH}) '
                f'ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id, name',
                (name,),
            )
            row = db.to_dict(cur.fetchone())
        else:
            cur.execute(f'INSERT OR IGNORE INTO doc_types (name) VALUES ({db.PH})', (name,))
            cur.execute(f'SELECT id, name FROM doc_types WHERE name = {db.PH}', (name,))
            row = db.to_dict(cur.fetchone())
    return jsonify(row), 201


@app.route('/api/doc-types/<int:type_id>', methods=['DELETE'])
def delete_doc_type(type_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'DELETE FROM doc_types WHERE id = {db.PH}', (type_id,))
    return jsonify({'ok': True})


# ── Documents ───────────────────────────────────────────────────────────────

_DOC_SELECT = (
    'SELECT d.id, d.title, d.doc_type_id, dt.name AS doc_type_name, d.doc_date, '
    'd.filename, d.size_bytes, d.created_at '
    'FROM project_documents d LEFT JOIN doc_types dt ON d.doc_type_id = dt.id '
)


@app.route('/api/projects/<int:project_id>/documents', methods=['GET'])
def get_documents(project_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            _DOC_SELECT + f'WHERE d.project_id = {db.PH} '
            f'ORDER BY COALESCE(d.doc_date, d.created_at) DESC, d.id DESC',
            (project_id,),
        )
        return jsonify(db.to_dicts(cur.fetchall()))


@app.route('/api/projects/<int:project_id>/documents', methods=['POST'])
def create_document(project_id):
    if missing := _bucket_missing():
        return missing
    project = _project_exists(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    info, err = _store_upload(
        request.files.get('file'), parts=_project_folder(project) + ['documents'],
    )
    if err:
        return err

    data  = request.form
    title = (data.get('title') or '').strip() or info['filename']
    now   = datetime.now().isoformat()
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        doc_id = db.insert_id(
            cur, 'project_documents',
            ('project_id', 'title', 'doc_type_id', 'doc_date', 'filename',
             'object_key', 'size_bytes', 'created_at'),
            (project_id, title, _opt_int(data, 'doc_type_id'), data.get('doc_date') or None,
             info['filename'], info['object_key'], info['size_bytes'], now),
        )
        cur.execute(_DOC_SELECT + f'WHERE d.id = {db.PH}', (doc_id,))
        row = db.to_dict(cur.fetchone())
    return jsonify(row), 201


@app.route('/api/documents/<int:doc_id>', methods=['PATCH'])
def update_document(doc_id):
    data = request.get_json(force=True)
    sets, params = [], []
    if 'title' in data:
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'error': 'title cannot be empty'}), 400
        sets.append(f'title = {db.PH}'); params.append(title)
    if 'doc_type_id' in data:
        sets.append(f'doc_type_id = {db.PH}'); params.append(_opt_int(data, 'doc_type_id'))
    if 'doc_date' in data:
        sets.append(f'doc_date = {db.PH}'); params.append(data.get('doc_date') or None)
    if not sets:
        return jsonify({'error': 'nothing to update'}), 400

    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE project_documents SET {", ".join(sets)} WHERE id = {db.PH}',
            params + [doc_id],
        )
        cur.execute(_DOC_SELECT + f'WHERE d.id = {db.PH}', (doc_id,))
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(row)


@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])
def delete_document(doc_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT object_key FROM project_documents WHERE id = {db.PH}', (doc_id,))
        row = db.to_dict(cur.fetchone())
        cur.execute(f'DELETE FROM project_documents WHERE id = {db.PH}', (doc_id,))
    if row:
        _delete_keys([row['object_key']])
    return jsonify({'ok': True})


@app.route('/api/documents/<int:doc_id>/download')
def download_document(doc_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT filename, object_key FROM project_documents WHERE id = {db.PH}', (doc_id,),
        )
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return _redirect_to_file(row['object_key'], row['filename'])


# ── Model versions ──────────────────────────────────────────────────────────
# Append-only: uploading makes a new version, highest number is current, and
# every prior version stays downloadable.

@app.route('/api/projects/<int:project_id>/model', methods=['GET'])
def get_model_versions(project_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT id, version, label, filename, size_bytes, created_at '
            f'FROM model_versions WHERE project_id = {db.PH} ORDER BY version DESC',
            (project_id,),
        )
        return jsonify(db.to_dicts(cur.fetchall()))


@app.route('/api/projects/<int:project_id>/model', methods=['POST'])
def create_model_version(project_id):
    if missing := _bucket_missing():
        return missing
    project = _project_exists(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'error': 'No file supplied'}), 400
    raw = upload.read()
    if oversize := _check_size(raw, upload.filename):
        return oversize

    # If the workbook is stamped for a different project, say so rather than
    # quietly filing someone's model in the wrong place.
    tagged = xlsxmeta.read_project_id(raw)
    retagged_from = None
    if tagged and tagged != project_id:
        other = _project_exists(tagged)
        if other and request.form.get('force') != 'true':
            return jsonify({
                'error': f'This workbook is tagged as the model for "{other["name"]}".',
                'tagged_project': {'id': other['id'], 'name': other['name']},
                'needs_confirm': True,
            }), 409
        if other:
            retagged_from = {'id': other['id'], 'name': other['name']}

    return _add_model_version(project, raw, upload.filename,
                              (request.form.get('label') or '').strip() or None,
                              retagged_from=retagged_from)


def _add_model_version(project, raw, filename, label, retagged_from=None):
    """Claim the next version, stamp the workbook, store it, record the row.

    `retagged_from` names the project the file used to claim, when the user has
    deliberately overridden its tag — reported back so the UI can say so.
    """
    project_id = project['id']

    # Claim the version number before uploading, so it can go in the object name
    # and in the file's own metadata. Numbers are issued from a high-water mark,
    # never recomputed from live rows — deleting v3 must not hand "v3" to a later
    # file. A failed upload burns a number: the right trade for never reusing one.
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT COALESCE(MAX(version), 0) AS v FROM model_versions '
            f'WHERE project_id = {db.PH}', (project_id,),
        )
        highest_live = (db.to_dict(cur.fetchone()) or {}).get('v', 0) or 0
        cur.execute(
            f'SELECT COALESCE(model_version_seq, 0) AS s FROM projects WHERE id = {db.PH}',
            (project_id,),
        )
        issued = (db.to_dict(cur.fetchone()) or {}).get('s', 0) or 0

        version = max(highest_live, issued) + 1
        cur.execute(
            f'UPDATE projects SET model_version_seq = {db.PH} WHERE id = {db.PH}',
            (version, project_id),
        )

    # Stamp the workbook so a later re-upload pairs itself. Leaves non-Excel
    # files untouched.
    stamped = xlsxmeta.stamp_for_project(raw, project_id, project['name'], version)

    # v3 2026-07-28 1432 AAP_model.xlsx — the version leads, then the upload
    # time. The original filename can change between revisions and there may be
    # several uploads in a day, so neither alone identifies a version.
    uploaded = datetime.now()
    info, err = _store_bytes(
        stamped, filename, parts=_project_folder(project) + ['model'],
        rename=f'v{version} {uploaded.strftime("%Y-%m-%d %H%M")} {filename}',
    )
    if err:
        return err

    now = uploaded.isoformat()
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        vid = db.insert_id(
            cur, 'model_versions',
            ('project_id', 'version', 'label', 'filename', 'object_key',
             'size_bytes', 'created_at'),
            (project_id, version, label, info['filename'], info['object_key'],
             info['size_bytes'], now),
        )
    return jsonify({
        'id': vid, 'version': version, 'label': label,
        'filename': info['filename'], 'size_bytes': info['size_bytes'],
        'created_at': now, 'project': {'id': project_id, 'name': project['name']},
        'tagged': xlsxmeta.is_supported(filename),
        'retagged_from': retagged_from,
    }), 201


@app.route('/api/model/upload', methods=['POST'])
def upload_model_by_tag():
    """Drop a workbook anywhere; the file's own metadata says where it belongs.

    Falls back to asking when the file carries no tag — which is every model
    that predates this, and any built outside the app.
    """
    if missing := _bucket_missing():
        return missing

    upload = request.files.get('file')
    if not upload or not upload.filename:
        return jsonify({'error': 'No file supplied'}), 400
    raw = upload.read()
    if oversize := _check_size(raw, upload.filename):
        return oversize

    # An explicit project_id is a deliberate override — reusing an old model as
    # the template for a different name, say. The file gets re-tagged to match.
    chosen    = _opt_int(request.form, 'project_id')
    tagged_id = xlsxmeta.read_project_id(raw)
    retagged_from = None
    if chosen and tagged_id and chosen != tagged_id:
        previous = _project_exists(tagged_id)
        if previous:
            retagged_from = {'id': previous['id'], 'name': previous['name']}

    project_id = chosen or tagged_id
    project = _project_exists(project_id) if project_id else None

    if not project:
        with db.get_conn() as conn:
            cur = db.cursor(conn)
            cur.execute('SELECT id, name FROM projects ORDER BY name')
            choices = db.to_dicts(cur.fetchall())
        return jsonify({
            'error': 'This file isn\'t tagged for a project yet — pick one and it will be '
                     'tagged from now on.',
            'needs_project': True,
            'projects': choices,
        }), 409

    return _add_model_version(project, raw, upload.filename,
                              (request.form.get('label') or '').strip() or None,
                              retagged_from=retagged_from)


@app.route('/api/model-versions/<int:version_id>', methods=['DELETE'])
def delete_model_version(version_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT object_key FROM model_versions WHERE id = {db.PH}', (version_id,))
        row = db.to_dict(cur.fetchone())
        cur.execute(f'DELETE FROM model_versions WHERE id = {db.PH}', (version_id,))
    if row:
        _delete_keys([row['object_key']])
    return jsonify({'ok': True})


@app.route('/api/model-versions/<int:version_id>/download')
def download_model_version(version_id):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT filename, object_key FROM model_versions WHERE id = {db.PH}', (version_id,),
        )
        row = db.to_dict(cur.fetchone())
    if not row:
        return jsonify({'error': 'Not found'}), 404
    # Spreadsheets should save, not try to render in a tab.
    return _redirect_to_file(row['object_key'], row['filename'], force_download=True)


# ── Sub-pages ───────────────────────────────────────────────────────────────

@app.route('/projects/<int:project_id>/<any(notes, documents, model):section>')
def project_section(project_id, section):
    project = _project_exists(project_id)
    if not project:
        return 'Project not found', 404
    return render_template(
        f'project_{section}.html', project=project,
        section=section, max_mb=MAX_UPLOAD_MB, storage_enabled=storage.ENABLED,
    )


# ── AI drafting ──────────────────────────────────────────────────────────────

def _ai_context(project):
    """Compact reference block handed to Claude alongside the prompt."""
    lines = [
        f"Project name: {project.get('name')}",
        f"Ticker: {project.get('ticker') or 'none'}",
        f"Direction: {project.get('direction') or 'undecided'}",
        f"Stage: {project.get('stage')}",
    ]
    if project.get('idea_type_name'):
        lines.append(f"Idea type: {project['idea_type_name']}")
    if project.get('subtype_name'):
        lines.append(f"Sub-type: {project['subtype_name']}")
    if project.get('thesis'):
        lines.append(f"\nThe user's own thesis so far:\n{project['thesis']}")

    ticker = project.get('ticker')
    if not ticker:
        lines.append('\nNo ticker attached, so no market data is available.')
        return '\n'.join(lines)

    try:
        t = _cached(f'tear:{ticker}', 900, lambda: _build_tearsheet(ticker))
    except Exception as exc:
        lines.append(f'\nMarket data unavailable ({exc}).')
        return '\n'.join(lines)

    def fmt(v):
        # Round hard — full float precision is noise the model has to read past.
        if not isinstance(v, float):
            return v
        if abs(v) >= 1e6:
            return f'{v:,.0f}'
        return f'{v:.2f}'.rstrip('0').rstrip('.')

    def block(title, mapping):
        rows = [f'  {k}: {fmt(v)}' for k, v in mapping.items() if v is not None]
        return f'\n{title}\n' + '\n'.join(rows) if rows else ''

    lines.append(f"\nCompany (per Yahoo Finance): {t.get('name') or ticker}")
    lines.append(block('Capitalization (USD):', t['capitalization']))
    lines.append(block('Technicals:', t['technicals']))
    lines.append(block('Credit:', t['credit']))
    lines.append(block('Valuation (trailing):', t['valuation']))
    lines.append(block('Margins (trailing, %):', t['margins']))
    lines.append(block('Growth (trailing, %):', t['growth']))
    consensus = t.get('consensus') or {}
    for row in consensus.get('periods') or []:
        lines.append(block(
            f"Consensus — {row.get('label')} (per {consensus.get('source')}):",
            {k: v for k, v in row.items() if k not in ('label', 'date')},
        ))

    if consensus.get('source') == 'FMP':
        lines.append(
            '\nNote: forward multiples above hold the current price and EV against each '
            'estimate year. Consensus free cash flow is not published, so FCF yield is '
            'trailing.'
        )
    else:
        lines.append(
            '\nNote: forward estimates cover EPS and revenue only, two periods out. '
            'Forward EBITDA, EBIT and FCF are unavailable, so every margin and leverage '
            'figure above is trailing.'
        )
    return '\n'.join(l for l in lines if l)


# Drafting runs in a background thread and reports through generation_jobs, so
# no HTTP request stays open while Claude works. Nothing in the request path can
# then be killed by a worker or proxy timeout, however long a draft takes.

# A thread dies with its worker on redeploy, leaving a job stuck at 'running'.
# Anything older than this is treated as interrupted rather than in progress.
GENERATION_STALE_SECONDS = 15 * 60


def _job_start(project_id, field):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'DELETE FROM generation_jobs WHERE project_id = {db.PH} AND field = {db.PH}',
            (project_id, field),
        )
        cur.execute(
            f'INSERT INTO generation_jobs (project_id, field, status, started_at) '
            f'VALUES ({db.PH}, {db.PH}, {db.PH}, {db.PH})',
            (project_id, field, 'running', datetime.now().isoformat()),
        )


def _job_finish(project_id, field, status, error=None):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE generation_jobs SET status = {db.PH}, error = {db.PH}, '
            f'finished_at = {db.PH} WHERE project_id = {db.PH} AND field = {db.PH}',
            (status, error, datetime.now().isoformat(), project_id, field),
        )


def _job_read(project_id, field):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'SELECT status, error, started_at, finished_at FROM generation_jobs '
            f'WHERE project_id = {db.PH} AND field = {db.PH}',
            (project_id, field),
        )
        job = db.to_dict(cur.fetchone())
    if not job:
        return None

    if job['status'] == 'running':
        try:
            age = (datetime.now() - datetime.fromisoformat(job['started_at'])).total_seconds()
        except (TypeError, ValueError):
            age = 0
        if age > GENERATION_STALE_SECONDS:
            msg = ('The draft was interrupted — most likely the server restarted while it '
                   'was running. Nothing was saved; try again.')
            _job_finish(project_id, field, 'error', msg)
            return {'status': 'error', 'error': msg}
    return job


def _run_generation(project_id, field):
    """Background worker. Owns its own DB connections; no request context."""
    try:
        with db.get_conn() as conn:
            cur = db.cursor(conn)
            cur.execute(_PROJECT_SELECT + f'WHERE p.id = {db.PH}', (project_id,))
            project = db.to_dict(cur.fetchone())
        if not project:
            _job_finish(project_id, field, 'error', 'Project no longer exists.')
            return

        result = ai.generate(field, project, _ai_context(project))

        now = datetime.now().isoformat()
        with db.get_conn() as conn:
            cur = db.cursor(conn)
            cur.execute(
                f'UPDATE projects SET {field} = {db.PH}, {field}_detail = {db.PH}, '
                f'{field}_generated_at = {db.PH}, updated_at = {db.PH} WHERE id = {db.PH}',
                (result['summary'], result['detail'], now, now, project_id),
            )
        _job_finish(project_id, field, 'done')
    except (ai.NotConfigured, ai.Refused) as exc:
        _job_finish(project_id, field, 'error', str(exc))
    except Exception as exc:
        app.logger.error(
            'Generation failed for project %s / %s\n%s',
            project_id, field, traceback.format_exc(),
        )
        _job_finish(project_id, field, 'error', f'{type(exc).__name__}: {exc}')


@app.route('/api/projects/<int:project_id>/generate/<field>', methods=['POST'])
def generate_writeup(project_id, field):
    if field not in ai.FIELDS:
        return jsonify({'error': f'unknown field: {field}'}), 400
    if not ai.api_key():
        # Fail here rather than in the thread, so the user gets it immediately.
        return jsonify({
            'error': 'No Anthropic API key. Set the ANTHROPIC_API_KEY environment '
                     'variable, or add a key on the Admin page.'
        }), 503

    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(f'SELECT id FROM projects WHERE id = {db.PH}', (project_id,))
        if not cur.fetchone():
            return jsonify({'error': 'Project not found'}), 404

    running = _job_read(project_id, field)
    if running and running['status'] == 'running':
        return jsonify({'status': 'running'}), 202

    _job_start(project_id, field)
    threading.Thread(
        target=_run_generation, args=(project_id, field), daemon=True,
    ).start()
    return jsonify({'status': 'running'}), 202


@app.route('/api/projects/<int:project_id>/generation/<field>')
def generation_status(project_id, field):
    if field not in ai.FIELDS:
        return jsonify({'error': f'unknown field: {field}'}), 400

    job = _job_read(project_id, field)
    if not job:
        return jsonify({'status': 'idle'})

    payload = {'status': job['status'], 'error': job.get('error')}
    if job['status'] == 'done':
        with db.get_conn() as conn:
            cur = db.cursor(conn)
            cur.execute(_PROJECT_SELECT + f'WHERE p.id = {db.PH}', (project_id,))
            row = db.to_dict(cur.fetchone()) or {}
        payload['summary']      = row.get(field)
        payload['detail']       = row.get(f'{field}_detail')
        payload['generated_at'] = row.get(f'{field}_generated_at')
    return jsonify(payload)


@app.route('/projects/<int:project_id>/detail/<field>')
def writeup_detail(project_id, field):
    if field not in ai.FIELDS:
        return 'Unknown section', 404
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(_PROJECT_SELECT + f'WHERE p.id = {db.PH}', (project_id,))
        project = db.to_dict(cur.fetchone())
    if not project:
        return 'Project not found', 404
    return render_template(
        'detail.html', project=project, field=field, label=ai.FIELDS[field]
    )


# ── Model template ──────────────────────────────────────────────────────────
# One blank model kept in Admin. Downloading it from a project hands back a copy
# already tagged for that project, so filling it in and dropping it back files
# it as v1 without being told where it belongs.

def _template_info():
    key = db.get_setting('model_template_key')
    if not key:
        return None
    return {'object_key': key,
            'filename': db.get_setting('model_template_name') or 'model-template.xlsx',
            'uploaded_at': db.get_setting('model_template_uploaded_at')}


@app.route('/api/model-template', methods=['GET'])
def get_model_template():
    info = _template_info()
    return jsonify({
        'template': {'filename': info['filename'], 'uploaded_at': info['uploaded_at']}
        if info else None,
    })


@app.route('/api/model-template', methods=['POST'])
def set_model_template():
    if missing := _bucket_missing():
        return missing

    existing = _template_info()
    info, err = _store_upload(request.files.get('file'), parts=['templates'])
    if err:
        return err

    db.set_setting('model_template_key', info['object_key'])
    db.set_setting('model_template_name', info['filename'])
    db.set_setting('model_template_uploaded_at', datetime.now().isoformat())
    if existing and existing['object_key'] != info['object_key']:
        storage.delete(existing['object_key'])   # only one template at a time
    return jsonify({'filename': info['filename'], 'size_bytes': info['size_bytes']}), 201


@app.route('/api/model-template', methods=['DELETE'])
def clear_model_template():
    info = _template_info()
    for key in ('model_template_key', 'model_template_name', 'model_template_uploaded_at'):
        db.set_setting(key, '')
    if info:
        storage.delete(info['object_key'])
    return jsonify({'ok': True})


@app.route('/api/model-template/download')
def download_model_template():
    info = _template_info()
    if not info:
        return jsonify({'error': 'No template has been uploaded yet.'}), 404
    return _redirect_to_file(info['object_key'], info['filename'], force_download=True)


@app.route('/api/projects/<int:project_id>/model-template')
def download_template_for_project(project_id):
    """The template, stamped for this project so it self-files on the way back."""
    from flask import send_file
    from io import BytesIO

    info = _template_info()
    if not info:
        return jsonify({'error': 'No template has been uploaded yet.'}), 404
    project = _project_exists(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    try:
        raw = storage.read(info['object_key'])
    except Exception as exc:
        return jsonify({'error': f'Could not read the template: {exc}'}), 502

    # Tag it as belonging here, with no version yet — the first upload becomes v1.
    stamped = xlsxmeta.stamp_for_project(raw, project_id, project['name'])
    stem, _, ext = info['filename'].rpartition('.')
    label = storage.safe_segment(project.get('ticker') or project['name'], 'model')
    name = f'{label} {stem or info["filename"]}' + (f'.{ext}' if ext else '')

    return send_file(BytesIO(stamped), as_attachment=True, download_name=name,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── Admin / settings ─────────────────────────────────────────────────────────
# The API key is never returned by the API — only whether one is set and where
# it came from. An env-provided key can't be edited or read back through the UI.

@app.route('/admin')
def admin_page():
    return render_template('admin.html', fields=ai.FIELDS, models=ai.MODELS)


@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify({
        'key_source':      ai.key_source(),
        'fmp_key_source':  fmp.key_source(),
        'model':           ai.model(),
        'models':          [{'id': m, 'label': l} for m, l in ai.MODELS],
        'effort':          ai.effort(),
        'efforts':         [{'id': e, 'label': l} for e, l in ai.EFFORTS],
        'prompts':         {f: ai.prompt_for(f) for f in ai.FIELDS},
        'default_prompts': ai.DEFAULT_PROMPTS,
        'system_prompt':   ai.SYSTEM_PROMPT,
    })


@app.route('/api/settings', methods=['POST'])
def save_settings():
    data = request.get_json(force=True)

    # An empty string means "leave it alone"; the sentinel below clears it.
    if 'anthropic_api_key' in data:
        key = (data.get('anthropic_api_key') or '').strip()
        if key == '__CLEAR__':
            db.set_setting('anthropic_api_key', '')
        elif key:
            db.set_setting('anthropic_api_key', key)

    if 'fmp_api_key' in data:
        key = (data.get('fmp_api_key') or '').strip()
        if key == '__CLEAR__':
            db.set_setting('fmp_api_key', '')
        elif key:
            db.set_setting('fmp_api_key', key)

    if data.get('model') in dict(ai.MODELS):
        db.set_setting('ai_model', data['model'])

    if data.get('effort') in dict(ai.EFFORTS):
        db.set_setting('ai_effort', data['effort'])

    for field, text in (data.get('prompts') or {}).items():
        if field in ai.FIELDS:
            db.set_setting(f'prompt_{field}', (text or '').strip())

    return jsonify({'ok': True, 'key_source': ai.key_source(),
                    'fmp_key_source': fmp.key_source(),
                    'model': ai.model(), 'effort': ai.effort()})


@app.route('/api/fmp/test', methods=['POST'])
def test_fmp_key():
    """Prove the stored key actually works, rather than only that it's present."""
    try:
        return jsonify({'ok': True, 'sample': fmp.test()})
    except fmp.NotConfigured as exc:
        return jsonify({'error': str(exc)}), 503
    except fmp.FMPError as exc:
        return jsonify({'error': str(exc)}), 502


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
