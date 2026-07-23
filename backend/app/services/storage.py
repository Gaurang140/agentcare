"""Document storage adapters behind one interface: `.save(patient_id,
filename, content) -> str` returning a storage reference.

LocalStorage is fully implemented and is the default (STORAGE_BACKEND=local).
GCSStorage matches the same interface for STORAGE_BACKEND=gcs; the
google-cloud-storage dependency is intentionally not installed yet, so it is
imported lazily inside __init__ and fails with a clear AppError, not an
ImportError, if the backend is selected without the package present.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.config import settings
from app.exceptions import AppError

# Anything outside this set is replaced with "_", which also collapses any
# path separator (/, \) or ".." traversal component to a harmless literal.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_filename(filename: str) -> str:
    """Strip directory components and path-traversal-unsafe characters.

    `Path(...).name` drops any leading directories from either separator
    style, then the regex whitelist removes everything that isn't a plain
    filename character - no "/", "\\", or ".." can survive into the path we
    build on disk (or the object key we build for GCS).
    """
    base = Path(filename.replace("\\", "/")).name.strip()
    safe = _SAFE_CHARS.sub("_", base)
    return safe or "upload"


class LocalStorage:
    """Writes uploaded bytes under settings.upload_dir/{patient_id}/{uuid}_{name}."""

    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = Path(base_dir or settings.upload_dir)

    def save(self, patient_id: int, filename: str, content: bytes) -> str:
        safe_name = _sanitize_filename(filename)
        patient_dir = self.base_dir / str(patient_id)
        patient_dir.mkdir(parents=True, exist_ok=True)

        stored_name = f"{uuid.uuid4().hex}_{safe_name}"
        target = patient_dir / stored_name
        target.write_bytes(content)
        return str(target)


class GCSStorage:
    """Same interface as LocalStorage, backed by a Google Cloud Storage bucket."""

    def __init__(self, bucket_name: str | None = None) -> None:
        try:
            from google.cloud import storage as gcs_sdk
        except ImportError as exc:
            raise AppError(
                "google-cloud-storage is not installed. Add "
                "google-cloud-storage>=2.16 to requirements.txt to use "
                "STORAGE_BACKEND=gcs."
            ) from exc

        self.bucket_name = bucket_name or settings.gcs_bucket
        self._client = gcs_sdk.Client()
        self._bucket = self._client.bucket(self.bucket_name)

    def save(self, patient_id: int, filename: str, content: bytes) -> str:
        safe_name = _sanitize_filename(filename)
        blob_name = f"{patient_id}/{uuid.uuid4().hex}_{safe_name}"
        blob = self._bucket.blob(blob_name)
        blob.upload_from_string(content)
        return f"gs://{self.bucket_name}/{blob_name}"


def get_storage() -> LocalStorage | GCSStorage:
    """Return the storage backend selected by settings.storage_backend."""
    if settings.storage_backend == "gcs":
        return GCSStorage()
    return LocalStorage()
