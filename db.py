"""
Database abstraction: PostgreSQL (Railway) when DATABASE_URL is set, SQLite locally.
"""
import os
import sqlite3
from contextlib import contextmanager

DATABASE_URL = os.environ.get('DATABASE_URL')
IS_PG = bool(DATABASE_URL)
PH = '%s' if IS_PG else '?'  # query placeholder

if IS_PG:
    import psycopg2
    import psycopg2.extras

    @contextmanager
    def get_conn():
        c = psycopg2.connect(DATABASE_URL)
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def cursor(conn):
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    _CREATE_SOURCES = """
        CREATE TABLE IF NOT EXISTS sources (
            id   SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        )
    """

    _CREATE_SUBTYPES = """
        CREATE TABLE IF NOT EXISTS subtypes (
            id   SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        )
    """

    _CREATE_IDEA_TYPES = """
        CREATE TABLE IF NOT EXISTS idea_types (
            id   SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        )
    """

    _CREATE_PROJECTS = """
        CREATE TABLE IF NOT EXISTS projects (
            id              SERIAL PRIMARY KEY,
            name            TEXT             NOT NULL,
            ticker          TEXT,
            direction       TEXT,
            stage           TEXT             NOT NULL,
            thesis          TEXT,
            rating          DOUBLE PRECISION,
            idea_type_id    INTEGER          REFERENCES idea_types(id) ON DELETE SET NULL,
            subtype_id      INTEGER          REFERENCES subtypes(id)   ON DELETE SET NULL,
            source_id       INTEGER          REFERENCES sources(id)    ON DELETE SET NULL,
            hat_tip_id      INTEGER          REFERENCES sources(id)    ON DELETE SET NULL,
            origin_idea_id  INTEGER,
            current_price   DOUBLE PRECISION,
            business_description TEXT,
            pros            TEXT,
            cons            TEXT,
            bull_case       TEXT,
            bear_case       TEXT,
            key_questions   TEXT,
            created_at      TEXT             NOT NULL,
            updated_at      TEXT             NOT NULL,
            attachment_url  TEXT,
            attachment_name TEXT,
            attachment_data BYTEA,
            attachment_key  TEXT
        )
    """

    _CREATE_QUESTIONS = """
        CREATE TABLE IF NOT EXISTS project_questions (
            id         SERIAL PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            question   TEXT    NOT NULL,
            detail     TEXT,
            position   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL,
            updated_at TEXT    NOT NULL
        )
    """

    _CREATE = """
        CREATE TABLE IF NOT EXISTS ideas (
            id              SERIAL PRIMARY KEY,
            ticker          TEXT             NOT NULL,
            idea_date       TEXT             NOT NULL,
            idea_price      DOUBLE PRECISION,
            initial_date    TEXT             NOT NULL,
            initial_price   DOUBLE PRECISION,
            current_price   DOUBLE PRECISION,
            thesis          TEXT             NOT NULL,
            direction       TEXT             NOT NULL,
            asset_class     TEXT             NOT NULL,
            source_id       INTEGER          REFERENCES sources(id) ON DELETE SET NULL,
            created_at      TEXT             NOT NULL,
            attachment_url  TEXT,
            attachment_name TEXT,
            attachment_data BYTEA
        )
    """
