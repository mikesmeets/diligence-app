"""
One-off script: move file attachments from the database to the bucket.

Run with:  railway run python migrate_attachments.py
"""
import db
import storage

if not storage.ENABLED:
    print("ERROR: bucket env vars not set. Run via 'railway run python migrate_attachments.py'")
    exit(1)

with db.get_conn() as conn:
    cur = db.cursor(conn)
    cur.execute(
        "SELECT id, attachment_name, attachment_data "
        "FROM ideas "
        "WHERE attachment_data IS NOT NULL AND attachment_key IS NULL"
    )
    rows = db.to_dicts(cur.fetchall())

if not rows:
    print("Nothing to migrate.")
    exit(0)

print(f"Migrating {len(rows)} file(s) to bucket…")

migrated = 0
errors   = 0
for row in rows:
    idea_id = row['id']
    name    = row['attachment_name'] or f'attachment_{idea_id}'
    data    = row['attachment_data']
    if isinstance(data, memoryview):
        data = bytes(data)

    try:
        key = storage.upload(data, name)
        with db.get_conn() as conn:
            cur = db.cursor(conn)
            cur.execute(
                f"UPDATE ideas SET attachment_key = {db.PH}, attachment_data = NULL WHERE id = {db.PH}",
                (key, idea_id),
            )
        print(f"  [{idea_id}] {name} → {key}")
        migrated += 1
    except Exception as e:
        print(f"  [{idea_id}] {name} FAILED: {e}")
        errors += 1

print(f"\nDone. {migrated} migrated, {errors} failed.")
