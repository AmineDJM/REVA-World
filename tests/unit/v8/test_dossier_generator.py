"""V8DOSSIER generator invariants (no database): every sentence comes from a template, every value has a footnote
(source, licence, period), missing values are stated (never zero), exports keep every footnote."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from reva.v8.dossier import model as M
from reva.v8.dossier.context import Ctx
from reva.v8.dossier.export import to_html, to_markdown
from reva.v8.dossier.generator import Notes, Renderer, TemplateError, build_document
from reva.v8.dossier.queries import QUERIES
from reva.v8.dossier.templates import PLACEHOLDER, TEMPLATE_DIR, Template, TemplateT, all_templates
from reva.v8.dossier.values import Miss, Result, SeriesData, Src, Val, label_val, num_val

SRC = Src(
    source="wb",
    title="Banque mondiale",
    dataset="wb.wdi",
    indicator="NY.GDP",
    period="2024",
    licence="CC-BY-4.0",
    ref="abc",
)
SRC2 = Src(
    source="who",
    title="OMS",
    dataset="who.ghe",
    indicator="deaths",
    period="2023",
    licence="CC BY 4.0",
    ref="def",
    non_commercial=True,
    restricted=True,
)


def _fake(ctx: Ctx, *, mode: str = "ok") -> Result:
    if mode == "empty":
        return Result(empty_reason="table non chargée")
    r = Result()
    r.values["gdp"] = num_val("3366315927447.33", SRC, unit="US dollars")
    r.values["gdp_year"] = label_val("2024", SRC)
    r.values["deaths"] = num_val("185828.76", SRC2, unit="deaths", kind="estimate")
    r.values["gone"] = Miss("alphabétisation", "aucune source consultée ne rapporte de valeur")
    r.rows = [
        {
            "name": label_val("Nigeria", SRC2, href="/v8/fiche/country/NGA"),
            "v": num_val("185828.76", SRC2, unit="deaths", kind="estimate"),
        },
        {"name": label_val("RD Congo", SRC2), "v": Miss("décès", "non publié")},
    ]
    r.series = [
        SeriesData(
            "PIB",
            "observed",
            SRC,
            "US dollars",
            "year",
            [("2023", 1.0, "1", None, None), ("2024", None, None, None, None)],
        )
    ]
    return r


QUERIES["test.fake"] = _fake

TPL = {
    "kind": "testkind",
    "label": "Test",
    "resolver": "curated",
    "id_pattern": "^[a-z]+$",
    "title": "{$subject}",
    "subtitle": "Dossier test",
    "honesty": ["Chaque nombre a sa source."],
    "not_covered": [{"what": "Essais", "reason": "non chargés"}],
    "subjects": {"demo": {"label": "Démo", "params": {"x": "1"}}},
    "summary": [
        {"id": "s1", "text": "PIB : {b1.gdp} ({b1.gdp_year})."},
        {"id": "s2", "text": "Alphabétisation : {b1.gone}."},
        {"id": "s3", "text": "Absent : {b2.anything}.", "missing": "Le bloc vide n'a rien produit."},
    ],
    "sections": [
        {
            "id": "eco",
            "title": "Économie",
            "blocks": [
                {
                    "id": "b1",
                    "query": "test.fake",
                    "sentences": [
                        {"id": "gdp", "text": "Le PIB atteint {gdp} en {gdp_year}."},
                        {"id": "lit", "what": "Alphabétisation", "text": "Alphabétisation : {gone}."},
                        {
                            "id": "lit2",
                            "text": "Taux : {gone} ; décès : {deaths}.",
                            "missing": "Décès : {deaths} ; alphabétisation : {gone}.",
                        },
                    ],
                    "list": {"item": "{name} : {v}", "item_missing": "{name} (valeur non publiée)"},
                    "table": {
                        "caption": "Décès",
                        "columns": [
                            {"header": "Pays", "cell": "{name}"},
                            {"header": "Décès", "cell": "{v}", "numeric": True},
                        ],
                    },
                    "chart": {"type": "line", "title": "PIB"},
                    "empty": {"what": "Économie"},
                },
                {
                    "id": "b2",
                    "query": "test.fake",
                    "params": {"mode": "empty"},
                    "sentences": [{"id": "x", "text": "{anything}"}],
                    "empty": {"what": "Bloc vide"},
                },
            ],
        }
    ],
}


def _template(tmp_path: Path, data: dict[str, Any] | None = None) -> Template:
    p = tmp_path / "testkind.yaml"
    p.write_text(yaml.safe_dump(data or TPL, allow_unicode=True))
    return Template(TemplateT.model_validate(yaml.safe_load(p.read_text())), p, "v1")


def _doc(tmp_path: Path) -> tuple[M.Document, Template]:
    tpl = _template(tmp_path)
    ctx = Ctx(conn=None, store=None, snapshot_id="snap")
    ref = M.SubjectRef(kind="testkind", id="demo", label="Démo")
    svals = {"subject": label_val("Démo", Src(source="reva.registry", title="modèle"))}
    return build_document(tpl, ctx, ref, {"id": "demo", "x": "1"}, svals, 0.0), tpl


def _sentences(doc: M.Document) -> list[M.Sentence]:
    out = list(doc.summary)
    for s in doc.sections:
        for b in s.blocks:
            if isinstance(b, M.ParagraphBlock):
                out += b.sentences
            elif isinstance(b, M.ListBlock):
                out += b.items
    return out


def _all_parts(doc: M.Document) -> list[M.Part]:
    parts = [p for s in _sentences(doc) for p in s.parts]
    for s in doc.sections:
        for b in s.blocks:
            if isinstance(b, M.TableBlock):
                parts += [p for row in b.rows for c in row for p in c.parts]
    return parts


def _template_text(tpl: Template, template_id: str) -> str:
    t = tpl.t
    if template_id.startswith("summary."):
        sid = template_id.split(".")[1]
        s = next(x for x in t.summary if x.id == sid)
        return s.missing if template_id.endswith(".missing") else s.text  # type: ignore[return-value]
    if template_id in ("title", "subtitle"):
        return getattr(t, template_id)  # type: ignore[no-any-return]
    sec, blk, rest = template_id.split(".", 2)
    b = next(x for x in next(s for s in t.sections if s.id == sec).blocks if x.id == blk)
    if rest == "item":
        return b.list_.item  # type: ignore[union-attr]
    if rest == "item_missing":
        return b.list_.item_missing  # type: ignore[union-attr,return-value]
    sid, _, miss = rest.partition(".")
    s = next(x for x in b.sentences if x.id == sid)
    return s.missing if miss else s.text  # type: ignore[return-value]


def test_every_sentence_is_its_template_filled(tmp_path: Path) -> None:
    doc, tpl = _doc(tmp_path)
    ids = tpl.sentence_ids()
    sents = _sentences(doc)
    assert sents
    for s in sents:
        assert s.template_id in ids, s.template_id
        # the text runs are exactly the template's literal segments, in order: no free text
        literal = [seg for seg in PLACEHOLDER.split(_template_text(tpl, s.template_id))[::3] if seg]
        assert [p.text for p in s.parts if p.t == "text"] == literal
        assert s.text == "".join(p.text for p in s.parts)


def test_every_value_has_a_footnote_with_source_licence_period(tmp_path: Path) -> None:
    doc, _ = _doc(tmp_path)
    fns = {f.n: f for f in doc.footnotes}
    vals = [p for p in _all_parts(doc) if p.t == "value"]
    assert len(vals) >= 6
    for p in vals:
        assert p.fn in fns
        f = fns[p.fn]
        assert f.title and f.source
    nums = [p for p in vals if p.exact and re.fullmatch(r"-?\d+(\.\d+)?", p.exact)]
    assert nums and all(fns[p.fn].licence and fns[p.fn].period for p in nums if p.fn is not None)
    for b in (b for s in doc.sections for b in s.blocks if isinstance(b, M.ChartBlock)):
        assert all(s.fn in fns for s in b.series)
    # a number never appears in template text: digits only come from value parts
    for s in _sentences(doc):
        for p in s.parts:
            if p.t == "text":
                assert not re.search(r"\d", p.text), p.text


def test_missing_is_stated_never_zero(tmp_path: Path) -> None:
    doc, _ = _doc(tmp_path)
    eco = doc.sections[0]
    miss_blocks = [b for b in eco.blocks if isinstance(b, M.MissingBlock)]
    whats = {b.what for b in miss_blocks}
    assert "Alphabétisation" in whats  # sentence without `missing:` -> explicit Missing block
    assert "Bloc vide" in whats  # empty query -> Missing block with the query's reason
    assert any(b.reason == "table non chargée" for b in miss_blocks)
    lit2 = next(
        s
        for b in eco.blocks
        if isinstance(b, M.ParagraphBlock)
        for s in b.sentences
        if s.template_id.endswith("lit2.missing")
    )
    assert any(p.t == "missing" and "aucune source" in (p.reason or "") for p in lit2.parts)
    assert all(p.text != "0" for p in _all_parts(doc) if p.t == "value")
    # the list item with a missing value uses its fallback template; the table states the cell missing
    lst = next(b for b in eco.blocks if isinstance(b, M.ListBlock))
    assert lst.items[1].template_id.endswith("item_missing")
    tbl = next(b for b in eco.blocks if isinstance(b, M.TableBlock))
    assert tbl.rows[1][1].parts[0].t == "missing"
    # summary: a sentence with a missing value is dropped or replaced by its missing template
    assert [s.template_id for s in doc.summary] == ["summary.s1", "summary.s3.missing"]
    assert {"Essais", "Alphabétisation", "Bloc vide"} <= {n.what for n in doc.not_covered}


def test_footnotes_deduplicated_and_numbered_in_order(tmp_path: Path) -> None:
    doc, _ = _doc(tmp_path)
    assert [f.n for f in doc.footnotes] == list(range(1, len(doc.footnotes) + 1))
    keys = [(f.source, f.dataset, f.indicator, f.period, f.ref, f.method, f.title) for f in doc.footnotes]
    assert len(keys) == len(set(keys))
    assert doc.licences[0]["restricted"] is True  # restricted licences listed first


def test_unknown_placeholder_is_an_error(tmp_path: Path) -> None:
    r = Renderer(Notes(), {})
    with pytest.raises(TemplateError):
        r.sentence("x", "Valeur : {nope}.", {})
    with pytest.raises(TemplateError):
        r.sentence("x", "Sujet : {$nope}.", {})
    s, miss = r.sentence("x", "Bloc : {other.value}.", {})
    assert miss and s.parts[1].t == "missing"


def test_vocab_filter_keeps_exact_code(tmp_path: Path) -> None:
    r = Renderer(Notes(), {}, {"interstate": "interétatique"})
    s, _ = r.sentence("x", "Type : {t|vocab}.", {"t": label_val("interstate", SRC)})
    v = s.parts[1]
    assert v.text == "interétatique" and v.exact == "interstate" and v.fn == 1


def test_exports_keep_every_footnote(tmp_path: Path) -> None:
    doc, _ = _doc(tmp_path)
    md = to_markdown(doc)
    html = to_html(doc)
    for f in doc.footnotes:
        assert f"[^{f.n}]:" in md
        assert f'id="fn{f.n}"' in html
    for p in _all_parts(doc):
        if p.t == "value":
            assert f"[^{p.fn}]" in md
    assert "Ce que ce dossier ne couvre pas" in md and "Ce que ce dossier ne couvre pas" in html
    assert "manquant" in md and 'class="miss"' in html
    assert "@media print" in html and "<svg" in html
    assert "<script" not in html


@pytest.mark.parametrize("tpl", all_templates(), ids=lambda t: t.t.kind)
def test_registry_templates_are_consistent(tpl: Template) -> None:
    """Every real template: known queries, $parameters provided by every subject (curated, generic or resolver),
    no number in template text except in vocabulary / ids, block ids unique (validated on load)."""
    t = tpl.t
    resolver_params = {
        "country": {"iso3", "qid", "ids"},
        "kg": {"id", "ids"},
        "chembl_drug": {"id", "ids"},
        "ucdp_conflict": {"id", "ids", "countries", "events"},
        "decision": {"id", "reference_id", "countries"},
        "curated": {"id"},
    }[t.resolver]
    base = {k: "x" for k in resolver_params}
    param_sets = [{**base, **s.params} for s in t.subjects.values()]
    if t.resolver != "curated":
        param_sets.append({**base, **t.generic})
    for sec in t.sections:
        for b in sec.blocks:
            assert b.query in QUERIES, f"{sec.id}.{b.id}: unknown query {b.query}"
            used = set(re.findall(r'"\$([a-z_0-9]+)"', repr(b.params).replace("'", '"')))
            for ps in param_sets:
                if b.when and not ps.get(b.when):
                    continue
                missing = used - set(ps)
                assert not missing, f"{tpl.path.name} {sec.id}.{b.id}: parameters {missing} not provided"
    texts = [s.text for s in t.summary] + [x.text for sec in t.sections for b in sec.blocks for x in b.sentences]
    for x in texts:
        literal = "".join(PLACEHOLDER.split(x)[::3])
        assert not re.search(r"\d{2,}", literal.replace("CIM-11", "").replace("SH 2709", "")), (
            f"{tpl.path.name}: number in template text: {x}"
        )


def test_registry_dir_has_the_six_briefs() -> None:
    kinds = {p.stem for p in TEMPLATE_DIR.glob("*.yaml")}
    assert {"country", "commodity", "disease", "drug", "conflict", "decision"} <= kinds
    feats = {(t.t.kind, f.id) for t in all_templates() for f in t.t.featured} | {
        (t.t.kind, k) for t in all_templates() for k in t.t.subjects
    }
    assert {
        ("country", "FRA"),
        ("commodity", "crude_oil"),
        ("disease", "malaria"),
        ("drug", "chembl:CHEMBL941"),
        ("conflict", "ucdp:conflict:13243"),
        ("decision", "opec_plus_cut_2mbd_2027q1"),
    } <= feats


def test_num_val_keeps_exact_and_formats_french() -> None:
    v = num_val("3366315927447.3286", SRC, unit="US dollars")
    assert isinstance(v, Val) and v.exact == "3366315927447.3286"
    assert v.text.startswith("3 366 milliards") and v.text.endswith("dollars US")
    m = num_val(None, SRC)
    assert isinstance(m, Miss)
    assert num_val("0.841", SRC, unit="% change").text == "0,84 %"  # type: ignore[union-attr]
