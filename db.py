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

    _CREATE = """
        CREATE TABLE IF NOT EXISTS ideas (
            id            SERIAL PRIMARY KEY,
            ticker        TEXT             NOT NULL,
            idea_date     TEXT             NOT NULL,
            idea_price    DOUBLE PRECISION,
            initial_date  TEXT             NOT NULL,
            initial_price DOUBLE PRECISION,
            current_price DOUBLE PRECISION,
            thesis        TEXT             NOT NULL,
            direction     TEXT             NOT NULL,
            asset_class   TEXT             NOT NULL,
            created_at    TEXT             NOT NULL
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

    _CREATE = """
        CREATE TABLE IF NOT EXISTS ideas (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker        TEXT    NOT NULL,
            idea_date     TEXT    NOT NULL,
            idea_price    REAL,
            initial_date  TEXT    NOT NULL,
            initial_price REAL,
            current_price REAL,
            thesis        TEXT    NOT NULL,
            direction     TEXT    NOT NULL,
            asset_class   TEXT    NOT NULL,
            created_at    TEXT    NOT NULL
        )
    """


def to_dict(row):
    return dict(row) if row else None


def to_dicts(rows):
    return [dict(r) for r in rows]


def init():
    with get_conn() as conn:
        cur = cursor(conn)
        cur.execute(_CREATE)


def insert_idea(conn, values: tuple) -> dict:
    """INSERT a row and return it as a dict, works for both PG and SQLite."""
    cols = (
        'ticker', 'idea_date', 'idea_price', 'initial_date', 'initial_price',
        'current_price', 'thesis', 'direction', 'asset_class', 'created_at',
    )
    col_list = ', '.join(cols)
    ph_list  = ', '.join([PH] * len(cols))
    cur = cursor(conn)
    if IS_PG:
        cur.execute(
            f'INSERT INTO ideas ({col_list}) VALUES ({ph_list}) RETURNING *',
            values,
        )
        return to_dict(cur.fetchone())
    else:
        cur.execute(f'INSERT INTO ideas ({col_list}) VALUES ({ph_list})', values)
        cur.execute('SELECT * FROM ideas WHERE id = ?', (cur.lastrowid,))
        return to_dict(cur.fetchone())
