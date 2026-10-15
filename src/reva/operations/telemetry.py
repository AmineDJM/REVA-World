"""Structured JSON logs, secret redaction and a bounded Prometheus registry.

Logs: one JSON object per line. Correlation fields are restricted to a fixed
set (request_id, job_id, activity_id, source_id, release_id, run_id), taken
from ``extra=`` or from context variables set by the worker around a job.

Redaction: values of secret environment variables (REVA_*PASSWORD*, *SECRET*,
*KEY*, DATABASE URLs), values registered in-process (Settings secrets read from
``.env``) and passwords embedded in URLs (``scheme://user:pw@``) are replaced
by ``***`` in every formatted message, argument and exception. Shape rules also
mask SQLAlchemy bound parameters, PostgreSQL DETAIL value echoes, credential
headers, secret query parameters, application tokens/cookies and Argon2id
hashes. ``redact_obj`` does the same for structured payloads without breaking
their JSON shape.

Metrics: a deliberately small, dependency-free text-exposition registry. Label
names are fixed per metric and limited to {job_type, status, connector}; label
values are bounded (job types from a closed set, at most ``MAX_LABEL_VALUES``
distinct values per label, overflow collapses to ``other``). Entity IDs,
documents, URLs and free text never become labels.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

CORRELATION_FIELDS: tuple[str, ...] = ("request_id", "job_id", "activity_id", "source_id", "release_id", "run_id")
ALLOWED_LABELS = frozenset({"job_type", "status", "connector"})
MAX_LABEL_VALUES = 32
MAX_MESSAGE_CHARS = 4000

_correlation: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "reva_log_correlation", default=None
)


# --------------------------------------------------------------------------- redaction
_SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|(^|_)KEY(_|$)|KEY_ID|DATABASE_URL|_DSN$")
_URL_PASSWORD = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^:/@\s]*):(?P<pw>[^@/\s]+)@")
_KV_SECRET = re.compile(
    r"(?P<k>\b[A-Za-z0-9_]*(?:password|secret|api_key|apikey|token)[A-Za-z0-9_]*\b\s*[=:]\s*)(?P<v>[^\s,;'\"]+)",
    re.IGNORECASE,
)


# Shape-based rules (independent of which secrets this process knows by value). They apply to
# plain text (messages, tracebacks, error strings); structured payloads go through redact_obj.
#: SQLAlchemy appends the bound parameters to exception text ("[parameters: {...}]", one line).
_SQL_PARAMETERS = re.compile(r"\[parameters: [^\n]*")
#: PostgreSQL DETAIL/CONTEXT lines that echo row, key or input values.
_PG_VALUE_ECHO = (
    (re.compile(r"(Key \((?:[^()\n]|\([^()\n]*\))*\)=)\([^\n]*"), r"\1(***)"),
    (re.compile(r"(Failing row contains )\([^\n]*"), r"\1(***)"),
    (re.compile(r"(invalid input (?:syntax|value) for [^:\n]*: )\"[^\"\n]*\""), r'\1"***"'),
    (re.compile(r"(JSON data, line \d+: )[^\n]*"), r"\1***"),
    (re.compile(r"(Token )\"[^\"\n]*\"( is invalid)"), r'\1"***"\2'),
)
#: Credential-bearing HTTP headers rendered as text (``Authorization: Bearer x``, ``'cookie': '...'``).
_AUTH_HEADER = re.compile(
    r"(?i)\b(?P<k>(?:proxy-)?authorization|set-cookie|cookie|x-api-key|ocp-apim-subscription-key|x-amz-security-token)"
    r"(?P<sep>['\"]?\s*[:=]\s*['\"]?)(?P<v>[^'\"\n]+)"
)
_BEARER = re.compile(r"(?i)\b(?P<k>bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
#: Secret query parameters in URLs (provider keys, presigned S3 signatures/credentials).
_QUERY_SECRET = re.compile(
    r"(?i)(?P<k>[?&](?:subscription-key|api[_-]?key|apikey|key|token|access_token|secret|password|signature|sig"
    r"|x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&\s#'\"]+"
)
#: Cookies/tokens issued by this application and Argon2id hashes (REVA_ADMIN_PASSWORD_HASH).
_APP_SECRET_SHAPES = (
    (re.compile(r"(?P<k>\breva_(?:session|csrf)=)[^;\s,'\"]+"), r"\g<k>***"),
    (re.compile(r"\breva_[A-Za-z0-9_\-]{40,}"), "reva_***"),
    (re.compile(r"\$argon2id\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+"), "$argon2id$***"),
)
#: Mapping keys whose string values are always secret (structured payloads: audit, outbox, job errors).
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|passphrase|secret|api[_-]?key|apikey|authorization|cookie|private[_-]?key"
    r"|access[_-]?key|credential|^token$|[_-]token$|token[_-]?(sha256|hash)$|^dsn$|database_url)"
)
_MIN_REGISTERED = 6
_MAX_REGISTERED = 256
_registered: dict[str, None] = {}  # insertion-ordered, bounded
_registry_version = 0
_registry_lock = threading.Lock()


def is_secret_env_name(name: str) -> bool:
    return bool(_SECRET_NAME.search(name.upper()))


def register_secret_values(values: Iterable[str | None]) -> None:
    """Remember secret values known only in-process (e.g. read from ``.env`` by Settings, never exported to
    ``os.environ``) so every redaction pass masks them by value. Bounded; short values are ignored."""
    global _registry_version
    with _registry_lock:
        changed = False
        for v in values:
            if v and len(v) >= _MIN_REGISTERED and v not in _registered:
                _registered[v] = None
                changed = True
                while len(_registered) > _MAX_REGISTERED:
                    _registered.pop(next(iter(_registered)))
        if changed:
            _registry_version += 1


def secret_values(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    vals = {v for k, v in env.items() if v and len(v) >= 4 and is_secret_env_name(k)}
    for k, v in env.items():  # passwords embedded in any DSN-like value
        if v and "://" in v:
            m = _URL_PASSWORD.search(v)
            if m and len(m.group("pw")) >= 4:
                vals.add(m.group("pw"))
        _ = k
    with _registry_lock:
        vals.update(_registered)
    return sorted(vals, key=len, reverse=True)


def redact_text(s: str, secrets: Iterable[str] = ()) -> str:
    """Mask secrets in free text: known values, URL passwords, ``key=value`` secrets, SQL bound parameters,
    PostgreSQL value echoes, credential headers, secret query parameters and application token shapes."""
    for v in secrets:
        if v and v in s:
            s = s.replace(v, "***")
    s = _URL_PASSWORD.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:***@", s)
    s = _SQL_PARAMETERS.sub("[parameters: ***]", s)
    for rx, repl in _PG_VALUE_ECHO:
        s = rx.sub(repl, s)
    s = _AUTH_HEADER.sub(lambda m: f"{m.group('k')}{m.group('sep')}***", s)
    s = _BEARER.sub(lambda m: f"{m.group('k')} ***", s)
    s = _QUERY_SECRET.sub(lambda m: f"{m.group('k')}***", s)
    for rx, repl in _APP_SECRET_SHAPES:
        s = rx.sub(repl, s)
    s = _KV_SECRET.sub(lambda m: f"{m.group('k')}***", s)
    return s


def redact_obj(obj: Any, secrets: Iterable[str] | None = None, *, _depth: int = 0) -> Any:
    """Structure-preserving redaction for JSON-like payloads (audit/outbox/job errors): string values under
    secret-named keys become ``***``; every other string goes through :func:`redact_text`."""
    known = list(secret_values() if secrets is None else secrets)
    if _depth > 32:
        return "***"
    if isinstance(obj, str):
        return redact_text(obj, known)
    if isinstance(obj, Mapping):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and _SECRET_KEY.search(str(k)):
                out[k] = "***"
            else:
                out[k] = redact_obj(v, known, _depth=_depth + 1)
        return out
    if isinstance(obj, list | tuple):
        return [redact_obj(v, known, _depth=_depth + 1) for v in obj]
    return obj


class RedactionFilter(logging.Filter):
    """Rewrites msg/args so no handler ever sees a secret value."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self.environ = environ
        self._version = -1
        self.refresh()

    def refresh(self) -> None:
        self._version = _registry_version
        self._secrets = secret_values(self.environ)

    def filter(self, record: logging.LogRecord) -> bool:
        if self._version != _registry_version:
            self.refresh()
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact_text(msg, self._secrets)[:MAX_MESSAGE_CHARS]
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text, self._secrets)
        record.exc_info = None if record.exc_text else record.exc_info
        for f in CORRELATION_FIELDS:
            v = getattr(record, f, None)
            if v is not None:
                setattr(record, f, redact_text(str(v), self._secrets)[:128])
        return True


