"""Exports of a dossier ``Document``: Markdown and print-ready HTML (the reader prints to PDF; no PDF library).

Both exports render only the document's own parts (template text + sourced values + footnote numbers); every value
keeps its footnote marker ``[n]`` (Markdown) or superscript link (HTML), and every footnote lists its source,
licence, period and reference.
"""

from __future__ import annotations

import html
import math
from collections.abc import Iterable

from reva.v8.dossier import model as M

KIND_FR = {
    "observed": "observé",
    "estimate": "estimation",
    "projection": "projection",
    "forecast": "prévision scellée",
    "scenario": "scénario non validé",
    "derived": "calcul REVA",
    "claim": "affirmation attribuée",
    "label": "",
}


# ============================================================================== Markdown
def _md_parts(parts: Iterable[M.Part]) -> str:
    out = []
    for p in parts:
        if p.t == "text":
            out.append(p.text.replace("*", "\\*"))
        elif p.t == "value":
            badge = "" if p.kind in (None, "observed", "label") else f" _({KIND_FR.get(p.kind, p.kind)})_"
            out.append(f"**{p.text}**{badge}[^{p.fn}]")
        else:
            out.append(f"_⟨manquant : {p.reason}⟩_")
    return "".join(out)


def _fn_line(f: M.Footnote) -> str:
    bits = [f.title]
    if f.dataset:
        bits.append(f.dataset)
    if f.indicator:
        bits.append(f"`{f.indicator}`")
    if f.period:
        bits.append(f"période {f.period}")
    lic = f.licence or "licence non indiquée"
    if f.non_commercial:
        lic += " — usage non commercial"
    elif f.restricted:
        lic += " — conditions restrictives"
    bits.append(f"licence : {lic}")
    if f.method:
        bits.append(f"méthode : {f.method}")
    if f.verification:
        bits.append(f"statut : {f.verification}")
    if f.ref:
        bits.append(f"réf. `{f.ref[:64]}`")
    if f.url:
        bits.append(f.url)
    if f.required_notice:
        bits.append(f"mention obligatoire : {f.required_notice}")
    return "; ".join(bits)


def to_markdown(doc: M.Document) -> str:
    L: list[str] = [f"# {doc.title}", "", f"_{doc.subtitle}_", ""]
    m = doc.meta
    L += [
        f"> Généré le {m.generated_at} · modèle `{m.template}` v{m.template_version} · instantané de données "
        f"`{m.snapshot_id}`" + (f" · atlas `{m.atlas_snapshot[:12]}`" if m.atlas_snapshot else ""),
        "",
    ]
    if doc.summary:
        L += ["## L'essentiel", ""]
        L += [f"- {_md_parts(s.parts)}" for s in doc.summary]
        L.append("")
    L += ["## Sommaire", ""]
    L += [f"{i}. {s.title}" for i, s in enumerate(doc.sections, 1)]
    L += [f"{len(doc.sections) + 1}. Ce que ce dossier ne couvre pas", f"{len(doc.sections) + 2}. Sources", ""]
    for s in doc.sections:
        L += [f"## {s.title}", ""]
        if s.lede:
            L += [f"_{s.lede}_", ""]
        for b in s.blocks:
            L += _md_block(b)
    L += ["## Ce que ce dossier ne couvre pas", ""]
    L += [f"- **{n.what}** — {n.reason}" + (f" _(section {n.section})_" if n.section else "") for n in doc.not_covered]
    L += ["", "## Règles de lecture", ""]
    L += [f"- {h}" for h in doc.honesty]
    L += ["", "## Licences des sources citées", ""]
    for x in doc.licences:
        flag = " ⚠ restrictive" if x.get("restricted") else ""
        L.append(f"- {x['title']} — {x['licence']}{flag}")
    L += ["", "## Sources", ""]
    L += [f"[^{f.n}]: {_fn_line(f)}" for f in doc.footnotes]
    L.append("")
    return "\n".join(L)


