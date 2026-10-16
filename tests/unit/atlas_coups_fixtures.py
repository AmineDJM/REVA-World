"""Builds the A-COUPS test fixtures from real captures (2026-10-16) as documented, byte-faithful subsets.

Usage (once, by the agent; the outputs are committed under ``tests/fixtures/source/atlas.{coups,archigos,
reign_leaders}/``)::

    uv run python -m tests.unit.atlas_coups_fixtures <capture_dir>

``<capture_dir>`` holds the real downloads: ``coups.html`` (https://jonathanmpowell.com/coups/), ``pt.csv``
(pt_20260829.csv), ``Archigos_4.1_stata14.dta``, ``leaderlist.xlsx``. Subsets:

* coups page: the ``<body>`` text through the archive links + the four data-file blocks; navigation and the
  media-coverage list are dropped (marked with an HTML comment);
* P&T CSV: header + the original lines of the selected states, byte for byte (CRLF kept);
* Archigos: the original ``.dta`` with only the selected rows (:func:`subset_dta`: header N and section map
  rewritten, every other byte copied, rows copied raw -- so non-UTF-8 strings stay as they are);
* leader list: the selected rows rewritten into a minimal workbook keeping the original cell texts, kinds and
  row numbers (:func:`subset_xlsx`).
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import struct
import sys
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "source"
RETRIEVED = "2026-10-16T20:20:00+00:00"
#: Correlates of War codes kept in the fixtures: Cuba, Chile, Argentina, USSR/Russia, South Vietnam, Niger,
#: Turkey, Honduras (a Windows-1252 leader name), Thailand.
STATES = {40, 155, 160, 365, 817, 436, 640, 91, 800}
ARCHIGOS_FROM_YEAR = 1945
PT_URL = "https://jonathanmpowell.com/wp-content/uploads/2026/08/pt_20260829.csv"
PAGE_URL = "https://jonathanmpowell.com/coups/"
DTA_URL = "https://www.rochester.edu/college/faculty/hgoemans/Archigos_4.1_stata14.dta"
LL_URL = "https://jonathanmpowell.com/wp-content/uploads/2026/04/leaderlist.xlsx"
NO_LICENCE = "no licence stated by the provider (citation requested); test fixture subset only"


def subset_dta(body: bytes, rows: Sequence[int]) -> bytes:
    """A Stata 118 (LSF) file with only ``rows`` (0-based), every non-data byte kept."""
    from reva.adapters.connectors.atlas_leaders import read_dta

    fr = read_dta(body)
    assert fr.release == 118 and body[body.index(b"<byteorder>") + 11 :][:3] == b"LSF"
    widths = []
    for t in fr.types:
        widths.append(t if t <= 2045 else 8 if t == 32768 else {65526: 8, 65527: 4, 65528: 4, 65529: 2, 65530: 1}[t])
    width = sum(widths)
    mp = body.index(b"<map>") + 5
    offsets = list(struct.unpack_from("<14Q", body, mp))
    d0 = offsets[9] + len(b"<data>")
    data = b"".join(body[d0 + r * width : d0 + (r + 1) * width] for r in rows)
    old_len = fr.nobs * width
    delta = len(data) - old_len
    new_offsets = offsets[:10] + [o + delta for o in offsets[10:]]
    n_at = body.index(b"<N>") + 3
    out = bytearray(body[:d0]) + data + body[d0 + old_len :]
    struct.pack_into("<Q", out, n_at, len(rows))
    struct.pack_into("<14Q", out, mp, *new_offsets)
    return bytes(out)


def subset_xlsx(body: bytes, sheet: str, keep: Iterable[int]) -> bytes:
    """Minimal workbook with the header row and rows ``keep`` (1-based sheet rows), original texts and kinds."""
    from reva.adapters.connectors.atlas_xlsx import Workbook, col_letters

    wb = Workbook(body)
    keep = set(keep) | {1}
    xml_rows = []
    for rnum, cells in wb.rows(sheet):
        if rnum not in keep:
            continue
        cs = []
        for ci, c in sorted(cells.items()):
            ref = f"{col_letters(ci)}{rnum}"
            if c.kind == "n":
                cs.append(f'<c r="{ref}"><v>{escape(c.text)}</v></c>')
            else:
                cs.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(c.text)}</t></is></c>')
        xml_rows.append(f'<row r="{rnum}">{"".join(cs)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    parts = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
        'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/'
        'workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{ns}" xmlns:r="{rns}"><sheets>'
        f'<sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.'
        'org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{ns}"><sheetData>'
        f"{''.join(xml_rows)}</sheetData></worksheet>",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 16, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text.encode("utf-8"))
    return buf.getvalue()


def subset_csv(body: bytes, keep_lines: Iterable[int]) -> bytes:
    lines = body.split(b"\r\n") if b"\r\n" in body else body.split(b"\n")
    sep = b"\r\n" if b"\r\n" in body else b"\n"
    keep = sorted(set(keep_lines) | {1})
    return sep.join(lines[i - 1] for i in keep) + sep


def trim_page(body: bytes) -> bytes:
    t = body.decode("utf-8")
    b0 = t.index("<body")
    cov = t.index("SELECTED COVERAGE")
    cov = t.rindex("<", 0, cov)
    files = [
        t.rfind('<div class="wp-block-file">', 0, t.index(name)) for name in ("electionlist.xlsx", "regimelist.xlsx")
    ]
    blk_end = t.index("</div>", t.index("regimelist.xlsx")) + len("</div>")
    head = '<!DOCTYPE html>\n<html lang="en-US"><head><meta charset="UTF-8"><title>Coups - Jonathan Powell</title></head>\n'
    return (
        head
        + t[b0:cov]
        + "\n<!-- A-COUPS fixture: navigation and media-coverage list trimmed -->\n"
        + t[min(files) : blk_end]
        + "\n</body></html>\n"
    ).encode("utf-8")


def _fp(url: str) -> str:
    from reva.evidence.runner import _fixture_fingerprint

    return _fixture_fingerprint(url)


def _entry(
    url: str, body: bytes, ext: str, content_type: str | None, note: str, derived: bytes, licence: str
) -> tuple[dict, bytes]:
    fp = _fp(url)
    e = {
        "request_fingerprint": fp,
        "url": url,
        "file": f"{fp[:16]}{ext}",
        "sha256": hashlib.sha256(body).hexdigest(),
        "status": 200,
        "headers": {"content-type": content_type} if content_type else {},
        "retrieved_at": RETRIEVED,
        "licence": licence,
        "note": note,
        "derived_from_sha256": hashlib.sha256(derived).hexdigest(),
    }
    return e, body


def _write(name: str, entries: list[tuple[dict, bytes]]) -> None:
    d = FIX / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    ucdp = json.loads((FIX / "atlas.ucdp" / "manifest.json").read_text())
    cc = next(e for e in ucdp if e["url"].endswith("countrycode_1.9.0.tar.gz"))
    shutil.copy(FIX / "atlas.ucdp" / cc["file"], d / cc["file"])
    man = [cc]
    for e, body in entries:
        (d / e["file"]).write_bytes(body)
        man.append(e)
    (d / "manifest.json").write_text(json.dumps(man, indent=1, ensure_ascii=False) + "\n")


def build(cap: Path) -> dict[str, int]:
    from reva.adapters.connectors.atlas_coups import parse_csv
    from reva.adapters.connectors.atlas_leaders import parse_archigos
    from reva.adapters.connectors.atlas_xlsx import Workbook

    page = (cap / "coups.html").read_bytes()
    pt = (cap / "pt.csv").read_bytes()
    dta = (cap / "Archigos_4.1_stata14.dta").read_bytes()
    ll = (cap / "leaderlist.xlsx").read_bytes()
    events = parse_csv(pt)
    pt_lines = [e.row for e in events if e.ccode in STATES]
    spells, _ = parse_archigos(dta)
    rows = [s.row for s in spells if s.ccode in STATES and s.end.year >= ARCHIGOS_FROM_YEAR]
    rows += [s.row for s in spells if s.ccode == 2][:3]  # first USA spells (DBpedia titles, regular exits)
    rows = sorted(set(rows))
    wb = Workbook(ll)
    ll_rows = [
        r for r, cells in wb.rows("leaderlist") if r > 1 and cells.get(1) and int(float(cells[1].text)) in STATES
    ]
    ll_rows += [
        2,
        1117,
        1530,
        2242,
    ]  # row 2 holds the `updated` cell; then  # the three provider defects (MNE end month without year, ISL day/month, emonth 20)
    pg = trim_page(page)
    pt_sub = subset_csv(pt, pt_lines)
    page_e = _entry(
        PAGE_URL,
        pg,
        ".html",
        "text/html",
        "trimmed copy of the coups page (body text, data links)",
        page,
        "page text: no licence stated",
    )
    pt_e = _entry(
        PT_URL,
        pt_sub,
        ".csv",
        "text/csv",
        f"P&T coup list V2026.08.29, original lines of COW {sorted(STATES)}",
        pt,
        NO_LICENCE,
    )
    _write("atlas.coups", [page_e, pt_e])
    dta_sub = subset_dta(dta, rows)
    dta_e = _entry(
        DTA_URL,
        dta_sub,
        ".dta",
        None,
        f"Archigos 4.1 rows of COW {sorted(STATES)} ending >= "
        f"{ARCHIGOS_FROM_YEAR} + first 3 USA rows; original bytes, header N/map rewritten "
        "(captured from rochester.edu, same file as www.rochester.edu: 2,905,898 bytes)",
        dta,
        NO_LICENCE,
    )
    _write("atlas.archigos", [dta_e, page_e, pt_e])
    ll_sub = subset_xlsx(ll, "leaderlist", ll_rows)
    ll_e = _entry(
        LL_URL,
        ll_sub,
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        f"leaderlist.xlsx (2026-04-14) rows of COW {sorted(STATES)} + 3 defect rows, rewritten workbook with "
        "original cell texts and row numbers",
        ll,
        NO_LICENCE,
    )
    _write("atlas.reign_leaders", [page_e, ll_e])
    return {
        "pt_lines": len(pt_lines),
        "archigos_rows": len(rows),
        "leaderlist_rows": len(ll_rows),
        "dta_bytes": len(dta_sub),
        "xlsx_bytes": len(ll_sub),
        "page_bytes": len(pg),
    }


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(build(Path(sys.argv[1])), indent=1))