# --------------------------------------------------------------------------- JSON logs
@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind correlation fields (only the allowed names) for the current context."""
    current = dict(_correlation.get() or {})
    current.update({k: str(v) for k, v in fields.items() if k in CORRELATION_FIELDS and v is not None})
    token = _correlation.set(current)
    try:
        yield
    finally:
        _correlation.reset(token)


def current_correlation() -> dict[str, str]:
    return dict(_correlation.get() or {})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage()[:MAX_MESSAGE_CHARS],
        }
        corr = current_correlation()
        for f in CORRELATION_FIELDS:
            v = getattr(record, f, None) or corr.get(f)
            if v is not None:
                out[f] = str(v)[:128]
        event = getattr(record, "event", None)
        if isinstance(event, str):
            out["event"] = event[:64]
        if record.exc_text:
            out["exc"] = record.exc_text[-MAX_MESSAGE_CHARS:]
        elif record.exc_info:
            out["exc"] = self.formatException(record.exc_info)[-MAX_MESSAGE_CHARS:]
        return json.dumps(out, default=str, ensure_ascii=False)


#: Server loggers that install their own (unredacted, plain-text) handlers; configure_logging routes them
#: through the redacting JSON handler instead (uvicorn logs "Exception in ASGI application" tracebacks,
#: and access lines with full query strings).
ADOPTED_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access")
#: Libraries whose DEBUG/INFO output carries request URLs, headers or SQL parameters: never below WARNING,
#: whatever REVA_LOG_LEVEL says (httpx logs URLs with query-string keys at INFO; botocore logs signed
#: request headers at DEBUG; sqlalchemy.engine logs statements with bound parameters at INFO).
QUIET_LOGGERS: tuple[str, ...] = ("httpx", "httpcore", "botocore", "boto3", "s3transfer", "urllib3", "sqlalchemy")


def configure_logging(level: str = "INFO", *, stream: Any = None) -> logging.Handler:
    """Install a single JSON handler with redaction on the root logger (idempotent).

    Also adopts the server loggers (their own handlers are removed, records propagate to the redacting
    handler) and keeps credential-bearing library loggers at WARNING or above.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_reva_json", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    handler._reva_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ADOPTED_LOGGERS:
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True
    for name in QUIET_LOGGERS:
        lg = logging.getLogger(name)
        if lg.getEffectiveLevel() < logging.WARNING or lg.level == logging.NOTSET:
            lg.setLevel(logging.WARNING)
    return handler


