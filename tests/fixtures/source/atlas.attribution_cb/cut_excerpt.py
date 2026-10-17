"""Cut the derived excerpts of tests/fixtures/source/atlas.attribution_cb from the real Carbon Brief files (V4CLIMATE).

usage: python -I cut_excerpt.py <index.html> <papers-download.csv> <out_dir>

* CSV: the header and the real rows (cell values unchanged, re-serialised with csv.writer) at the 1-based data positions in ROWS plus every row of the
  names in NAMES (several studies of one extreme);
* HTML: the page's real text segments the connector checks (licence footer with the CC BY-NC-ND link, the 'Last
  updated' line, the human-caused climate change sentence), each copied verbatim around its marker, in a minimal
  page with a REVA_EXCERPT comment.
Licence of the source: CC BY-NC-ND 4.0 (Carbon Brief) -- test use only, unadapted rows.
"""

import hashlib
import sys

ROWS = range(1, 41)
NAMES = ("Ethiopia drought, 2015", "Russia summer heatwave, 2010", "Okavango basin floods, 2009-11")
MARKERS = (
    "Last updated:",
    "They calculated the specific contribution",
    "Published under a CC license",
    "creativecommons.org/licenses/by-nc-nd/4.0",
)


def main(page: str, csv_path: str, out: str) -> None:
    raw = open(csv_path, "rb").read()
    lines = raw.decode("utf-8-sig").splitlines(True)
    import csv
    import io

    recs = list(csv.reader(io.StringIO("".join(lines))))
    keep = [recs[0]] + [r for i, r in enumerate(recs[1:], start=1) if i in ROWS or r[0] in NAMES]
    o = io.StringIO()
    csv.writer(o, lineterminator="\r\n").writerows(keep)
    open(f"{out}/papers-download.excerpt.csv", "w", encoding="utf-8", newline="").write(o.getvalue())
    h = open(page, encoding="utf-8").read()
    segs = []
    for m in MARKERS:
        i = h.find(m)
        segs.append(h[max(0, i - 300) : i + 400])
    note = f"REVA_EXCERPT derived from index.html sha256 {hashlib.sha256(open(page, 'rb').read()).hexdigest()}"
    open(f"{out}/index.excerpt.html", "w", encoding="utf-8").write(
        f"<!-- {note} -->\n<html><body>\n" + "\n<hr>\n".join(segs) + "\n</body></html>\n"
    )
    print(len(keep) - 1, "rows")


if __name__ == "__main__":
    main(*sys.argv[1:4])
