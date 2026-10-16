"""The atlas local cache directory (per-snapshot DuckDB query files, parsed catalogs).

Review V3 ATLAS-1: the cache must not live in a directory other users can write. The default is a
per-user directory under the system temp dir (``reva-atlas-cache-<uid>``) created with mode 0700;
``REVA_ATLAS_CACHE_DIR`` overrides it (created 0700 if missing). An existing directory that is not
owned by this user, or that group/others can write, is refused (typed ``IntegrityFailure``): the
query layer additionally verifies every cached DuckDB file against a digest written at build time.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from reva.kernel.errors import IntegrityFailure


def private_dir(path: Path) -> Path:
    """``path`` created 0700 if missing; refused when another user owns it or others can write it."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise IntegrityFailure("atlas cache path is not a directory", path=str(path))
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise IntegrityFailure("atlas cache directory is owned by another user", path=str(path))
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise IntegrityFailure("atlas cache directory is writable by group/others (chmod 700)", path=str(path))
    return path


def cache_dir() -> Path:
    env = os.environ.get("REVA_ATLAS_CACHE_DIR")
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return private_dir(Path(env) if env else Path(tempfile.gettempdir()) / f"reva-atlas-cache-{uid}")