def _md_block(b: M.Block) -> list[str]:
    if isinstance(b, M.ParagraphBlock):
        return [" ".join(_md_parts(s.parts) for s in b.sentences), ""]
    if isinstance(b, M.ListBlock):
        out = [f"**{b.title}**", ""] if b.title else []
        out += [f"- {_md_parts(i.parts)}" for i in b.items]
        if b.more:
            out.append(f"- _… et {b.more} autre(s) chez la source (non listés)_")
        return [*out, ""]
    if isinstance(b, M.TableBlock):
        out = [f"_{b.caption}_", "", "| " + " | ".join(b.columns) + " |"]
        out.append("|" + "|".join("---:" if n else "---" for n in b.numeric) + "|")
        for row in b.rows:
            out.append("| " + " | ".join(_md_parts(c.parts).replace("|", "\\|") for c in row) + " |")
        if b.note:
            out += ["", f"_{b.note}_"]
        return [*out, ""]
    if isinstance(b, M.ChartBlock):
        out = [f"_Graphique : {b.title} ({b.unit})_", ""]
        for s in b.series:
            pts = [p for p in s.points if p.y is not None]
            if pts:
                rng = f"{pts[0].x} → {pts[-1].x}"
                out.append(f"- {s.label} ({KIND_FR.get(s.kind, s.kind) or 'observé'}) : {len(pts)} points, {rng}[^{s.fn}]")
        if b.note:
            out.append(f"_{b.note}_")
        return [*out, ""]
    if isinstance(b, M.MissingBlock):
        return [f"> **Manquant — {b.what}** : {b.reason}", ""]
    return []


# ============================================================================== HTML (print-ready)
CSS = """
:root{--ink:#1b1d22;--muted:#5b616d;--line:#d9dce2;--accent:#2f4bb8;--miss:#7a5a1c;--missbg:#faf5ea;--proj:#7c3fc4;
--scen:#b35400;--est:#0b7a70;--paper:#fffefb}
*{box-sizing:border-box}
html{background:#eceae4}
body{margin:0;color:var(--ink);font:17px/1.62 "Source Serif 4","Source Serif Pro",Georgia,"Times New Roman",serif}
.page{max-width:760px;margin:24px auto;background:var(--paper);padding:56px 64px;box-shadow:0 1px 3px rgba(0,0,0,.12)}
header .kicker{font:600 12px/1.4 system-ui,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
h1{font-size:38px;line-height:1.15;margin:.2em 0 .15em;font-weight:700;letter-spacing:-.01em}
.sub{color:var(--muted);font-style:italic;margin:0 0 1.2em}
.meta{font:12px/1.5 system-ui,sans-serif;color:var(--muted);border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:8px 0;margin-bottom:1.6em}
h2{font-size:24px;margin:1.8em 0 .4em;padding-top:.3em;border-top:2px solid var(--ink);break-after:avoid}
.lede{color:var(--muted);font-style:italic;margin-top:0}
.essentiel{background:#f4f2ec;border-left:4px solid var(--accent);padding:14px 20px;margin:0 0 1.4em}
.essentiel h2{border:0;margin:0 0 .3em;font-size:18px;font:700 13px/1.4 system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase}
.essentiel ul{margin:0;padding-left:1.1em}
.toc{font:14px/1.6 system-ui,sans-serif;columns:2;column-gap:32px;margin-bottom:1em}
.toc ol{margin:0;padding-left:1.3em}
.v{font-weight:600;font-variant-numeric:tabular-nums}
.v.projection,.v.forecast{color:var(--proj)} .v.scenario{color:var(--scen)} .v.estimate{color:var(--est)}
sup.fn a{font:600 10px system-ui,sans-serif;color:var(--accent);text-decoration:none;margin-left:1px}
.miss{color:var(--miss);background:repeating-linear-gradient(135deg,rgba(122,90,28,.22) 0 1.5px,transparent 1.5px 6px),var(--missbg);padding:0 4px;border-radius:3px;font-style:italic;font-size:.92em}
.missing-block{border:1px dashed #c9b48a;background:repeating-linear-gradient(135deg,rgba(122,90,28,.12) 0 1.5px,transparent 1.5px 7px),var(--missbg);padding:8px 12px;border-radius:4px;font:14px/1.5 system-ui,sans-serif;color:var(--miss);margin:.6em 0}
table{border-collapse:collapse;width:100%;font:13.5px/1.45 system-ui,sans-serif;margin:.6em 0 1.2em;break-inside:avoid}
caption{text-align:left;font-style:italic;color:var(--muted);padding-bottom:4px;font-family:Georgia,serif}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{font-weight:600;border-bottom:1.5px solid var(--ink)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
ul.items{padding-left:1.1em} ul.items li{margin:.15em 0}
.list-title{font:600 14px/1.4 system-ui,sans-serif;margin:.8em 0 .2em}
figure{margin:.8em 0 1.4em;break-inside:avoid}
figcaption{font:13px/1.4 system-ui,sans-serif;color:var(--muted)}
.chart svg{width:100%;height:auto;display:block}
.notcovered li{margin:.25em 0}
.sources{font:12px/1.5 system-ui,sans-serif;color:#333}
.sources li{margin:.2em 0;word-break:break-word}
.rules{font-size:15px;color:var(--muted)}
.nc{color:#9a3412;font-weight:600}
@media (max-width:700px){.page{padding:24px 16px;margin:0}h1{font-size:30px}.toc{columns:1}}
@media print{html{background:#fff}.page{box-shadow:none;margin:0;max-width:none;padding:0}
@page{size:A4;margin:18mm 16mm 20mm}body{font-size:10.5pt}h1{font-size:24pt}h2{font-size:15pt;break-before:auto}
a{color:inherit}.toc{columns:2}table,figure,.missing-block{break-inside:avoid}.noprint{display:none}}
"""


