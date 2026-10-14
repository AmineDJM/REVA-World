"""Code and environment digests recorded on every Activity."""

from __future__ import annotations

import hashlib
import os
import platform
import sys
from functools import lru_cache
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG_ROOT.parents[1]


@lru_cache(maxsize=1)
def code_digest() -> str:
    """Git commit if supplied by the build, else a hash of the package sources."""
    commit = os.environ.get("REVA_GIT_COMMIT") or os.environ.get("RENDER_GIT_COMMIT")
    if commit:
        return f"git:{commit}"
    h = hashlib.sha256()
    for p in sorted(PKG_ROOT.rglob("*.py")):
        h.update(str(p.relative_to(PKG_ROOT)).encode())
        h.update(p.read_bytes())
    return f"src-sha256:{h.hexdigest()}"


@lru_cache(maxsize=1)
def environment_digest() -> str:
    h = hashlib.sha256()
    h.update(sys.version.encode())
    h.update(platform.machine().encode())
    lock = REPO_ROOT / "uv.lock"
    if lock.exists():
        h.update(lock.read_bytes())
    image = os.environ.get("REVA_IMAGE_DIGEST", "")
    h.update(image.encode())
    return f"env-sha256:{h.hexdigest()}"
