"""Index lifecycle for V8 Liens (agent V8LIENS): one shared :class:`PathIndex` per API process.

* First use: the on-disk cache (``var/v8/paths/index-<data key>.npz``) is loaded when its data key matches the
  database (≈0.4 s); otherwise the index is built in a background thread on a dedicated connection (≈20 s on the
  dev DB) and the request waits up to ``wait_s`` (then 503 ``paths_index_building``, retry).
* Freshness: every ``CHECK_EVERY_S`` the data key (max row ids, retraction counts, registry signature) is
  re-read; a change starts a background rebuild and the previous index keeps serving, flagged ``stale``.

CLI (prebuild the cache): ``uv run python -m reva.v8.paths.service build``.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from reva.v8.paths import index as ix

log = logging.getLogger(__name__)

CHECK_EVERY_S = 300.0
REPO_ROOT = Path(__file__).resolve().parents[4]


def cache_dir() -> Path | None:
    """``REVA_V8_PATHS_CACHE`` (``off`` disables the disk cache), else ``var/v8/paths`` in the repository."""
    v = os.environ.get("REVA_V8_PATHS_CACHE")
    if v == "off":
        return None
    return Path(v) if v else REPO_ROOT / "var" / "v8" / "paths"


class IndexBuilding(RuntimeError):
    pass


class IndexHolder:
    def __init__(self) -> None:
        self._idx: ix.PathIndex | None = None
        self._cond = threading.Condition()
        self._building = False
        self._error: str | None = None
        self._last_check = 0.0
        self._current_key: str | None = None
        self._registries: ix.Registries | None = None
        self._reg_at = 0.0
        self._warm_started = False

    # ------------------------------------------------------------------------------------------ state
    def status(self) -> dict[str, Any]:
        with self._cond:
            idx = self._idx
            return {
                "ready": idx is not None,
                "building": self._building,
                "stale": bool(idx is not None and self._current_key and idx.meta.get("data_key") != self._current_key),
                "error": self._error,
                "index": idx.summary() if idx is not None else None,
            }

    def set(self, idx: ix.PathIndex) -> None:
        """Install an index (tests, CLI)."""
        with self._cond:
            self._idx = idx
            self._current_key = idx.meta.get("data_key")
            self._last_check = time.monotonic()
            self._cond.notify_all()

    def reset(self) -> None:
        with self._cond:
            self._idx = None
            self._building = False
            self._error = None
            self._current_key = None
            self._last_check = 0.0

    def _regs(self) -> ix.Registries:
        if self._registries is None or time.monotonic() - self._reg_at > CHECK_EVERY_S:
            self._registries = ix.load_registries()
            self._reg_at = time.monotonic()
        return self._registries

    # ------------------------------------------------------------------------------------------ access
    def get(self, conn: Any, connect: Callable[[], Any], *, wait_s: float = 25.0) -> ix.PathIndex:
        """The current index. ``conn``: the request's read connection (key checks); ``connect``: opens a dedicated
        connection context for a background build."""
        now = time.monotonic()
        with self._cond:
            idx = self._idx
            due = idx is None or now - self._last_check > CHECK_EVERY_S
        if due and not self._building:
            reg = self._regs()
            key = ix.data_key(conn, reg)
            with self._cond:
                self._last_check = now
                self._current_key = key
                idx = self._idx
            if idx is None:
                idx = self._from_cache(key)
                if idx is not None:
                    self.set(idx)
            if idx is None or idx.meta.get("data_key") != key:
                self._start_build(connect, reg)
        with self._cond:
            if self._idx is None:
                self._cond.wait_for(lambda: self._idx is not None or not self._building, timeout=wait_s)
            if self._idx is None:
                raise IndexBuilding(self._error or "the path index is being built")
            idx = self._idx
            start_warm = not self._warm_started
            self._warm_started = True
        if start_warm:
            threading.Thread(target=_warm_renderers, name="v8-paths-warm", daemon=True).start()
        return idx

    def _from_cache(self, key: str) -> ix.PathIndex | None:
        d = cache_dir()
        if d is None:
            return None
        path = d / f"index-{key}.npz"
        if not path.exists():
            return None
        try:
            idx = ix.load(path)
        except Exception as e:  # a corrupt cache is rebuilt, never served
            log.warning("v8 paths: cache %s unreadable (%s)", path, e)
            return None
        return idx if idx.meta.get("format") == ix.FORMAT else None

    def _start_build(self, connect: Callable[[], Any], reg: ix.Registries) -> None:
        with self._cond:
            if self._building:
                return
            self._building = True
            self._error = None

        def run() -> None:
            try:
                with connect() as conn, conn.begin():
                    idx = ix.build_from_db(conn, reg)
                d = cache_dir()
                if d is not None:
                    try:
                        ix.save(idx, d)
                    except OSError as e:
                        log.warning("v8 paths: cache not written (%s)", e)
                with self._cond:
                    self._idx = idx
                    self._building = False
                    self._cond.notify_all()
                log.info("v8 paths: index built %s", idx.summary())
            except Exception as e:  # pragma: no cover - reported through status and 503
                log.exception("v8 paths: index build failed")
                with self._cond:
                    self._building = False
                    self._error = f"index build failed: {type(e).__name__}: {str(e)[:300]}"
                    self._cond.notify_all()

        threading.Thread(target=run, name="v8-paths-index", daemon=True).start()


def _warm_renderers() -> None:
    """Load the registries the renderer reads (effect labels need the atlas catalog: ~10 s cold) once, in the
    background, so that the first visitor does not pay for them."""
    try:
        from reva.v8.paths import render

        render.warm()
    except Exception:  # pragma: no cover - warming is an optimisation only
        log.warning("v8 paths: renderer warm-up failed", exc_info=True)


HOLDER = IndexHolder()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["build"]:
        print("usage: python -m reva.v8.paths.service build", file=sys.stderr)
        return 2
    from reva.adapters.postgres.engine import make_engine

    eng = make_engine("api")
    with eng.connect() as conn, conn.begin():
        idx = ix.build_from_db(conn)
    d = cache_dir()
    path = ix.save(idx, d) if d is not None else None
    print({"cache": str(path) if path else None, **idx.summary()})
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
