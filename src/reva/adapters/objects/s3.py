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

Reads verify too: ``get`` hashes the bytes it returns and ``open`` hashes while
streaming and raises ``IntegrityFailure`` after the last chunk if the content
does not match its address (a consumer that stops early gets no verdict).

S3-compatible stores that ignore ``If-None-Match`` could overwrite an existing
object between HEAD and PUT; the post-write content verification still detects
a mismatch (IntegrityFailure) but cannot undo the overwrite.

Uploads are a single PutObject from a spooled file (streamed from disk, bounded
memory); multipart upload is not implemented, so one object is limited to the
S3 single-PUT maximum of 5 GiB (the store default ``max_bytes`` is 1 GiB).

Missing objects raise ``FileNotFoundError`` from ``get``/``open``/``range_read``,
like the filesystem store. Client settings (``from_settings``): TLS verification
is botocore's default (on; ``AWS_CA_BUNDLE`` selects a private CA), connect/read
timeouts and the total attempt count are bounded, and a blank endpoint/region is
treated as unset.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import time
from collections.abc import Iterator
from typing import Any, BinaryIO

from reva.adapters.objects.common import CHUNK, check_namespace, object_key, spool_and_hash, verify
from reva.kernel.errors import ConfigurationError, IntegrityFailure
from reva.ports.storage import ObjectHead, StoredObject

#: botocore client bounds for object I/O (worst case per call: TOTAL_ATTEMPTS x (connect + read) + backoff).
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 60
TOTAL_ATTEMPTS = 4
#: Reachability probe bounds (``probe``): one attempt, short timeouts.
PROBE_CONNECT_TIMEOUT_S = 2
PROBE_READ_TIMEOUT_S = 3
#: S3 single PutObject limit; larger objects would need multipart upload (not implemented).
MAX_SINGLE_PUT_BYTES = 5 * 1024**3

_NOT_FOUND = ("404", "NoSuchKey", "NotFound")
_FORBIDDEN = ("403", "AccessDenied", "Forbidden")
_CONDITIONAL = ("PreconditionFailed", "ConditionalRequestConflict", "412", "409")


def _code(e: Any) -> str:
    return str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _client_config(settings: Any, *, connect: float, read: float, attempts: int) -> Any:
    from botocore.config import Config

    addressing = _blank_to_none(getattr(settings, "reva_s3_addressing_style", None)) or "auto"
    if addressing not in ("auto", "path", "virtual"):
        raise ConfigurationError("REVA_S3_ADDRESSING_STYLE must be auto, path or virtual")
    return Config(
        connect_timeout=connect,
        read_timeout=read,
        retries={"total_max_attempts": attempts, "mode": "standard"},
        s3={"addressing_style": addressing},
    )


