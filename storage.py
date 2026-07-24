"""
Minute Man v6 — object storage for attachments (S3-compatible).

Works with any S3-compatible object store — Backblaze B2 (the free, no-card
default), Cloudflare R2, AWS S3, etc. It's a thin boto3 wrapper, no
vendor SDK. Same "unset env = feature invisible, never crash the app"
convention as webhooks_out.py's SMTP handling: if storage isn't configured the
app runs exactly as before and the attachment endpoints return a clear 501
telling the admin to configure storage — they never raise a raw 500.

Config (all from env — see .env.example and STORAGE-SETUP-FOR-CHRIS.md):

  Backblaze B2 (recommended, no credit card, 10GB free):
    S3_ENDPOINT_URL       e.g. https://s3.us-west-004.backblazeb2.com
    S3_ACCESS_KEY_ID      the B2 application keyID
    S3_SECRET_ACCESS_KEY  the B2 applicationKey
    S3_BUCKET_NAME        the bucket (e.g. minute-man-attachments)
    S3_REGION             optional — auto-parsed from a B2 endpoint if omitted

  Cloudflare R2 (still supported for backward compatibility):
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME

If both are set, S3_* wins. Nothing here stores a public URL: downloads are
always short-lived *signed* URLs generated per request from the object key, so
the bucket stays private.
"""

import os
import re
import uuid

# boto3 is only needed when storage is configured; importing lazily keeps the
# app bootable (and the test suite runnable) without it installed.


class StorageNotConfigured(RuntimeError):
    """Raised when an attachment operation is attempted but storage env vars
    are missing. main.py turns this into a friendly 501, never a 500."""


def _mode() -> str | None:
    """Which provider is configured: 's3' (B2/generic), 'r2', or None."""
    if all(os.getenv(k) for k in (
            "S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID",
            "S3_SECRET_ACCESS_KEY", "S3_BUCKET_NAME")):
        return "s3"
    if all(os.getenv(k) for k in (
            "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")):
        return "r2"
    return None


def is_configured() -> bool:
    """True when a complete set of storage settings is present (S3 or R2)."""
    return _mode() is not None


def _bucket() -> str:
    return os.environ["S3_BUCKET_NAME" if _mode() == "s3" else "R2_BUCKET_NAME"]


def _region_for(endpoint: str) -> str:
    """B2's S3 API signs with the real region (e.g. us-west-004), which is
    embedded in the endpoint host. Parse it; fall back to 'auto' (R2 ignores
    region) so any other S3 host still works."""
    explicit = os.getenv("S3_REGION")
    if explicit:
        return explicit
    m = re.search(r"s3\.([a-z0-9-]+)\.backblazeb2\.com", endpoint or "")
    return m.group(1) if m else "auto"


def _client():
    """An S3 client pointed at the configured endpoint. Created per call —
    cheap, thread-safe, and avoids holding a client when storage is unset."""
    mode = _mode()
    if mode is None:
        raise StorageNotConfigured(
            "File storage isn't set up yet. Set S3_ENDPOINT_URL, "
            "S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY and S3_BUCKET_NAME "
            "(see STORAGE-SETUP-FOR-CHRIS.md).")
    import boto3  # local import: only needed when storage is actually used

    if mode == "s3":
        endpoint = os.environ["S3_ENDPOINT_URL"]
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
            region_name=_region_for(endpoint),
        )

    # Cloudflare R2 (backward compatibility)
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
    """Store bytes under `key`. Raises StorageNotConfigured when storage is unset."""
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
