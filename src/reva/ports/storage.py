"""Object storage port. Objects are immutable and content-addressed."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO, Literal, Protocol

Namespace = Literal["raw", "normalised", "belief", "models", "scenarios", "evaluation", "quarantine"]
NAMESPACES: tuple[str, ...] = (
    "raw",
    "normalised",
    "belief",
    "models",
    "scenarios",
    "evaluation",
    "quarantine",
)


@dataclass(frozen=True)
class StoredObject:
    sha256: str
    byte_size: int
    namespace: str
    storage_uri: str
    media_type: str


@dataclass(frozen=True)
class ObjectHead:
    sha256: str
    byte_size: int
    storage_uri: str


class ArtifactStore(Protocol):
    """put streams + hashes with bounded memory and verifies bytes after write.

    A put never overwrites an existing object with different content. Objects
    are invisible to readers until their metadata is committed in PostgreSQL;
    the store itself has no notion of publication.
    """

    def put(self, stream: BinaryIO, namespace: str, media_type: str, *, max_bytes: int) -> StoredObject: ...

    def put_bytes(self, data: bytes, namespace: str, media_type: str) -> StoredObject: ...

    def get(self, namespace: str, sha256: str) -> bytes: ...

    def open(self, namespace: str, sha256: str) -> Iterator[bytes]: ...

    def head(self, namespace: str, sha256: str) -> ObjectHead | None: ...

    def range_read(self, namespace: str, sha256: str, start: int, length: int) -> bytes: ...

    def list_uris(self, namespace: str) -> Iterator[tuple[str, float]]: ...

    def delete_orphan(self, storage_uri: str) -> None: ...