# --------------------------------------------------------------------------- metrics
def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class _Metric:
    kind = "untyped"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> None:
        bad = set(labels) - ALLOWED_LABELS
        if bad:
            raise ValueError(f"label(s) {sorted(bad)} are not allowed (bounded cardinality)")
        if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", name):
            raise ValueError(f"invalid metric name {name}")
        self.name = name
        self.help = help_text
        self.labels = labels
        self._lock = threading.Lock()
        self._seen: dict[str, set[str]] = {k: set() for k in labels}

    def _key(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        if set(labels) != set(self.labels):
            raise ValueError(f"{self.name} expects labels {self.labels}, got {sorted(labels)}")
        out = []
        for k in self.labels:
            v = bound_label(k, str(labels[k]))
            seen = self._seen[k]
            if v not in seen and len(seen) >= MAX_LABEL_VALUES:
                v = "other"
            seen.add(v)
            out.append(v)
        return tuple(out)

    def _fmt_labels(self, key: tuple[str, ...], extra: dict[str, str] | None = None) -> str:
        pairs = [f'{k}="{_escape(v)}"' for k, v in zip(self.labels, key, strict=True)]
        pairs += [f'{k}="{_escape(v)}"' for k, v in (extra or {}).items()]
        return "{" + ",".join(pairs) + "}" if pairs else ""

    def header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    kind = "counter"

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("counters only increase")
        with self._lock:
            key = self._key(labels)
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(tuple(bound_label(k, str(labels[k])) for k in self.labels), 0.0)

    def render(self) -> list[str]:
        with self._lock:
            return self.header() + [f"{self.name}{self._fmt_labels(k)} {v:g}" for k, v in sorted(self._values.items())]


class Gauge(_Metric):
    kind = "gauge"

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        with self._lock:
            self._values[self._key(labels)] = float(value)

    def value(self, **labels: str) -> float | None:
        with self._lock:
            return self._values.get(tuple(bound_label(k, str(labels[k])) for k in self.labels))

    def render(self) -> list[str]:
        with self._lock:
            return self.header() + [f"{self.name}{self._fmt_labels(k)} {v:g}" for k, v in sorted(self._values.items())]


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...] = (),
                 buckets: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 1800)) -> None:
        super().__init__(name, help_text, labels)
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sum: dict[tuple[str, ...], float] = {}
        self._n: dict[tuple[str, ...], int] = {}

    def observe(self, value: float, **labels: str) -> None:
        with self._lock:
            key = self._key(labels)
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for i, b in enumerate(self.buckets):
                if value <= b:
                    counts[i] += 1
            self._sum[key] = self._sum.get(key, 0.0) + value
            self._n[key] = self._n.get(key, 0) + 1

    def count(self, **labels: str) -> int:
        with self._lock:
            return self._n.get(tuple(bound_label(k, str(labels[k])) for k in self.labels), 0)

    def render(self) -> list[str]:
        lines = self.header()
        with self._lock:
            for key in sorted(self._n):
                for b, c in zip(self.buckets, self._counts[key], strict=True):
                    lines.append(f"{self.name}_bucket{self._fmt_labels(key, {'le': f'{b:g}'})} {c}")
                lines.append(f"{self.name}_bucket{self._fmt_labels(key, {'le': '+Inf'})} {self._n[key]}")
                lines.append(f"{self.name}_sum{self._fmt_labels(key)} {self._sum[key]:g}")
                lines.append(f"{self.name}_count{self._fmt_labels(key)} {self._n[key]}")
        return lines