else:
    _DB_PATH = os.environ.get('DB_PATH', 'ideas.db')

    @contextmanager
    def get_conn():
        c = sqlite3.connect(_DB_PATH)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def cursor(conn):
        return conn.cursor()

    _CREATE_SOURCES = """
        CREATE TABLE IF NOT EXISTS sources (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
    """

    _CREATE_SUBTYPES = """
        CREATE TABLE IF NOT EXISTS subtypes (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
    """

    _CREATE_IDEA_TYPES = """
        CREATE TABLE IF NOT EXISTS idea_types (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
    """

    _CREATE_PROJECTS = """
        CREATE TABLE IF NOT EXISTS projects (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            ticker          TEXT,
            direction       TEXT,
            stage           TEXT    NOT NULL,
            thesis          TEXT,
            rating          REAL,
            idea_type_id    INTEGER REFERENCES idea_types(id) ON DELETE SET NULL,
            subtype_id      INTEGER REFERENCES subtypes(id)   ON DELETE SET NULL,
            source_id       INTEGER REFERENCES sources(id)    ON DELETE SET NULL,
            hat_tip_id      INTEGER REFERENCES sources(id)    ON DELETE SET NULL,
            origin_idea_id  INTEGER,
            current_price   REAL,
            business_description TEXT,
            pros            TEXT,
            cons            TEXT,
            bull_case       TEXT,
            bear_case       TEXT,
            key_questions   TEXT,
            created_at      TEXT    NOT NULL,
            updated_at      TEXT    NOT NULL,
            attachment_url  TEXT,
            attachment_name TEXT,
            attachment_data BLOB,
            attachment_key  TEXT
        )
    """

    _CREATE_QUESTIONS = """
        CREATE TABLE IF NOT EXISTS project_questions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            question   TEXT    NOT NULL,
            detail     TEXT,
            position   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL,
            updated_at TEXT    NOT NULL
        )
    """

    _CREATE = """
        CREATE TABLE IF NOT EXISTS ideas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT    NOT NULL,
            idea_date       TEXT    NOT NULL,
            idea_price      REAL,
            initial_date    TEXT    NOT NULL,
            initial_price   REAL,
            current_price   REAL,
            thesis          TEXT    NOT NULL,
            direction       TEXT    NOT NULL,
            asset_class     TEXT    NOT NULL,
            source_id       INTEGER REFERENCES sources(id) ON DELETE SET NULL,
            created_at      TEXT    NOT NULL,
            attachment_url  TEXT,
            attachment_name TEXT,
            attachment_data BLOB
        )
    """


_CREATE_SETTINGS = """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
"""