def _e(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def _h_parts(parts: Iterable[M.Part]) -> str:
    out = []
    for p in parts:
        if p.t == "text":
            out.append(_e(p.text))
        elif p.t == "value":
            k = p.kind or "observed"
            title = KIND_FR.get(k, "")
            exact = f" · valeur exacte {p.exact}" if p.exact and p.exact != p.text else ""
            out.append(
                f'<span class="v {k}" title="{_e(title + exact)}">{_e(p.text)}</span>'
                f'<sup class="fn"><a href="#fn{p.fn}" id="r{p.fn}">{p.fn}</a></sup>'
            )
        else:
            out.append(f'<span class="miss">manquant : {_e(p.reason)}</span>')
    return "".join(out)


def _svg_chart(b: M.ChartBlock, w: int = 680, h: int = 260) -> str:
    """Small static SVG (print-safe): lines for time series, bars for categories, bands for published bounds."""
    pad_l, pad_r, pad_t, pad_b = 56, 16, 14, 46 if b.x_kind == "category" else 28
    colors = ["#2f4bb8", "#d9711c", "#138a72", "#c43d7a", "#6b7a1a", "#7c3fc4"]
    ys = [v for s in b.series for p in s.points for v in (p.y, p.lo, p.hi) if v is not None]
    if not ys:
        return ""
    lo, hi = min(0.0, min(ys)), max(ys)
    if hi == lo:
        hi = lo + 1
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / 4)) if span > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= 5:
            step *= m
            break
    top = math.ceil(hi / step) * step
    bot = math.floor(lo / step) * step
    xs: list[str] = []
    for s in b.series:
        for p in s.points:
            if p.x not in xs:
                xs.append(p.x)
    if b.x_kind != "category":
        xs.sort()
    n = max(1, len(xs))
    iw, ih = w - pad_l - pad_r, h - pad_t - pad_b

    def X(i: int) -> float:
        return pad_l + (iw * (i + 0.5) / n if b.x_kind == "category" or b.chart == "bar" else iw * i / max(1, n - 1))

    def Y(v: float) -> float:
        return pad_t + ih * (1 - (v - bot) / (top - bot))

    g = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{_e(b.title)}" xmlns="http://www.w3.org/2000/svg">']
    t = bot
    while t <= top + 1e-9:
        y = Y(t)
        lab = f"{t:,.0f}".replace(",", " ") if step >= 1 else f"{t:.2f}".replace(".", ",")
        g.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e3e5ea"/>')
        g.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="10" fill="#5b616d" font-family="system-ui">{lab}</text>')
        t += step
    every = max(1, n // 8)
    for i, x in enumerate(xs):
        if b.x_kind == "category" or i % every == 0 or i == n - 1:
            anchor = "end" if b.x_kind == "category" else "middle"
            rot = f' transform="rotate(-30 {X(i):.1f} {h - pad_b + 12})"' if b.x_kind == "category" else ""
            g.append(f'<text x="{X(i):.1f}" y="{h - pad_b + 14}" text-anchor="{anchor}" font-size="10" fill="#5b616d" font-family="system-ui"{rot}>{_e(x[:18])}</text>')
    idx = {x: i for i, x in enumerate(xs)}
    for si, s in enumerate(b.series):
        c = colors[si % len(colors)]
        dash = ' stroke-dasharray="5 4"' if s.kind in ("projection", "forecast", "scenario") else ""
        if b.chart == "bar" or b.x_kind == "category":
            bw = iw / n * 0.7
            for p in s.points:
                if p.y is None:
                    continue
                x0 = X(idx[p.x]) - bw / 2
                g.append(f'<rect x="{x0:.1f}" y="{Y(max(p.y, 0)):.1f}" width="{bw:.1f}" height="{abs(Y(p.y) - Y(0)):.1f}" fill="{c}" opacity=".85"/>')
            continue
        band = [(idx[p.x], p.lo, p.hi) for p in s.points if p.lo is not None and p.hi is not None]
        if band:
            up = " ".join(f"{X(i):.1f},{Y(hv):.1f}" for i, _, hv in band)
            dn = " ".join(f"{X(i):.1f},{Y(lv):.1f}" for i, lv, _ in reversed(band))
            g.append(f'<polygon points="{up} {dn}" fill="{c}" opacity=".15"/>')
        segs: list[list[str]] = [[]]
        for p in sorted(s.points, key=lambda p: idx[p.x]):
            if p.y is None:
                segs.append([])
            else:
                segs[-1].append(f"{X(idx[p.x]):.1f},{Y(p.y):.1f}")
        for seg in segs:
            if len(seg) > 1:
                g.append(f'<polyline points="{" ".join(seg)}" fill="none" stroke="{c}" stroke-width="1.8"{dash}/>')
            elif len(seg) == 1:
                x, y = seg[0].split(",")
                g.append(f'<circle cx="{x}" cy="{y}" r="2.5" fill="{c}"/>')
    g.append("</svg>")
    return "".join(g)


def _h_block(b: M.Block) -> str:
    if isinstance(b, M.ParagraphBlock):
        return "<p>" + " ".join(_h_parts(s.parts) for s in b.sentences) + "</p>"
    if isinstance(b, M.ListBlock):
        t = f'<div class="list-title">{_e(b.title)}</div>' if b.title else ""
        items = "".join(f"<li>{_h_parts(i.parts)}</li>" for i in b.items)
        more = f'<li class="miss">… et {b.more} autre(s) chez la source, non listé(s)</li>' if b.more else ""
        return f'{t}<ul class="items">{items}{more}</ul>'
    if isinstance(b, M.TableBlock):
        head = "".join(f'<th class="{"n" if n else ""}">{_e(c)}</th>' for c, n in zip(b.columns, b.numeric, strict=True))
        rows = "".join(
            "<tr>" + "".join(f'<td class="{"n" if n else ""}">{_h_parts(c.parts)}</td>' for c, n in zip(r, b.numeric, strict=True)) + "</tr>"
            for r in b.rows
        )
        note = f'<p class="lede">{_e(b.note)}</p>' if b.note else ""
        return f"<table><caption>{_e(b.caption)}</caption><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>{note}"
    if isinstance(b, M.ChartBlock):
        legend = " · ".join(
            f'{_e(s.label)}{" (" + KIND_FR.get(s.kind, "") + ")" if s.kind not in ("observed", "label") else ""}<sup class="fn"><a href="#fn{s.fn}">{s.fn}</a></sup>'
            for s in b.series
        )
        note = f" — {_e(b.note)}" if b.note else ""
        return f'<figure class="chart">{_svg_chart(b)}<figcaption><b>{_e(b.title)}</b> ({_e(b.unit)}). {legend}{note}</figcaption></figure>'
    if isinstance(b, M.MissingBlock):
        return f'<div class="missing-block"><b>Manquant — {_e(b.what)}</b> : {_e(b.reason)}</div>'
    return ""


def to_html(doc: M.Document) -> str:
    m = doc.meta
    toc = "".join(f'<li><a href="#s-{s.id}">{_e(s.title)}</a></li>' for s in doc.sections)
    toc += '<li><a href="#non-couvert">Ce que ce dossier ne couvre pas</a></li><li><a href="#sources">Sources</a></li>'
    secs = []
    for s in doc.sections:
        lede = f'<p class="lede">{_e(s.lede)}</p>' if s.lede else ""
        secs.append(f'<section id="s-{s.id}"><h2>{_e(s.title)}</h2>{lede}{"".join(_h_block(b) for b in s.blocks)}</section>')
    summ = "".join(f"<li>{_h_parts(x.parts)}</li>" for x in doc.summary)
    nc = "".join(
        f"<li><b>{_e(n.what)}</b> — {_e(n.reason)}{' <i>(' + _e(n.section) + ')</i>' if n.section else ''}</li>" for n in doc.not_covered
    )
    lic = "".join(
        f"<li>{_e(x['title'])} — {_e(x['licence'])}{' <span class=nc>⚠ restrictive</span>' if x.get('restricted') else ''}</li>"
        for x in doc.licences
    )
    fns = "".join(f'<li id="fn{f.n}">{_e(_fn_line(f))} <a href="#r{f.n}" class="noprint">↩</a></li>' for f in doc.footnotes)
    rules = "".join(f"<li>{_e(h)}</li>" for h in doc.honesty)
    atlas = f" · atlas {_e(m.atlas_snapshot[:12])}" if m.atlas_snapshot else ""
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(doc.title)} — dossier REVA</title><style>{CSS}</style></head>
<body><article class="page">
<header><div class="kicker">REVA · {_e(doc.subtitle)}</div><h1>{_e(doc.title)}</h1>
<p class="sub">{_e(doc.subject.sublabel or "")}</p>
<div class="meta">Généré le {_e(m.generated_at)} · modèle {_e(m.template)} v{_e(m.template_version)} · instantané de données {_e(m.snapshot_id)}{atlas} · {m.values} valeurs sourcées, {m.missing} manques déclarés, {len(doc.footnotes)} notes</div></header>
{'<div class="essentiel"><h2>L’essentiel</h2><ul>' + summ + '</ul></div>' if summ else ''}
<nav class="toc" aria-label="Sommaire"><ol>{toc}</ol></nav>
{''.join(secs)}
<section id="non-couvert"><h2>Ce que ce dossier ne couvre pas</h2><ul class="notcovered">{nc}</ul></section>
<section><h2>Règles de lecture</h2><ul class="rules">{rules}</ul></section>
<section><h2>Licences des sources citées</h2><ul class="sources">{lic}</ul></section>
<section id="sources"><h2>Sources</h2><ol class="sources">{fns}</ol></section>
</article></body></html>"""