KNOWN_JOB_TYPES = frozenset({
    "ingest", "publish_beliefs", "simulation", "replay", "discovery", "gaps", "health", "gc", "evaluation",
    "population",
})
KNOWN_STATUSES = frozenset({
    "queued", "running", "succeeded", "failed", "cancel_requested", "cancelled", "dead", "retry", "released",
    "ok", "error", "published", "unchanged", "quarantined", "lost_lease", "2xx", "3xx", "4xx", "5xx",
})


def bound_label(name: str, value: str) -> str:
    """Map a label value onto a closed/bounded vocabulary."""
    if name == "job_type":
        return value if value in KNOWN_JOB_TYPES else "other"
    if name == "status":
        return value if value in KNOWN_STATUSES else "other"
    if name == "connector":  # connector names are a reviewed, small registry
        return value if re.fullmatch(r"[a-z0-9_.\-]{1,64}", value) else "other"
    return "other"


class Registry:
    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def _add(self, m: _Metric) -> Any:
        with self._lock:
            existing = self._metrics.get(m.name)
            if existing is not None:
                return existing
            self._metrics[m.name] = m
            return m

    def counter(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Counter:
        return self._add(Counter(name, help_text, labels))  # type: ignore[no-any-return]

    def gauge(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Gauge:
        return self._add(Gauge(name, help_text, labels))  # type: ignore[no-any-return]

    def histogram(self, name: str, help_text: str, labels: tuple[str, ...] = (), **kw: Any) -> Histogram:
        return self._add(Histogram(name, help_text, labels, **kw))  # type: ignore[no-any-return]

    def render(self) -> str:
        with self._lock:
            metrics = list(self._metrics.values())
        lines: list[str] = []
        for m in metrics:
            lines.extend(m.render())  # type: ignore[attr-defined]
        return "\n".join(lines) + "\n"


REGISTRY = Registry()
JOBS_CLAIMED = REGISTRY.counter("reva_jobs_claimed_total", "Jobs claimed by this worker", ("job_type",))
JOBS_COMPLETED = REGISTRY.counter("reva_jobs_completed_total", "Jobs finished by status", ("job_type", "status"))
JOBS_FAILED = REGISTRY.counter("reva_jobs_failed_total", "Job attempts that failed", ("job_type", "status"))
LEASE_EXPIRIES = REGISTRY.counter("reva_job_lease_expiries_total", "Expired leases re-queued or dead-lettered")
QUEUE_OLDEST_AGE = REGISTRY.gauge("reva_queue_oldest_age_seconds", "Age of the oldest available queued job",
                                  ("job_type",))
QUEUE_DEPTH = REGISTRY.gauge("reva_queue_depth", "Jobs per status", ("status",))
QUEUE_ACK = REGISTRY.histogram("reva_queue_ack_seconds", "Claim to handler start latency", ("job_type",),
                               buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
JOB_DURATION = REGISTRY.histogram("reva_job_duration_seconds", "Handler wall time", ("job_type", "status"))
INGEST_REQUESTS = REGISTRY.counter("reva_ingestion_requests_total", "Connector items by outcome",
                                   ("connector", "status"))
INGEST_BYTES = REGISTRY.counter("reva_ingestion_bytes_total", "Raw bytes acquired", ("connector",))
SIM_DURATION = REGISTRY.histogram("reva_simulation_duration_seconds", "Simulation job wall time", ("status",))
SIM_RSS = REGISTRY.gauge("reva_simulation_max_rss_bytes", "Peak worker RSS observed after a simulation job")
IDEMPOTENCY_CONFLICTS = REGISTRY.counter(
    "reva_idempotency_conflicts_total",
    "Internal enqueues whose idempotency key was held by a foreign job (created under a disambiguated key)",
    ("job_type",))
OUTBOX_EVENT_FAILURES = REGISTRY.counter("reva_outbox_event_failures_total",
                                         "Outbox events whose processing failed (isolated, retried with backoff)")
OUTBOX_FAILING_EVENTS = REGISTRY.gauge("reva_outbox_failing_events",
                                       "Unprocessed outbox events with at least one recorded failure")


def max_rss_bytes() -> int:
    """Peak resident set size of this process (Linux ru_maxrss is KiB)."""
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def collect_queue_metrics(engine: Any) -> dict[str, Any]:
    """Refresh queue gauges from the database (usable from the API /metrics)."""
    from sqlalchemy import text

    with engine.connect() as conn:
        depth = conn.execute(text("SELECT status, count(*) FROM ops.job GROUP BY status")).all()
        ages = conn.execute(
            text(
                "SELECT job_type, EXTRACT(EPOCH FROM clock_timestamp() - min(available_at)) FROM ops.job "
                "WHERE status = 'queued' AND available_at <= clock_timestamp() GROUP BY job_type"
            )
        ).all()
    for st in KNOWN_STATUSES & {"queued", "running", "succeeded", "failed", "cancel_requested", "cancelled", "dead"}:
        QUEUE_DEPTH.set(0, status=st)
    for st, n in depth:
        QUEUE_DEPTH.set(n, status=st)
    for jt in KNOWN_JOB_TYPES:
        QUEUE_OLDEST_AGE.set(0, job_type=jt)
    for jt, age in ages:
        QUEUE_OLDEST_AGE.set(float(age or 0), job_type=jt)
    return {"depth": {s: n for s, n in depth}, "oldest_age_seconds": {j: float(a or 0) for j, a in ages}}


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.t0
