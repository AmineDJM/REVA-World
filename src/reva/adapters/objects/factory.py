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