# One row per (project, field) tracking an in-flight or finished draft, so the
# HTTP request doesn't have to stay open while Claude works.
_CREATE_JOBS = """
    CREATE TABLE IF NOT EXISTS generation_jobs (
        id          %s,
        project_id  INTEGER NOT NULL,
        field       TEXT    NOT NULL,
        status      TEXT    NOT NULL,
        error       TEXT,
        started_at  TEXT    NOT NULL,
        finished_at TEXT
    )
""" % ('SERIAL PRIMARY KEY' if IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT')

_CREATE_JOBS_INDEX = """
    CREATE UNIQUE INDEX IF NOT EXISTS generation_jobs_project_field
    ON generation_jobs (project_id, field)
"""

_PK = 'SERIAL PRIMARY KEY' if IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'

# ── Research material attached to a project ──────────────────────────────────
# Notes are a dated log with files hanging off them; documents are a library
# with a type and a date; model versions are an ordered chain. Separate tables
# because the semantics differ — collapsing them into one "files" table would
# mean a pile of columns that are null for two of the three uses.

_CREATE_NOTES = f"""
    CREATE TABLE IF NOT EXISTS project_notes (
        id         {_PK},
        project_id INTEGER NOT NULL,
        body       TEXT    NOT NULL,
        source_id  INTEGER,
        created_at TEXT    NOT NULL,
        updated_at TEXT    NOT NULL
    )
"""

_CREATE_NOTE_FILES = f"""
    CREATE TABLE IF NOT EXISTS note_attachments (
        id         {_PK},
        note_id    INTEGER NOT NULL,
        project_id INTEGER NOT NULL,
        filename   TEXT    NOT NULL,
        object_key TEXT    NOT NULL,
        size_bytes INTEGER,
        created_at TEXT    NOT NULL
    )
"""

_CREATE_DOC_TYPES = f"""
    CREATE TABLE IF NOT EXISTS doc_types (
        id   {_PK},
        name TEXT NOT NULL UNIQUE
    )
"""

_CREATE_DOCUMENTS = f"""
    CREATE TABLE IF NOT EXISTS project_documents (
        id          {_PK},
        project_id  INTEGER NOT NULL,
        title       TEXT    NOT NULL,
        doc_type_id INTEGER,
        doc_date    TEXT,
        filename    TEXT    NOT NULL,
        object_key  TEXT    NOT NULL,
        size_bytes  INTEGER,
        created_at  TEXT    NOT NULL
    )
"""

_CREATE_MODEL_VERSIONS = f"""
    CREATE TABLE IF NOT EXISTS model_versions (
        id         {_PK},
        project_id INTEGER NOT NULL,
        version    INTEGER NOT NULL,
        label      TEXT,
        filename   TEXT    NOT NULL,
        object_key TEXT    NOT NULL,
        size_bytes INTEGER,
        created_at TEXT    NOT NULL
    )
"""

# Seeded once, then yours to edit — same pattern as sources and idea types.
DEFAULT_DOC_TYPES = [
    'Presentation', 'Transcript', 'Filing', 'Report',
    'Broker Research', 'Press Release', 'Other',
]


def get_setting(key, default=None):
    with get_conn() as conn:
        cur = cursor(conn)
        cur.execute(f'SELECT value FROM settings WHERE key = {PH}', (key,))
        row = to_dict(cur.fetchone())
    return row['value'] if row and row['value'] is not None else default


def set_setting(key, value):
    with get_conn() as conn:
        cur = cursor(conn)
        if IS_PG:
            cur.execute(
                f'INSERT INTO settings (key, value) VALUES ({PH}, {PH}) '
                f'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value',
                (key, value),
            )
        else:
            cur.execute(
                f'INSERT INTO settings (key, value) VALUES ({PH}, {PH}) '
                f'ON CONFLICT (key) DO UPDATE SET value = excluded.value',
                (key, value),
            )


def insert_id(cur, table, cols, values):
    """INSERT a row and return its new id, on either backend.

    Postgres needs RETURNING; SQLite exposes lastrowid. Callers shouldn't have
    to care which they're on.
    """
    col_list = ', '.join(cols)
    ph_list  = ', '.join([PH] * len(cols))
    if IS_PG:
        cur.execute(
            f'INSERT INTO {table} ({col_list}) VALUES ({ph_list}) RETURNING id', values,
        )
        return to_dict(cur.fetchone())['id']
    cur.execute(f'INSERT INTO {table} ({col_list}) VALUES ({ph_list})', values)
    return cur.lastrowid


def _pg_binary(data):
    from psycopg2 import Binary
    return Binary(data)


def to_dict(row):
    return dict(row) if row else None


def to_dicts(rows):
    return [dict(r) for r in rows]


def init():
    with get_conn() as conn:
        cur = cursor(conn)
        cur.execute(_CREATE_SOURCES)
        cur.execute(_CREATE_SUBTYPES)
        cur.execute(_CREATE_IDEA_TYPES)
        cur.execute(_CREATE)
        cur.execute(_CREATE_PROJECTS)
        cur.execute(_CREATE_QUESTIONS)
        cur.execute(_CREATE_SETTINGS)
        cur.execute(_CREATE_JOBS)
        cur.execute(_CREATE_JOBS_INDEX)
        cur.execute(_CREATE_NOTES)
        cur.execute(_CREATE_NOTE_FILES)
        cur.execute(_CREATE_DOC_TYPES)
        cur.execute(_CREATE_DOCUMENTS)
        cur.execute(_CREATE_MODEL_VERSIONS)

        # Seed document types on a first run only, so deletions stick.
        cur.execute('SELECT COUNT(*) AS n FROM doc_types')
        if (to_dict(cur.fetchone()) or {}).get('n', 0) == 0:
            for name in DEFAULT_DOC_TYPES:
                cur.execute(f'INSERT INTO doc_types (name) VALUES ({PH})', (name,))


_MIGRATIONS = [
    ('attachment_url',  'TEXT'),
    ('attachment_name', 'TEXT'),
    ('attachment_data', 'BYTEA' if IS_PG else 'BLOB'),
    ('source_id',       'INTEGER'),
    ('attachment_key',  'TEXT'),   # bucket object key (replaces attachment_data for new uploads)
    ('hat_tip_id',      'INTEGER'),
    ('rating',          'REAL'),
    ('idea_type',       'TEXT'),
    ('subtype_id',      'INTEGER'),
    ('idea_type_id',    'INTEGER'),
]


_NOTE_MIGRATIONS = [
    # Where the note came from — reuses the shared sources list.
    ('source_id', 'INTEGER'),
]

_PROJECT_MIGRATIONS = [
    ('business_description', 'TEXT'),
    ('pros',                 'TEXT'),
    ('cons',                 'TEXT'),
    ('key_questions',        'TEXT'),
    ('bull_case',            'TEXT'),
    ('bear_case',            'TEXT'),
    # Long-form companion to each summary, shown on its own page.
    ('business_description_detail', 'TEXT'),
    ('bull_case_detail',            'TEXT'),
    ('bear_case_detail',            'TEXT'),
    # Highest model version ever issued, so deleting one can't recycle its
    # number onto a different file.
    ('model_version_seq', 'INTEGER'),
    ('business_description_generated_at', 'TEXT'),
    ('bull_case_generated_at',            'TEXT'),
    ('bear_case_generated_at',            'TEXT'),
]


def migrate():
    """Add new columns to existing tables without breaking existing data."""
    with get_conn() as conn:
        cur = cursor(conn)
        for table, cols in (('ideas', _MIGRATIONS), ('projects', _PROJECT_MIGRATIONS),
                            ('project_notes', _NOTE_MIGRATIONS)):
            for col, typ in cols:
                if IS_PG:
                    cur.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}')
                else:
                    try:
                        cur.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typ}')
                    except Exception:
                        pass  # column already exists


