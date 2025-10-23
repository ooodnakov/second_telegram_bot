"""Media storage abstractions for managing uploaded photos."""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from bot.logging import logger
from minio import Minio
from minio.error import S3Error


@dataclass(slots=True)
class MediaSession:
    """Represents a logical submission session backed by storage."""

    key: str
    directory: Path | None = None


def _resolve_within(base: Path, relative: str) -> Path:
    """Resolve ``relative`` against ``base`` ensuring the result stays within ``base``."""

    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise FileNotFoundError(relative)

    base_resolved = base.resolve()
    target = (base_resolved / relative_path).resolve(strict=False)
    if not target.is_relative_to(base_resolved):
        raise FileNotFoundError(relative)
    return target


class MediaStorage(ABC):
    """Abstract base class describing media storage capabilities."""

    def __init__(self, *, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir
        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_dir = self._cache_dir.resolve()

    @abstractmethod
    def create_session(self, user_id: int) -> MediaSession:
        """Create a new media session for the given user."""

    @abstractmethod
    def get_session(self, session_key: str) -> MediaSession:
        """Return a handle to an existing session, creating it if needed."""

    @abstractmethod
    def allocate_path(self, session: MediaSession, filename: str) -> Path:
        """Return a filesystem path where an uploaded photo should be stored."""

    @abstractmethod
    def finalize_upload(self, session: MediaSession, path: Path) -> str:
        """Finalize the upload of ``path`` and return a persistent handle."""

    @abstractmethod
    def list_photo_handles(self, session_key: str) -> list[str]:
        """List persisted photo handles for ``session_key``."""

    @abstractmethod
    def cache_photo(self, handle: str) -> Path:
        """Ensure ``handle`` is materialized locally and return the path."""

    def cache_photos(self, handles: Iterable[str]) -> list[Path]:
        """Ensure a sequence of handles are cached locally."""

        cached: list[Path] = []
        seen: set[str] = set()
        for handle in handles:
            normalized = handle.strip() if isinstance(handle, str) else ""
            if not normalized or normalized in seen:
                continue
            if ".." in Path(normalized).parts:
                logger.warning("Skipping suspicious photo handle %s", normalized)
                continue
            seen.add(normalized)
            try:
                path = self.cache_photo(normalized)
            except FileNotFoundError:
                logger.warning("Photo handle %s could not be cached", normalized)
                continue
            if path.exists():
                cached.append(path)
        return cached

    def open_photo_stream(self, handle: str):
        """Open a binary stream for the provided photo handle."""

        path = self.cache_photo(handle)
        return path.open("rb")


def _generate_session_key(user_id: int) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{user_id}_{timestamp}_{uuid4().hex[:6]}"


class LocalMediaStorage(MediaStorage):
    """Media storage implementation backed by the local filesystem."""

    def __init__(self, root: Path, *, cache_dir: Path | None = None) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._root = self._root.resolve()
        super().__init__(cache_dir=cache_dir or root)

    def create_session(self, user_id: int) -> MediaSession:
        key = _generate_session_key(user_id)
        directory = _resolve_within(self._root, key)
        directory.mkdir(parents=True, exist_ok=True)
        logger.debug("Created local media session %s at %s", key, directory)
        return MediaSession(key=key, directory=directory)

    def get_session(self, session_key: str) -> MediaSession:
        try:
            directory = _resolve_within(self._root, session_key)
        except FileNotFoundError as exc:
            logger.warning(
                "Rejected session key %s that resolves outside storage root",
                session_key,
            )
            raise RuntimeError("Invalid session key") from exc
        directory.mkdir(parents=True, exist_ok=True)
        return MediaSession(key=session_key, directory=directory)

    def allocate_path(self, session: MediaSession, filename: str) -> Path:
        directory = self._ensure_session_directory(session)
        sanitized = Path(filename).name
        if not sanitized:
            raise ValueError("Filename must not be empty")
        return directory / sanitized

    def finalize_upload(self, session: MediaSession, path: Path) -> str:
        directory = self._ensure_session_directory(session)
        try:
            source = Path(path).resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(path) from exc

        if not source.is_relative_to(directory):
            target = directory / source.name
            shutil.move(str(source), target)
            source = target.resolve(strict=True)

        handle = f"{session.key}/{source.name}"
        logger.debug("Stored local media handle %s", handle)
        return handle

    def list_photo_handles(self, session_key: str) -> list[str]:
        try:
            directory = _resolve_within(self._root, session_key)
        except FileNotFoundError:
            logger.warning(
                "Skipping photo listing for invalid session key %s", session_key
            )
            return []
        if not directory.exists():
            return []
        handles: list[str] = []
        for item in sorted(directory.iterdir()):
            if item.is_file():
                handles.append(f"{session_key}/{item.name}")
        return handles

    def cache_photo(self, handle: str) -> Path:
        candidate = Path(handle)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
            if not resolved.exists():
                raise FileNotFoundError(handle)
            if not resolved.is_relative_to(self._root):
                raise FileNotFoundError(handle)
            return resolved

        path = _resolve_within(self._root, handle)
        if not path.exists():
            raise FileNotFoundError(handle)
        return path

    def _ensure_session_directory(self, session: MediaSession) -> Path:
        directory = session.directory
        if directory is not None:
            resolved = Path(directory).resolve(strict=False)
            if not resolved.is_relative_to(self._root):
                raise RuntimeError("Session directory escapes storage root")
            resolved.mkdir(parents=True, exist_ok=True)
            return resolved

        directory = _resolve_within(self._root, session.key)
        directory.mkdir(parents=True, exist_ok=True)
        return directory


class MinioMediaStorage(MediaStorage):
    """Media storage implementation backed by a MinIO bucket."""

    def __init__(
        self,
        client: Minio,
        bucket: str,
        *,
        object_prefix: str = "",
        cache_dir: Path,
    ) -> None:
        super().__init__(cache_dir=cache_dir)
        self._client = client
        self._bucket = bucket
        prefix = object_prefix.strip("/")
        self._prefix = f"{prefix}/" if prefix else ""
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
        except S3Error as exc:  # pragma: no cover - depends on remote service
            logger.exception("Failed to ensure MinIO bucket %s: %s", self._bucket, exc)
            raise

    def create_session(self, user_id: int) -> MediaSession:
        key = _generate_session_key(user_id)
        logger.debug("Created MinIO media session %s", key)
        return MediaSession(key=key)

    def get_session(self, session_key: str) -> MediaSession:
        return MediaSession(key=session_key)

    def allocate_path(self, session: MediaSession, filename: str) -> Path:
        directory = (self._cache_dir or Path(".")) / session.key
        directory.mkdir(parents=True, exist_ok=True)
        return directory / filename

    def finalize_upload(self, session: MediaSession, path: Path) -> str:
        object_name = self._object_name(f"{session.key}/{path.name}")
        self._client.fput_object(self._bucket, object_name, str(path))
        logger.debug(
            "Uploaded media %s to MinIO bucket %s as %s",
            path,
            self._bucket,
            object_name,
        )
        return f"{session.key}/{path.name}"

    def list_photo_handles(self, session_key: str) -> list[str]:
        prefix = self._object_name(f"{session_key}/")
        handles: list[str] = []
        try:
            for obj in self._client.list_objects(  # type: ignore[attr-defined]
                self._bucket, prefix=prefix, recursive=True
            ):
                object_name = getattr(obj, "object_name", "")
                if not object_name:
                    continue
                handles.append(self._handle_from_object_name(object_name))
        except S3Error as exc:  # pragma: no cover - depends on remote service
            logger.warning(
                "Failed to list objects for session %s: %s", session_key, exc
            )
        return handles

    def cache_photo(self, handle: str) -> Path:
        if self._cache_dir is None:
            raise RuntimeError("Cache directory not configured for MinIO storage")
        if Path(handle).is_absolute():
            raise FileNotFoundError(handle)

        base = self._cache_dir
        target = (base / Path(handle)).resolve(strict=False)
        if not target.is_relative_to(base):
            raise FileNotFoundError(handle)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            object_name = self._object_name(handle)
            try:
                self._client.fget_object(self._bucket, object_name, str(target))
            except S3Error as exc:
                logger.warning(
                    "Failed to download %s from MinIO bucket %s: %s",
                    object_name,
                    self._bucket,
                    exc,
                )
                raise FileNotFoundError(handle) from exc
        return target

    def _object_name(self, handle: str) -> str:
        normalized = handle.lstrip("/")
        return f"{self._prefix}{normalized}"

    def _handle_from_object_name(self, object_name: str) -> str:
        if self._prefix and object_name.startswith(self._prefix):
            return object_name[len(self._prefix) :]
        return object_name


def create_media_storage(settings: dict[str, object] | None) -> MediaStorage:
    """Factory that builds the configured media storage implementation."""

    settings = settings or {}
    backend = str(settings.get("backend", "local")).strip().lower()
    cache_dir_value = settings.get("cache_dir")
    cache_dir = Path(str(cache_dir_value)).expanduser() if cache_dir_value else None

    if backend == "local":
        root_value = settings.get("local_root")
        if root_value:
            root = Path(str(root_value)).expanduser()
        else:
            root = Path(__file__).resolve().parent.parent / "media"
        return LocalMediaStorage(root=root, cache_dir=cache_dir)

    if backend == "minio":
        minio_settings = settings.get("minio", {})
        if not isinstance(minio_settings, dict):
            raise RuntimeError("Invalid MinIO storage configuration")
        endpoint = str(minio_settings.get("endpoint", "")).strip()
        if not endpoint:
            raise RuntimeError("MinIO endpoint must be configured")
        bucket = str(minio_settings.get("bucket", "")).strip()
        if not bucket:
            raise RuntimeError("MinIO bucket must be configured")
        access_key = str(minio_settings.get("access_key", "")).strip() or None
        secret_key = str(minio_settings.get("secret_key", "")).strip() or None
        secure_value = minio_settings.get("secure", True)
        secure = bool(secure_value)
        prefix = str(minio_settings.get("prefix", "")).strip()
        cache_path = cache_dir or Path(
            minio_settings.get("cache_dir", Path.cwd() / "media_cache")
        )
        cache_path = Path(cache_path).expanduser()
        client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        return MinioMediaStorage(
            client,
            bucket=bucket,
            object_prefix=prefix,
            cache_dir=cache_path,
        )

    raise RuntimeError(f"Unsupported media storage backend: {backend}")


def get_media_storage(context) -> MediaStorage:
    """Retrieve the configured :class:`MediaStorage` from bot data."""

    storage = context.application.bot_data.get("media_storage")
    if storage is None:
        raise RuntimeError("Media storage is not configured")
    return storage


__all__ = [
    "MediaSession",
    "MediaStorage",
    "LocalMediaStorage",
    "MinioMediaStorage",
    "create_media_storage",
    "get_media_storage",
]
