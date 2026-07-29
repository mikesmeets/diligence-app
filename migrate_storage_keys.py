"""
Move existing bucket objects onto the readable key scheme.

Files uploaded before this change live at uploads/<uuid>/<name>, which tells
you nothing if you ever download the bucket on its own. This rewrites them to:

    ideas/2026-03-14 AAP/teardown.pdf
    projects/Advance Auto Parts/notes/IR call notes.pdf
    projects/Advance Auto Parts/documents/AAP_investor_day.pdf
    projects/Advance Auto Parts/model/v3 2026-07-28 1432 AAP_model.xlsx

Safe to re-run: objects already at their target key are skipped. Re-running is
also how you re-sync paths after renaming a project, since renames don't move
files on their own.

Run against production with:
    railway run python migrate_storage_keys.py

Add --dry-run to print the moves without touching anything.
"""
import sys
from datetime import datetime

import db
import storage


def _rows(sql, params=()):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(sql, params)
        return db.to_dicts(cur.fetchall())


def _set_key(table, row_id, column, key):
    with db.get_conn() as conn:
        cur = db.cursor(conn)
        cur.execute(
            f'UPDATE {table} SET {column} = {db.PH} WHERE id = {db.PH}', (key, row_id),
        )


def _idea_parts(row):
    stamp = (row.get('idea_date') or '')[:10]
    label = ' '.join(p for p in (stamp, (row.get('ticker') or '').upper()) if p)
    return ['ideas', label or 'undated']


def plan():
    """Every object that should move, as (table, id, column, old, new_parts, name)."""
    jobs = []

    for r in _rows(
        'SELECT id, ticker, idea_date, attachment_name, attachment_key FROM ideas '
        'WHERE attachment_key IS NOT NULL'
    ):
        jobs.append(('ideas', r['id'], 'attachment_key', r['attachment_key'],
                     _idea_parts(r), r['attachment_name'] or 'attachment'))

    for r in _rows(
        'SELECT id, name, attachment_name, attachment_key FROM projects '
        'WHERE attachment_key IS NOT NULL'
    ):
        jobs.append(('projects', r['id'], 'attachment_key', r['attachment_key'],
                     ['projects', r['name']], r['attachment_name'] or 'attachment'))

    for r in _rows(
        'SELECT a.id, a.filename, a.object_key, p.name FROM note_attachments a '
        'JOIN projects p ON a.project_id = p.id'
    ):
        jobs.append(('note_attachments', r['id'], 'object_key', r['object_key'],
                     ['projects', r['name'], 'notes'], r['filename']))

    for r in _rows(
        'SELECT d.id, d.filename, d.object_key, p.name FROM project_documents d '
        'JOIN projects p ON d.project_id = p.id'
    ):
        jobs.append(('project_documents', r['id'], 'object_key', r['object_key'],
                     ['projects', r['name'], 'documents'], r['filename']))

    for r in _rows(
        'SELECT m.id, m.version, m.filename, m.object_key, m.created_at, p.name '
        'FROM model_versions m JOIN projects p ON m.project_id = p.id'
    ):
        # Same shape as a fresh upload: version, then the moment it arrived.
        stamp = ''
        try:
            stamp = datetime.fromisoformat(r['created_at']).strftime('%Y-%m-%d %H%M') + ' '
        except (TypeError, ValueError):
            pass
        jobs.append(('model_versions', r['id'], 'object_key', r['object_key'],
                     ['projects', r['name'], 'model'],
                     f"v{r['version']} {stamp}{r['filename']}"))

    return jobs


def main():
    dry_run = '--dry-run' in sys.argv

    if not storage.ENABLED:
        sys.exit('Object storage is not configured — nothing to migrate.')

    db.init()
    db.migrate()

    jobs = plan()
    if not jobs:
        print('No stored objects found.')
        return

    moved = skipped = failed = 0
    for table, row_id, column, old_key, parts, filename in jobs:
        new_key = storage.build_key(parts, filename)
        if old_key == new_key:
            skipped += 1
            continue

        if dry_run:
            print(f'  {old_key}\n    -> {new_key}')
            moved += 1
            continue

        try:
            final_key = storage.move(old_key, new_key)
            _set_key(table, row_id, column, final_key)
            print(f'  moved {table}#{row_id} -> {final_key}')
            moved += 1
        except Exception as exc:
            # Leave the row pointing at the old key so the file stays reachable.
            print(f'  FAILED {table}#{row_id} ({old_key}): {type(exc).__name__}: {exc}')
            failed += 1

    verb = 'would move' if dry_run else 'moved'
    print(f'\n{verb} {moved}, already correct {skipped}, failed {failed}')
    if failed:
        print('Failed rows still point at their original key and remain downloadable.')


if __name__ == '__main__':
    main()