PROJECT_COLS = (
    'name', 'ticker', 'direction', 'stage', 'thesis', 'rating',
    'idea_type_id', 'subtype_id', 'source_id', 'hat_tip_id',
    'origin_idea_id', 'current_price',
    'business_description', 'pros', 'cons', 'bull_case', 'bear_case',
    'key_questions', 'created_at', 'updated_at',
    'attachment_url', 'attachment_name', 'attachment_data', 'attachment_key',
)


def insert_project(conn, values: tuple) -> dict:
    """INSERT a project row and return it as a dict. values matches PROJECT_COLS."""
    col_list = ', '.join(PROJECT_COLS)
    ph_list  = ', '.join([PH] * len(PROJECT_COLS))
    cur = cursor(conn)
    if IS_PG:
        from psycopg2 import Binary
        values = tuple(Binary(v) if isinstance(v, (bytes, bytearray)) else v for v in values)
        cur.execute(
            f'INSERT INTO projects ({col_list}) VALUES ({ph_list}) RETURNING *',
            values,
        )
        row = to_dict(cur.fetchone())
    else:
        cur.execute(f'INSERT INTO projects ({col_list}) VALUES ({ph_list})', values)
        cur.execute('SELECT * FROM projects WHERE id = ?', (cur.lastrowid,))
        row = to_dict(cur.fetchone())
    row.pop('attachment_data', None)
    return row


def insert_idea(conn, values: tuple) -> dict:
    """INSERT a row and return it as a dict, works for both PG and SQLite.
    values must be a 14-tuple matching the columns below."""
    cols = (
        'ticker', 'idea_date', 'idea_price', 'initial_date', 'initial_price',
        'current_price', 'thesis', 'direction', 'asset_class', 'source_id',
        'hat_tip_id', 'rating', 'idea_type', 'subtype_id', 'idea_type_id', 'created_at',
        'attachment_url', 'attachment_name', 'attachment_data', 'attachment_key',
    )
    col_list = ', '.join(cols)
    ph_list  = ', '.join([PH] * len(cols))
    cur = cursor(conn)
    if IS_PG:
        from psycopg2 import Binary
        # Wrap bytes so psycopg2 sends them as BYTEA, not text
        values = tuple(Binary(v) if isinstance(v, (bytes, bytearray)) else v for v in values)
        cur.execute(
            f'INSERT INTO ideas ({col_list}) VALUES ({ph_list}) RETURNING *',
            values,
        )
        row = to_dict(cur.fetchone())
        # Don't send binary blob back to the client
        row.pop('attachment_data', None)
        return row
    else:
        cur.execute(f'INSERT INTO ideas ({col_list}) VALUES ({ph_list})', values)
        cur.execute('SELECT * FROM ideas WHERE id = ?', (cur.lastrowid,))
        row = to_dict(cur.fetchone())
        row.pop('attachment_data', None)
        return row
