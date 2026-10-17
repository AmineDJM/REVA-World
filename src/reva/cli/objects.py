"""``reva objects``: maintenance of the filesystem object archive (V6 DISKZ).

* ``reva objects compress [--dry-run] [--min-saving-pct N]`` — in-place, verified, resumable zstd
  compression of existing objects (``reva.adapters.objects.compress_migrate``); object ids stay the
  sha256 of the raw bytes; reads decode transparently and re-verify.
* ``reva objects verify`` — decode + re-hash every object against its address.

Neither command touches the database.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import typer

objects_app = typer.Typer(no_args_is_help=True, help="V6 object archive maintenance: zstd compression, verification")


def _root(root: Path | None) -> tuple[Path, str]:
    from reva.settings import get_settings

    s = get_settings()
    if root is None:
        if s.reva_object_backend != "filesystem":
            typer.echo("the configured object backend is not the filesystem; pass --root", err=True)
            raise typer.Exit(2)
        root = Path(s.reva_object_root)
    return root.resolve(), s.reva_s3_prefix


def _namespaces(ns: list[str] | None) -> tuple[str, ...]:
    from reva.ports.storage import NAMESPACES

    if not ns:
        return NAMESPACES
    bad = [n for n in ns if n not in NAMESPACES]
    if bad:
        typer.echo(f"unknown namespace(s): {bad}", err=True)
        raise typer.Exit(2)
    return tuple(ns)


@objects_app.command("compress")
def objects_compress(
    dry_run: bool = typer.Option(False, "--dry-run", help="measure only: compress into a counter, change nothing"),
    min_saving_pct: float = typer.Option(10.0, "--min-saving-pct", help="keep the compressed form only if it saves N%"),
    root: Path | None = typer.Option(
        None, "--root", help="object root (default: REVA_OBJECT_ROOT), e.g. var/kg/objects"
    ),
    namespace: list[str] | None = typer.Option(None, "--namespace", help="restrict to these namespaces (repeatable)"),
    level: int = typer.Option(9, "--level", min=1, max=19, help="zstd level"),
    min_age_minutes: float = typer.Option(
        60.0, "--min-age-minutes", help="skip objects modified in the last N minutes (running writers may re-read them)"
    ),
    limit: int | None = typer.Option(None, "--limit", help="stop after N objects (were/would be) compressed"),
    log: Path | None = typer.Option(None, "--log", help="append one JSON line per compressed/corrupt object"),
) -> None:
    """Compress existing objects in place, file by file (atomic replace after decode+sha256 verification)."""
    from reva.adapters.objects.compress_migrate import CompressReport, compress_tree

    r, prefix = _root(root)

    def progress(rep: CompressReport) -> None:
        d = rep.as_dict()
        typer.echo(
            f"[objects compress] files={d['files']} compressed={d['statuses'].get('compressed', 0)}"
            f" would={d['statuses'].get('would_compress', 0)} freed={d['gb_freed']} GB {d['seconds']}s",
            err=True,
        )

    fh = log.open("a") if log is not None else None
    try:
        rep = compress_tree(
            r,
            prefix,
            namespaces=_namespaces(namespace),
            level=level,
            min_saving_pct=min_saving_pct,
            min_age_s=min_age_minutes * 60.0,
            dry_run=dry_run,
            limit=limit,
            log=fh,
            progress=progress,
        )
    finally:
        if fh is not None:
            fh.close()
    du = shutil.disk_usage(r)
    out = {**rep.as_dict(), "disk_free_gb": round(du.free / 1e9, 2)}
    typer.echo(json.dumps(out, indent=2))
    if rep.corrupt or rep.errors:
        sys.exit(1)


@objects_app.command("verify")
def objects_verify(
    root: Path | None = typer.Option(None, "--root", help="object root (default: REVA_OBJECT_ROOT)"),
    namespace: list[str] | None = typer.Option(None, "--namespace", help="restrict to these namespaces (repeatable)"),
) -> None:
    """Decode and re-hash every object (raw or compressed); exit 1 if any does not match its address."""
    from reva.adapters.objects.compress_migrate import verify_tree

    r, prefix = _root(root)
    res = verify_tree(r, prefix, namespaces=_namespaces(namespace))
    typer.echo(json.dumps(res, indent=2))
    if not res["ok"]:
        sys.exit(1)