class S3ArtifactStore:
    def __init__(
        self,
        client: Any,
        bucket: str,
        prefix: str = "reva/",
        spool_dir: str | None = None,
        default_max_bytes: int = 1 << 30,
        probe_client: Any | None = None,
    ):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix
        self.spool_dir = spool_dir
        self.default_max_bytes = default_max_bytes
        self.probe_client = probe_client

    @classmethod
    def from_settings(cls, settings: Any) -> S3ArtifactStore:
        import boto3

        endpoint = _blank_to_none(settings.reva_s3_endpoint_url)
        region = _blank_to_none(settings.reva_s3_region)
        bucket = _blank_to_none(settings.reva_s3_bucket)
        if endpoint is not None and not endpoint.startswith(("https://", "http://")):
            raise ConfigurationError("REVA_S3_ENDPOINT_URL must be an http(s) URL")
        if endpoint is not None and endpoint.startswith("http://") and settings.reva_env == "production":
            raise ConfigurationError("REVA_S3_ENDPOINT_URL must use https in production")
        if bucket is None:
            raise ConfigurationError("REVA_S3_BUCKET is required for the s3 object backend")
        key_id, secret = settings.reva_s3_access_key_id, settings.reva_s3_secret_access_key
        if key_id is None or secret is None:
            # Never fall back to the ambient AWS credential chain: credentials are explicit.
            raise ConfigurationError("REVA_S3_ACCESS_KEY_ID and REVA_S3_SECRET_ACCESS_KEY are required")
        session = boto3.session.Session(
            aws_access_key_id=key_id.get_secret_value(),
            aws_secret_access_key=secret.get_secret_value(),
            region_name=region,
        )

        def make(config: Any) -> Any:
            # verify is left to botocore: TLS verification on, AWS_CA_BUNDLE honoured.
            return session.client("s3", endpoint_url=endpoint, config=config)

        client = make(_client_config(settings, connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, attempts=TOTAL_ATTEMPTS))
        probe = make(_client_config(settings, connect=PROBE_CONNECT_TIMEOUT_S, read=PROBE_READ_TIMEOUT_S, attempts=1))
        return cls(client, bucket, settings.reva_s3_prefix, probe_client=probe)

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    # ------------------------------------------------------------------ write
    def put(self, stream: BinaryIO, namespace: str, media_type: str, *, max_bytes: int | None = None) -> StoredObject:
        check_namespace(namespace)
        limit = min(max_bytes or self.default_max_bytes, MAX_SINGLE_PUT_BYTES)
        tmp, sha, size = spool_and_hash(stream, self.spool_dir, limit)
        key = object_key(self.prefix, namespace, sha)
        try:
            # A 403 on HEAD of a missing key is what AWS returns when the caller's
            # s3:ListBucket is prefix-conditioned; the conditional PUT decides then.
            try:
                existing = self._head_key(key)
            except self.client.exceptions.ClientError as e:
                if _code(e) not in _FORBIDDEN:
                    raise
                existing = None
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
                    except self.client.exceptions.ClientError as e:
                        if _code(e) not in _CONDITIONAL:
                            raise
                existing = self._head_key(key)
                # A concurrent conditional write (409 ConditionalRequestConflict) may still be
                # completing: give it a short, bounded time to become visible.
                delay = 0.1
                while existing is None and not uploaded and delay <= 1.6:
                    time.sleep(delay)
                    delay *= 2
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
        except Exception:
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

    # ------------------------------------------------------------------ read
    def _head_key(self, key: str) -> ObjectHead | None:
        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.ClientError as e:
            if _code(e) in _NOT_FOUND:
                return None
            raise
        meta_sha = (r.get("Metadata") or {}).get("reva-sha256", "")
        return ObjectHead(meta_sha, int(r["ContentLength"]), self.uri(key))

    def head(self, namespace: str, sha256: str) -> ObjectHead | None:
        return self._head_key(object_key(self.prefix, namespace, sha256))

    def _get_object(self, namespace: str, sha256: str, **kw: Any) -> Any:
        key = object_key(self.prefix, namespace, sha256)
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key, **kw)
        except self.client.exceptions.ClientError as e:
            if _code(e) in _NOT_FOUND:
                raise FileNotFoundError(f"object {namespace}/{sha256} not found") from None
            raise

    def get(self, namespace: str, sha256: str) -> bytes:
        r = self._get_object(namespace, sha256)
        data = r["Body"].read()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise IntegrityFailure("stored object bytes do not match their address", sha256=sha256)
        return bytes(data)

    def open(self, namespace: str, sha256: str) -> Iterator[bytes]:
        """Stream the object in chunks; raises IntegrityFailure after the last chunk on a mismatch."""
        r = self._get_object(namespace, sha256)
        body = r["Body"]
        h = hashlib.sha256()
        try:
            for chunk in body.iter_chunks(CHUNK):
                h.update(chunk)
                yield chunk
        finally:
            body.close()
        if h.hexdigest() != sha256:
            raise IntegrityFailure("stored object bytes do not match their address", sha256=sha256)

    def range_read(self, namespace: str, sha256: str, start: int, length: int) -> bytes:
        if start < 0 or length < 0:
            raise ValueError("invalid range")
        if length == 0:
            if self.head(namespace, sha256) is None:
                raise FileNotFoundError(f"object {namespace}/{sha256} not found")
            return b""
        try:
            r = self._get_object(namespace, sha256, Range=f"bytes={start}-{start + length - 1}")
        except self.client.exceptions.ClientError as e:
            if _code(e) != "InvalidRange":
                raise
            return b""  # start at/after the end of the object (filesystem semantics: empty read)
        return bytes(r["Body"].read())

    def list_uris(self, namespace: str) -> Iterator[tuple[str, float]]:
        check_namespace(namespace)
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
            if _code(e) in _NOT_FOUND:
                return None
            raise
        return float(r["LastModified"].timestamp())

    def delete_orphan(self, storage_uri: str) -> None:
        prefix = f"s3://{self.bucket}/{self.prefix}"
        if not storage_uri.startswith(prefix):
            raise ValueError("refusing to delete outside the configured prefix")
        self.client.delete_object(Bucket=self.bucket, Key=storage_uri.removeprefix(f"s3://{self.bucket}/"))

    # ------------------------------------------------------------------ health
    def probe(self) -> dict[str, Any]:
        """Cheap, non-fatal reachability/authorisation check for readiness endpoints.

        One ListObjectsV2 (MaxKeys=1) under the configured prefix — allowed by both the
        read-only API and the worker policy — on a client with short timeouts and a
        single attempt. Never raises; the result carries no bucket, key or credential.
        """
        client = self.probe_client or self.client
        t0 = time.monotonic()
        error: str | None = None
        try:
            client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)
        except Exception as e:  # a probe reports, it never fails the caller
            error = _code(e) or type(e).__name__
        return {
            "backend": "s3",
            "status": "ok" if error is None else "unavailable",
            "error": error,
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        }
