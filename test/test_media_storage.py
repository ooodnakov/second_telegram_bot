from __future__ import annotations

from pathlib import Path

import pytest


def _make_minio_storage(bot_modules, tmp_path: Path):
    client = bot_modules.media_storage.Minio(
        "minio", access_key=None, secret_key=None, secure=False
    )
    storage = bot_modules.media_storage.MinioMediaStorage(
        client,
        bucket="secure-bucket",
        cache_dir=tmp_path / "cache",
    )
    return client, storage


def test_local_allocate_path_sanitizes_filename(tmp_path: Path, bot_modules) -> None:
    storage = bot_modules.media_storage.LocalMediaStorage(tmp_path / "media")
    session = storage.create_session(123)

    path = storage.allocate_path(session, "../evil.jpg")

    assert path.parent == session.directory
    assert path.name == "evil.jpg"


def test_local_storage_rejects_traversal(tmp_path: Path, bot_modules) -> None:
    storage = bot_modules.media_storage.LocalMediaStorage(tmp_path / "media")
    session = storage.create_session(321)

    photo_path = storage.allocate_path(session, "photo.jpg")
    photo_path.write_bytes(b"data")
    handle = storage.finalize_upload(session, photo_path)

    cached = storage.cache_photo(handle)
    assert cached.exists()
    assert cached.is_relative_to((tmp_path / "media").resolve())

    with pytest.raises(FileNotFoundError):
        storage.cache_photo("../outside.jpg")

    with pytest.raises(RuntimeError):
        storage.get_session("../escape")


def test_minio_storage_rejects_traversal(tmp_path: Path, bot_modules) -> None:
    _client, storage = _make_minio_storage(bot_modules, tmp_path)
    session = storage.create_session(111)

    photo_path = storage.allocate_path(session, "photo.jpg")
    photo_path.write_bytes(b"minio")
    handle = storage.finalize_upload(session, photo_path)

    cached = storage.cache_photo(handle)
    assert cached.exists()
    assert cached.is_relative_to((tmp_path / "cache").resolve())

    with pytest.raises(FileNotFoundError):
        storage.cache_photo("../escape.jpg")
