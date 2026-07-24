"""
Minute Man v6 — object storage for attachments (Cloudflare R2).

R2 speaks the S3 API, so this is a thin boto3 wrapper — no Cloudflare-specific
SDK. Same "unset env = feature invisible, never crash the app" convention as
webhooks_out.py's SMTP handling: if the four R2_* vars aren't set, the app
runs exactly as before and the attachment endpoints return a clear 501 telling
the admin to configure storage — they never raise a raw 500.

Config (all from env — see .env.example and R2-SETUP-FOR-CHRIS.md):
  R2_ACCOUNT_ID         your Cloudflare account id
  R2_ACCESS_KEY_ID      R2 API token access key id
  R2_SECRET_ACCESS_KEY  R2 API token secret
  R2_BUCKET_NAME        the bucket (e.g. minute-man-attachments)

Nothing here stores a public URL: downloads are always short-lived *signed*
URLs generated per request from the object key, so the bucket stays private.
"""

import os
import uuid

# boto3 is only needed when R2 is configured; importing lazily keeps the app
# bootable (and the test suite runnable) without it installed.


class StorageNotConfigured(RuntimeError):
    """Raised when an attachment operation is attempted but the R2_* env vars
    are missing. main.py turns this into a friendly 501, never a 500."""


def is_configured() -> bool:
    """True only when all four R2 settings are present."""
    return all(os.getenv(k) for k in (
        "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"))


def _bucket() -> str:
    return os.environ["R2_BUCKET_NAME"]


def _client():
    """An S3 client pointed at the account's R2 endpoint. Created per call —
    cheap, thread-safe, and avoids holding a client when R2 is unconfigured."""
    if not is_configured():
        raise StorageNotConfigured(
            "File storage isn't set up yet. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME (see R2-SETUP-FOR-CHRIS.md).")
    import boto3  # local import: only needed when storage is actually used

    account_id = os.environ["R2_ACCOUNT_ID"]
    endpoint = os.getenv("R2_ENDPOINT_URL") or f"https://{account_id}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",  # R2 ignores region but boto3 wants one
    )


def make_key(meeting_id: int, filename: str | None) -> str:
    """A collision-proof object key namespaced by meeting. Keeps the original
    extension so content sniffs and downloads name sensibly."""
    ext = ""
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[1].lower()[:10]
    return f"meetings/{meeting_id}/{uuid.uuid4().hex}{ext}"


def upload_attachment(file_bytes: bytes, key: str, content_type: str | None) -> None:
    """Store bytes under `key`. Raises StorageNotConfigured when R2 is unset."""
    client = _client()
    client.put_object(
        Bucket=_bucket(), Key=key, Body=file_bytes,
        ContentType=content_type or "application/octet-stream")


def get_signed_url(key: str, expires: int = 3600, download_name: str | None = None) -> str:
    """A short-lived signed GET URL for `key`. `download_name`, when given,
    sets a Content-Disposition so the browser saves a friendly filename."""
    client = _client()
    params = {"Bucket": _bucket(), "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f'inline; filename="{download_name}"'
    return client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)


def delete_attachment(key: str) -> None:
    """Best-effort delete. Never raises for a missing object (idempotent);
    a StorageNotConfigured still propagates so callers can report it."""
    client = _client()
    client.delete_object(Bucket=_bucket(), Key=key)
