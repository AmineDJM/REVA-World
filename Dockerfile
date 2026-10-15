# REVA World application image (API and worker share it; render.yaml selects the command).
#
# Stages:
#   ui       locked Node build of ui/ (npm ci + npm run build) -> src/reva/api/static
#   builder  locked Python dependencies (uv sync --frozen --no-dev) into /opt/venv
#   runtime  slim, non-root, no compilers/build tools; venv + sources + static UI
#
# Base images are pinned by multi-arch index digest (resolved 2026-10-14 with
# `docker pull` + `docker inspect --format '{{index .RepoDigests 0}}'`). Refresh
# them deliberately (see docs/operations.md) and rebuild; never use moving tags alone.
#
# The release build requires ui/package-lock.json and a working `npm run build`;
# there is no fallback that ships an image without the UI.

# node:22-bookworm-slim (Node 22.23.3)
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS ui
WORKDIR /build/ui
ENV NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false NPM_CONFIG_AUDIT=false
COPY ui/package.json ui/package-lock.json ./
# Optional build-time CA (only for builds behind a TLS-intercepting proxy):
#   docker build --secret id=build_ca,src=/path/to/ca-bundle.crt .
# The secret is mounted for this step only and never stored in a layer.
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    npm ci
COPY ui/ ./
COPY contracts/ /build/contracts/
# vite.config.ts writes to ../src/reva/api/static (served by FastAPI in production).
RUN npm run build && test -f /build/src/reva/api/static/index.html

# ghcr.io/astral-sh/uv:0.11.32 (static uv binary only)
FROM ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

# python:3.12-slim-bookworm (CPython 3.12.15)
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /app
# Dependencies first (cache layer), then the project itself (editable -> /app/src,
# so module-relative paths to registry/, migrations/ and contracts/ resolve under /app).
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --frozen --no-dev
# Runtime hygiene (measured 2026-10-15, docs/dev/docker_check.md): uv leaves a
# world-writable (0666) /opt/venv/.lock, and third-party wheels ship their own
# test suites (~66 MB: pandas/tests alone is 37 MB) that nothing imports at runtime.
RUN rm -f /opt/venv/.lock \
 && find /opt/venv/lib/python3.12/site-packages -depth -type d -name tests \
      -path '*/site-packages/*/tests' -exec rm -rf {} + \
 && test -z "$(find /opt/venv -perm -o+w -not -type l -print -quit)"

# python:3.12-slim-bookworm (CPython 3.12.15) -- same base as builder: the venv's
# interpreter symlink and any compiled wheels match exactly.
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS runtime
ARG REVA_GIT_COMMIT=""
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/reva \
    REVA_GIT_COMMIT=${REVA_GIT_COMMIT}
RUN groupadd --system --gid 10001 reva \
 && useradd --system --uid 10001 --gid reva --home-dir /home/reva --create-home --shell /usr/sbin/nologin reva
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY registry/ ./registry/
COPY contracts/ ./contracts/
COPY scripts/ ./scripts/
COPY --from=ui /build/src/reva/api/static/ ./src/reva/api/static/
# Precompile application bytecode as root (read-only for the runtime user), and
# create the only writable directory: a bounded scratch/cache area.
RUN python -m compileall -q /app/src /app/scripts /app/migrations \
 && mkdir -p /app/var && chown reva:reva /app/var \
 && python -c "import reva, sys; sys.exit(0 if reva.__file__.startswith('/app/src/') else 1)"
USER 10001:10001
# Render supplies the command per service (start_api.py / start_worker.py) and
# runs `python scripts/bootstrap_runtime.py` as the API's preDeployCommand.
# Health checking is Render's healthCheckPath (/health/ready); no HEALTHCHECK here.
CMD ["python", "scripts/start_api.py"]
