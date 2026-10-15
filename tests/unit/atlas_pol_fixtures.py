"""How the A-POL test fixtures were derived from the live captures of 2026-10-15 (agent A-POL).

Run once by hand (``python tests/unit/atlas_pol_fixtures.py <raw dir>``); the tests only read the
written fixtures. Provider files are too large to commit whole (vdem.RData 34 MB, IPU chambers 26 MB,
LIED 2.4 MB), so each fixture is a **subset** of the real bytes:

* V-Dem: rows of a few units/years and every column the connector reads, re-serialized as RData with
  values copied bit-for-bit (:mod:`atlas_pol_rdata_writer`); DESCRIPTION and codebook.RData are the
  real bytes.
* LIED: the header and the rows of a few units, written as a minimal XLSX (inline strings, exact
  cell text); BMR: the header and the rows of a few ccodes, CSV text unchanged.
* Dataverse records: the real records with the data file's MD5 replaced by the subset's MD5
  (``_fixture_note`` added) so the integrity check runs on the subset.
* IPU: ``data`` restricted to a few parliaments with ``meta.total`` set to the subset size
  (``meta._fixture_note`` added); everything else unchanged.

Each manifest entry records ``derived_from_sha256`` (the live bytes) and a ``note``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "source"
RETRIEVED = "2026-10-15T22:08:36+00:00"

VDEM_UNITS = ("FRA", "DEU", "PSE", "BVR", "TWN", "USA")
VDEM_YEARS = (1789, 1800, 1871, 1900, 1945, 1990, 2024, 2025)
LIED_KEYS = (
    "France",
    "Prussia",
    "Germany",
    "Germany, West",
    "Vietnam",
    "Vietnam, North",
    "Bavaria",
    "Taiwan",
    "Palestine/West Bank",
    "Brunei",
)
BMR_CCODES = ("2", "220", "255", "260", "364", "365", "490", "713")
IPU_PARLIAMENTS = ("FR", "NA")  # FR: bicameral, history since 1945; NA (Namibia): the code "NA" must not become null


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ----------------------------------------------------------------------------- xlsx writer
def xlsx(rows: list[list[str | None]]) -> bytes:
    def col(i: int) -> str:
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    sheet = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>',
    ]
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, v in enumerate(row):
            if v is None or v == "":
                continue
            ref = f"{col(c)}{r}"
            if r > 1 and _is_number(v):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>')
        sheet.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet.append("</sheetData></worksheet>")
    parts = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": "".join(sheet),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            zi = zipfile.ZipInfo(name, date_time=(2026, 10, 15, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, text)
    return buf.getvalue()


def _is_number(v: Any) -> bool:
    try:
        float(str(v))
        return True
    except ValueError:
        return False


# ----------------------------------------------------------------------------- derivations
def derive_vdem(raw: bytes) -> bytes:
    sys.path.insert(0, str(Path(__file__).parent))
    import atlas_pol_rdata_writer as w

    from reva.adapters.connectors import atlas_vdem as v
    from reva.adapters.connectors.atlas_rdata import IntColumn, RealColumn, StrColumn

    data = v.read_vdem(raw)
    tid = data.columns["country_text_id"].values  # type: ignore[union-attr]
    yr = data.columns["year"]
    rows = [i for i in range(data.nrows) if tid[i] in VDEM_UNITS and int(yr.values[i]) in VDEM_YEARS]
    cols: dict[str, Any] = {}
    for name in data.all_columns:
        c = data.columns.get(name)
        if c is None:
            continue
        if isinstance(c, StrColumn):
            cols[name] = ("str", [c.values[i] for i in rows])
        elif isinstance(c, RealColumn):
            cols[name] = ("real", [None if c.na[i] else float(c.values[i]) for i in rows])
        elif isinstance(c, IntColumn):
            cols[name] = ("int", [None if c.na[i] else int(c.values[i]) for i in rows])
    return w.rdata({"vdem": w.data_frame(cols, len(rows))})


def derive_lied(body: bytes) -> bytes:
    from reva.adapters.connectors.atlas_xlsx import Workbook

    wb = Workbook(body)
    table = wb.table(next(iter(wb.sheets)))
    keep = [table[0]] + [r for r in table[1:] if str(r[0]).strip() in LIED_KEYS]
    return xlsx([[None if v is None else str(v) for v in r] for r in keep])


def derive_bmr(body: bytes) -> bytes:
    text = body.decode("utf-8")
    lines = text.splitlines(keepends=True)
    rdr = csv.reader(io.StringIO(text))
    header = next(rdr)
    ci = header.index("ccode")
    out = [lines[0]]
    for line, row in zip(lines[1:], rdr, strict=True):
        if row[ci] in BMR_CCODES:
            out.append(line)
    return "".join(out).encode("utf-8")


def derive_record(record: bytes, file_id: int, payload: bytes) -> bytes:
    doc = json.loads(record)
    for f in doc["data"]["latestVersion"]["files"]:
        if f["dataFile"]["id"] == file_id:
            f["dataFile"]["checksum"]["value"] = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    doc["_fixture_note"] = "A-POL fixture: real record, data file MD5 replaced by the MD5 of the fixture subset"
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def derive_ipu(body: bytes, name: str) -> bytes:
    doc = json.loads(body)
    if name == "elections":
        keep = [r for r in doc["data"] if r["attributes"]["chamber"]["value"][:2] in IPU_PARLIAMENTS]
    elif name == "chambers":
        keep = [r for r in doc["data"] if r["id"][:2] in IPU_PARLIAMENTS]
    else:
        keep = [r for r in doc["data"] if r["id"] in IPU_PARLIAMENTS]
    doc["data"] = keep
    doc["meta"]["total"] = len(keep)
    doc["meta"]["_fixture_note"] = f"A-POL fixture: data restricted to parliaments {IPU_PARLIAMENTS}; total adjusted"
    return json.dumps(doc, ensure_ascii=False).encode("utf-8")


def write(connector: str, entries: list[dict[str, Any]]) -> None:
    from reva.evidence.runner import _fixture_fingerprint

    d = FIX / connector
    d.mkdir(parents=True, exist_ok=True)
    manifest = []
    for e in entries:
        fp = _fixture_fingerprint(e["url"])
        fname = fp[:16] + e["ext"]
        (d / fname).write_bytes(e["body"])
        manifest.append(
            {
                "request_fingerprint": fp,
                "url": e["url"],
                "file": fname,
                "sha256": _sha(e["body"]),
                "status": 200,
                "headers": {"content-type": e["ctype"]},
                "retrieved_at": RETRIEVED,
                "licence": e["licence"],
                "derived_from_sha256": e.get("orig"),
                "note": e.get("note", "live bytes, unchanged"),
            }
        )
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def main(raw_dir: str) -> None:
    from reva.adapters.connectors import atlas_ipu, atlas_regimes, atlas_vdem

    r = Path(raw_dir)
    vd = (r / "vdem" / "vdem.RData").read_bytes()
    items = {i.key: i.url for i in atlas_vdem.fetch_items()}
    sub = derive_vdem(vd)
    write(
        "atlas.vdem",
        [
            {
                "url": items["vdemdata.description"],
                "body": (r / "vdem" / "DESCRIPTION").read_bytes(),
                "ext": ".txt",
                "ctype": "text/plain; charset=utf-8",
                "licence": "GPL-3 (package metadata)",
            },
            {
                "url": items["vdemdata.codebook"],
                "body": (r / "vdem" / "codebook.RData").read_bytes(),
                "ext": ".RData",
                "ctype": "application/octet-stream",
                "licence": "CC-BY-SA-4.0",
            },
            {
                "url": items["vdemdata.vdem"],
                "body": sub,
                "ext": ".RData",
                "ctype": "application/octet-stream",
                "licence": "CC-BY-SA-4.0",
                "orig": _sha(vd),
                "note": f"subset of vdem.RData v16: units {VDEM_UNITS} x years {VDEM_YEARS}, every column the connector reads; values bit-identical",
            },
        ],
    )
    items = {i.key: i.url for w in ("lied", "bmr") for i in atlas_regimes.fetch_items(w)}
    lied = (r / "dv" / "LIED_6.9.xlsx").read_bytes()
    bmr = (r / "dv" / "democracy-v4.0.csv").read_bytes()
    lsub, bsub = derive_lied(lied), derive_bmr(bmr)
    write(
        "atlas.lied",
        [
            {
                "url": items["lied.record"],
                "body": derive_record((r / "dv" / "lied_record.json").read_bytes(), atlas_regimes.LIED_FILE_ID, lsub),
                "ext": ".json",
                "ctype": "application/json",
                "licence": "CC0-1.0",
                "note": "real record; file MD5 replaced by the subset's",
            },
            {
                "url": items["lied.file"],
                "body": lsub,
                "ext": ".xlsx",
                "ctype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "licence": "CC0-1.0",
                "orig": _sha(lied),
                "note": f"subset of LIED_6.9.xlsx: header + rows of {LIED_KEYS}; exact cell text",
            },
        ],
    )
    write(
        "atlas.bmr",
        [
            {
                "url": items["bmr.record"],
                "body": derive_record((r / "dv" / "bmr_record.json").read_bytes(), atlas_regimes.BMR_FILE_ID, bsub),
                "ext": ".json",
                "ctype": "application/json",
                "licence": "CC0-1.0",
                "note": "real record; file MD5 replaced by the subset's",
            },
            {
                "url": items["bmr.file"],
                "body": bsub,
                "ext": ".csv",
                "ctype": "text/csv",
                "licence": "CC0-1.0",
                "orig": _sha(bmr),
                "note": f"subset of democracy-v4.0.csv: header + rows of ccodes {BMR_CCODES}; lines unchanged",
            },
        ],
    )
    entries = []
    for it in atlas_ipu.fetch_items():
        name = it.key.split(".", 1)[1]
        body = (r.parent / "web" / f"all_{name}.json").read_bytes()
        entries.append(
            {
                "url": it.url,
                "body": derive_ipu(body, name),
                "ext": ".json",
                "ctype": "application/json",
                "licence": "CC-BY-NC-SA-4.0",
                "orig": _sha(body),
                "note": f"subset of the live response: parliaments {IPU_PARLIAMENTS}; meta.total adjusted",
            }
        )
    write("atlas.ipu", entries)


if __name__ == "__main__":
    main(sys.argv[1])
