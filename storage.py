"""
Object storage via S3-compatible bucket (Tigris on Railway).

Keys are laid out so a raw bucket download is navigable on its own, without
the database to interpret it:

    ideas/2026-03-14 AAP/teardown.pdf
    projects/Advance Auto Parts/notes/IR call notes.pdf
    projects/Advance Auto Parts/documents/AAP_investor_day.pdf
    projects/Advance Auto Parts/model/v3 AAP_model.xlsx

Falls back gracefully: if env vars are missing, ENABLED is False and callers
keep using DB binary storage.
"""
import os
import re
import uuid

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

_ENDPOINT = os.environ.get('AWS_ENDPOINT_URL', '')
_REGION   = 'auto'   # Tigris requires 'auto'; ignore AWS_REGION to avoid Railway overrides
_BUCKET   = os.environ.get('AWS_S3_BUCKET_NAME', '')
_KEY_ID   = os.environ.get('AWS_ACCESS_KEY_ID', '')
_SECRET   = os.environ.get('AWS_SECRET_ACCESS_KEY', '')

ENABLED = bool(_ENDPOINT and _BUCKET and _KEY_ID and _SECRET)

# Characters that break paths once a bucket is synced to a real filesystem.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _client():
    return boto3.client(
        's3',
        endpoint_url=_ENDPOINT,
        aws_access_key_id=_KEY_ID,
        aws_secret_access_key=_SECRET,
        region_name=_REGION,
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'},
        ),
    )


def safe_segment(text, fallback='untitled'):
    """Make one path segment safe for both S3 and a local filesystem."""
    s = _UNSAFE.sub('-', str(text or ''))
    s = re.sub(r'\s+', ' ', s).strip().strip('.')
    return s[:80].strip() or fallback


def build_key(parts, filename):
    """Join folder parts and a filename into a sanitised object key."""
    segments = [safe_segment(p) for p in parts if p not in (None, '')]
    return '/'.join(segments + [safe_segment(filename, 'file')])


def exists(key: str) -> bool:
    try:
        _client().head_object(Bucket=_BUCKET, Key=key)
        return True
    except ClientError:
        return False


def _free_key(key: str) -> str:
    """Append ' (2)', ' (3)'… if the key is taken, so nothing is overwritten."""
    if not exists(key):
        return key
    folder, _, name = key.rpartition('/')
    stem, dot, ext = name.rpartition('.')
    if not dot:
        stem, ext = name, ''
    suffix = f'.{ext}' if ext else ''
    for n in range(2, 100):
        candidate = f'{folder}/{stem} ({n}){suffix}' if folder else f'{stem} ({n}){suffix}'
        if not exists(candidate):
            return candidate
    # Pathological case only — never silently overwrite.
    return f'{folder}/{stem} ({uuid.uuid4().hex[:8]}){suffix}'


def upload(file_bytes: bytes, filename: str, parts=None) -> str:
    """Upload bytes under a readable path; return the object key.

    `parts` is the folder chain, e.g. ['projects', 'Advance Auto Parts', 'notes'].
    Omitting it keeps the old flat 'uploads/<uuid>' behaviour.
    """
    if parts:
        key = _free_key(build_key(parts, filename))
    else:
        key = f'uploads/{uuid.uuid4().hex}/{safe_segment(filename, "file")}'
    _client().put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=file_bytes,
        ContentDisposition=f'inline; filename="{filename}"',
    )
    return key


def presigned_url(key: str, expires: int = 3600, download_as: str = None) -> str:
    """Return a presigned GET URL valid for `expires` seconds.

    Pass download_as to force a save-as with that filename rather than letting
    the browser try to display it — right for spreadsheets and decks.
    """
    params = {'Bucket': _BUCKET, 'Key': key}
    if download_as:
        params['ResponseContentDisposition'] = f'attachment; filename="{download_as}"'
    return _client().generate_presigned_url(
        'get_object', Params=params, ExpiresIn=expires,
    )


def read(key: str) -> bytes:
    """Fetch an object's bytes — needed when the server has to alter a file."""
    return _client().get_object(Bucket=_BUCKET, Key=key)['Body'].read()


def move(old_key: str, new_key: str) -> str:
    """Copy an object to a new key and drop the old one. Returns the key in use."""
    if old_key == new_key:
        return old_key
    target = _free_key(new_key)
    client = _client()
    client.copy_object(
        Bucket=_BUCKET, Key=target, CopySource={'Bucket': _BUCKET, 'Key': old_key},
    )
    client.delete_object(Bucket=_BUCKET, Key=old_key)
    return target


def delete(key: str) -> None:
    """Delete an object from the bucket (best-effort, ignores missing)."""
    try:
        _client().delete_object(Bucket=_BUCKET, Key=key)
    except Exception:
        pass
