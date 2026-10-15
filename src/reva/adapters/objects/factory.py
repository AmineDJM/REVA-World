from __future__ import annotations

from functools import lru_cache

from reva.ports.storage import ArtifactStore
from reva.settings import Settings, get_settings


def build_store(settings: Settings) -> ArtifactStore:
    if settings.reva_object_backend == "s3":
        from reva.adapters.objects.s3 import S3ArtifactStore

        return S3ArtifactStore.from_settings(settings)
    if settings.reva_env == "production":  # pragma: no cover - guarded by settings validation
        raise RuntimeError("filesystem storage is never selected in production")
    from reva.adapters.objects.filesystem import FilesystemArtifactStore

    return FilesystemArtifactStore(settings.reva_object_root, settings.reva_s3_prefix)


@lru_cache(maxsize=1)
def get_store() -> ArtifactStore:
    return build_store(get_settings())


def probe_store(store: ArtifactStore) -> dict[str, object]:
    """Cheap, non-fatal object-store check for readiness endpoints (never raises, no secrets).

    S3: one bounded ListObjectsV2 under the prefix (``S3ArtifactStore.probe``).
    Filesystem: the object root exists.
    """
    probe = getattr(store, "probe", None)
    if callable(probe):
        return probe()  # type: ignore[no-any-return]
    root = getattr(store, "root", None)
    ok = root is not None and root.is_dir()
    return {"backend": "filesystem", "status": "ok" if ok else "unavailable", "error": None if ok else "NoRoot"}
