"""S3-compatible ArtifactStore.

SHA-256 is computed locally while spooling and sent as ChecksumSHA256 (the
provider validates the upload) and stored as object metadata; ETags are never
treated as SHA-256. Writes are conditional (If-None-Match: *) so an object is
never overwritten.

Verification never trusts REVA-written metadata alone (review R-16):

* an object that already existed (HEAD hit, or the conditional PUT lost a race)
  is re-hashed by streaming its content (bounded memory, aborted as soon as it
  exceeds the expected size) and its ``reva-sha256`` metadata must also match;
* a freshly uploaded object is checked against the provider-computed
  full-object ``ChecksumSHA256`` (HEAD with ChecksumMode=ENABLED); when the
  provider returns none or a composite (multipart) checksum, it is stream-hashed.

S3-compatible stores that ignore ``If-None-Match`` could overwrite an existing
object between HEAD and PUT; the post-write content verification still detects
a mismatch (IntegrityFailure) but cannot undo the overwrite.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
from collections.abc import Iterator
from typing import Any, BinaryIO

from reva.adapters.objects.common import CHUNK, check_namespace, object_key, spool_and_hash, verify
from reva.kernel.errors import IntegrityFailure
from reva.ports.storage import ObjectHead, StoredObject


class S3ArtifactStore:
    def __init__(
        self,
        client: Any,
        bucket: str,
        prefix: str = "reva/",
        spool_dir: str | None = None,
        default_max_bytes: int = 1 << 30,
    ):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix
        self.spool_dir = spool_dir
        self.default_max_bytes = default_max_bytes

    @classmethod
    def from_settings(cls, settings: Any) -> S3ArtifactStore:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=settings.reva_s3_endpoint_url,
            region_name=settings.reva_s3_region,
            aws_access_key_id=settings.reva_s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.reva_s3_secret_access_key.get_secret_value(),
            config=Config(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=10),
        )
        return cls(client, settings.reva_s3_bucket, settings.reva_s3_prefix)

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def put(
        self, stream: BinaryIO, namespace: str, media_type: str, *, max_bytes: int | None = None
    ) -> StoredObject:
        check_namespace(namespace)
        tmp, sha, size = spool_and_hash(stream, self.spool_dir, max_bytes or self.default_max_bytes)
        key = object_key(self.prefix, namespace, sha)
        try:
            existing = self._head_key(key)
            uploaded = False
            if existing is None:
                checksum = base64.b64encode(binascii.unhexlify(sha)).decode()
                with open(tmp, "rb") as body:
                    try:
                        self.client.put_object(
                            Bucket=self.bucket,
                            Key=key,
                            Body=body,
                            ContentType=media_type,
                            ContentLength=size,
                            ChecksumSHA256=checksum,
                            Metadata={"reva-sha256": sha},
                            IfNoneMatch="*",
                        )
                        uploaded = True
                    except self.client.exceptions.ClientError as e:  # pragma: no cover - provider
                        code = e.response.get("Error", {}).get("Code")
                        if code not in ("PreconditionFailed", "ConditionalRequestConflict"):
                            raise
                existing = self._head_key(key)
            if existing is None:
                raise IntegrityFailure("object missing after upload", key=key)
            # Metadata must be consistent (head() reports it) ...
            verify(sha, size, existing.sha256, existing.byte_size)
            # ... but the bytes themselves are what is verified.
            if not (uploaded and self._server_checksum_matches(key, sha, size)):
                got_sha, got_size = self._stream_hash(key, size)
                verify(sha, size, got_sha, got_size)
        finally:
            os.unlink(tmp)
        return StoredObject(sha, size, namespace, self.uri(key), media_type)

    def _server_checksum_matches(self, key: str, sha: str, size: int) -> bool:
        """True only when the provider reports a full-object SHA-256 equal to ``sha``."""
        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except Exception:  # noqa: BLE001 - unsupported parameter/provider: fall back to streaming
            return False
        server = r.get("ChecksumSHA256")
        if not server or "-" in server or r.get("ChecksumType", "FULL_OBJECT") != "FULL_OBJECT":
            return False
        try:
            server_hex = base64.b64decode(server, validate=True).hex()
        except (binascii.Error, ValueError):
            return False
        return server_hex == sha and int(r.get("ContentLength", -1)) == size

    def _stream_hash(self, key: str, expected_size: int) -> tuple[str, int]:
        """SHA-256 and size of the stored bytes; one chunk in memory; stops past ``expected_size``."""
        import hashlib

        r = self.client.get_object(Bucket=self.bucket, Key=key)
        body = r["Body"]
        h = hashlib.sha256()
        n = 0
        try:
            while chunk := body.read(CHUNK):
                n += len(chunk)
                if n > expected_size:
                    break  # larger than expected: a mismatch; do not read further
                h.update(chunk)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        return h.hexdigest(), n

    def put_bytes(self, data: bytes, namespace: str, media_type: str) -> StoredObject:
        return self.put(io.BytesIO(data), namespace, media_type, max_bytes=max(len(data), 1))

    def _head_key(self, key: str) -> ObjectHead | None:
        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        meta_sha = (r.get("Metadata") or {}).get("reva-sha256", "")
        return ObjectHead(meta_sha, int(r["ContentLength"]), self.uri(key))

    def head(self, namespace: str, sha256: str) -> ObjectHead | None:
        return self._head_key(object_key(self.prefix, namespace, sha256))

    def get(self, namespace: str, sha256: str) -> bytes:
        import hashlib

        r = self.client.get_object(Bucket=self.bucket, Key=object_key(self.prefix, namespace, sha256))
        data = r["Body"].read()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise IntegrityFailure("stored object bytes do not match their address", sha256=sha256)
        return bytes(data)

    def open(self, namespace: str, sha256: str) -> Iterator[bytes]:
        r = self.client.get_object(Bucket=self.bucket, Key=object_key(self.prefix, namespace, sha256))
        yield from r["Body"].iter_chunks(1024 * 1024)

    def range_read(self, namespace: str, sha256: str, start: int, length: int) -> bytes:
        if start < 0 or length <= 0:
            raise ValueError("invalid range")
        r = self.client.get_object(
            Bucket=self.bucket,
            Key=object_key(self.prefix, namespace, sha256),
            Range=f"bytes={start}-{start + length - 1}",
        )
        return bytes(r["Body"].read())

    def list_uris(self, namespace: str) -> Iterator[tuple[str, float]]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}{namespace}/"):
            for obj in page.get("Contents", []):
                yield self.uri(obj["Key"]), obj["LastModified"].timestamp()

    def uri_mtime(self, storage_uri: str) -> float | None:
        """Current LastModified (seconds) for a storage URI, or None if absent."""
        prefix = f"s3://{self.bucket}/{self.prefix}"
        if not storage_uri.startswith(prefix):
            raise ValueError("storage URI outside the configured prefix")
        try:
            r = self.client.head_object(Bucket=self.bucket, Key=storage_uri.removeprefix(f"s3://{self.bucket}/"))
        except self.client.exceptions.ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return float(r["LastModified"].timestamp())

    def delete_orphan(self, storage_uri: str) -> None:
        prefix = f"s3://{self.bucket}/{self.prefix}"
        if not storage_uri.startswith(prefix):
            raise ValueError("refusing to delete outside the configured prefix")
        self.client.delete_object(Bucket=self.bucket, Key=storage_uri.removeprefix(f"s3://{self.bucket}/"))
