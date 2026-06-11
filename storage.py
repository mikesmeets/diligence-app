"""
Object storage via S3-compatible bucket (Tigris on Railway).

Falls back gracefully: if env vars are missing, ENABLED is False and
callers keep using DB binary storage.
"""
import os
import uuid
import boto3
from botocore.client import Config

_ENDPOINT = os.environ.get('AWS_ENDPOINT_URL', '')
_REGION   = os.environ.get('AWS_REGION', 'auto')
_BUCKET   = os.environ.get('AWS_S3_BUCKET_NAME', '')
_KEY_ID   = os.environ.get('AWS_ACCESS_KEY_ID', '')
_SECRET   = os.environ.get('AWS_SECRET_ACCESS_KEY', '')

ENABLED = bool(_ENDPOINT and _BUCKET and _KEY_ID and _SECRET)


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


def upload(file_bytes: bytes, filename: str) -> str:
    """Upload bytes to bucket; return the object key."""
    key = f'uploads/{uuid.uuid4().hex}/{filename}'
    _client().put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=file_bytes,
        ContentDisposition=f'inline; filename="{filename}"',
    )
    return key


def presigned_url(key: str, expires: int = 3600) -> str:
    """Return a presigned GET URL valid for `expires` seconds."""
    return _client().generate_presigned_url(
        'get_object',
        Params={'Bucket': _BUCKET, 'Key': key},
        ExpiresIn=expires,
    )


def delete(key: str) -> None:
    """Delete an object from the bucket (best-effort, ignores missing)."""
    try:
        _client().delete_object(Bucket=_BUCKET, Key=key)
    except Exception:
        pass
