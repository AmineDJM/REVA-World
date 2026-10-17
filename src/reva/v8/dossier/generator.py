"""Brief generator: template + subject -> ``Document`` (every sentence templated, every value footnoted).

Rendering rules (tested in tests/unit/v8/test_dossier_generator.py):

* A sentence is one template sentence with its ``{placeholders}`` filled; template text is copied verbatim.
* ``{name}`` reads the block's query values (or the current row for list items / table cells), ``{$name}`` the
  subject values, ``{block.name}`` the values of another block (summary). An unknown placeholder is a template
  error, never an empty string.
* A value that is missing makes the sentence use its ``missing:`` template (missing parts stated with their
  reason), or — without one — is dropped and replaced by an explicit Missing block. Nothing is ever shown as 0.
* Every value part gets a footnote (source, licence, period, reference); footnotes are deduplicated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reva.v8.dossier import model as M
from reva.v8.dossier.context import Ctx, _Lru, data_snapshot_id
from reva.v8.dossier.queries import QUERIES, country_val
from reva.v8.dossier.templates import PLACEHOLDER, BlockT, SentenceT, Template, load_template
from reva.v8.dossier.values import Item, Miss, Result, Src, Val, label_val

log = logging.getLogger(__name__)

CACHE = _Lru(64)
DISK_CACHE = Path(tempfile.gettempdir()) / "reva-dossier-cache"


class TemplateError(ValueError):
    pass


class UnknownSubject(LookupError):
    pass


# ============================================================================== footnotes
class Notes:
    def __init__(self) -> None:
        self.by_key: dict[tuple[Any, ...], M.Footnote] = {}

    def fn(self, src: Src) -> int:
        k = src.key()
        f = self.by_key.get(k)
        if f is None:
            f = M.Footnote(n=len(self.by_key) + 1, **src.__dict__)
            self.by_key[k] = f
        return f.n

    def list(self) -> list[M.Footnote]:
        return sorted(self.by_key.values(), key=lambda f: f.n)


# ============================================================================== sentence rendering
class Renderer:
    def __init__(self, notes: Notes, subject_values: Mapping[str, Item], vocab: Mapping[str, str] | None = None) -> None:
        self.notes = notes
        self.subject = subject_values
        self.vocab = dict(vocab or {})
        self.blocks: dict[str, Result] = {}

    def lookup(self, name: str, local: Mapping[str, Item]) -> Item:
        if name.startswith("$"):
            if name[1:] in self.subject:
                return self.subject[name[1:]]
            raise TemplateError(f"unknown subject value {name}")
        if "." in name:
            b, _, k = name.partition(".")
            res = self.blocks.get(b)
            if res is None:
                return Miss(name, "section non calculée")
            if k in res.values:
                return res.values[k]
            return Miss(name, res.empty_reason or "valeur non disponible")
        if name in local:
            return local[name]
        raise TemplateError(f"unknown placeholder {{{name}}}")

    def parts(self, text: str, local: Mapping[str, Item]) -> tuple[list[M.Part], list[Miss]]:
        parts: list[M.Part] = []
        miss: list[Miss] = []
        pos = 0
        for m in PLACEHOLDER.finditer(text):
            if m.start() > pos:
                parts.append(M.Part(t="text", text=text[pos : m.start()]))
            it = self.lookup(m.group(1), local)
            if isinstance(it, Val):
                shown = self.vocab.get(it.text, it.text) if m.group(2) else it.text
                parts.append(
                    M.Part(
                        t="value",
                        text=shown,
                        exact=it.exact if it.exact is not None or shown == it.text else it.text,
                        unit=it.unit,
                        kind=it.kind,  # type: ignore[arg-type]
                        fn=self.notes.fn(it.src),
                        href=it.href,
                    )
                )
            else:
                miss.append(it)
                parts.append(M.Part(t="missing", text=f"manquant : {it.reason}", reason=it.reason))
            pos = m.end()
        if pos < len(text):
            parts.append(M.Part(t="text", text=text[pos:]))
        return parts, miss

    def sentence(self, tid: str, text: str, local: Mapping[str, Item]) -> tuple[M.Sentence, list[Miss]]:
        parts, miss = self.parts(text, local)
        return M.Sentence(template_id=tid, parts=parts, text="".join(p.text for p in parts)), miss


# ============================================================================== subject resolution
def _country_qids() -> dict[str, str]:
    from reva.kg import query as kq

    return kq.country_qid_registry()


def resolve_subject(tpl: Template, sid: str, ctx: Ctx) -> tuple[M.SubjectRef, dict[str, Any], dict[str, Item]]:
    """(subject ref, raw params for queries, subject values for templates)."""
    import re

    t = tpl.t
    if not re.fullmatch(t.id_pattern, sid):
        raise UnknownSubject(f"identifiant {sid!r} invalide pour un dossier {t.kind}")
    reg_src = Src(
        source="reva.registry",
        title=f"Modèle de dossier REVA ({tpl.path.name})",
        dataset=f"registry/dossier/{tpl.path.name}",
        licence="REVA",
        ref=tpl.version,
    )
    if t.resolver == "country":
        iso3 = sid.upper()
        c = country_val(iso3)
        if isinstance(c, Miss):
            raise UnknownSubject(f"pays {iso3} inconnu du registre")
        qid = _country_qids().get(iso3)
        params = {"iso3": iso3, "qid": qid or "", "ids": [qid] if qid else []}
        ref = M.SubjectRef(kind=t.kind, id=iso3, label=c.text, sublabel=f"{iso3}" + (f" · {qid}" if qid else ""), entity_href=f"/v8/fiche/country/{iso3}")
        return ref, params, {"subject": c, "iso3": label_val(iso3, c.src)}
    cur = t.subjects.get(sid)
    if cur is not None:
        params = {"id": sid, **cur.params}
        lab = label_val(cur.label, reg_src)
        href = f"/v8/fiche/{cur.entity_kind or 'entity'}/{cur.entity}" if cur.entity else None
        ref = M.SubjectRef(kind=t.kind, id=sid, label=cur.label, sublabel=cur.sublabel, entity_href=href)
        vals: dict[str, Item] = {"subject": lab}
        if t.resolver == "decision":
            vals.update(_decision_values(sid))
        return ref, params, vals
    if t.resolver == "curated":
        raise UnknownSubject(f"aucun modèle de dossier {t.kind} pour {sid!r} (sujets décrits : {', '.join(t.subjects)})")
    if t.resolver == "decision":
        from reva.predict.decision import reference_decisions

        d = reference_decisions().get(sid)
        if d is None:
            raise UnknownSubject(f"décision de référence {sid!r} inconnue")
        dd = d.model_dump(mode="json")
        ref = M.SubjectRef(kind=t.kind, id=sid, label=dd["label_fr"], sublabel=dd["instrument"])
        return ref, {"id": sid, "reference_id": sid, "countries": dd.get("countries") or []}, {"subject": label_val(dd["label_fr"], reg_src), **_decision_values(sid)}
    # KG-backed generic subjects
    from reva.kg import query as kq

    if not kq.exists(ctx.conn, sid):
        raise UnknownSubject(f"entité {sid!r} absente du graphe de connaissances")
    ent = kq.entity(ctx.conn, sid, origin=ctx.origin) or {}
    labels = kq.labels_for(ctx.conn, [sid]).get(sid, {})
    label = labels.get("fr") or labels.get("en") or sid
    kind = ent.get("kind") or "entity"
    ksrc = Src(source=str((ent.get("provenance") or {}).get("source_key") or "kg"), title="Graphe de connaissances REVA", ref=sid, licence=None)
    params: dict[str, Any] = {"id": sid, "ids": [sid]}
    params.update(_subst(t.generic, params))
    if t.resolver == "chembl_drug":
        params["ids"] = _drug_ids(ctx, sid)
    if t.resolver == "ucdp_conflict":
        params["countries"] = _conflict_countries(ctx, sid)
        params["events"] = []
    ref = M.SubjectRef(kind=t.kind, id=sid, label=label, sublabel=kind, entity_href=f"/v8/fiche/{kind}/{sid}")
    return ref, params, {"subject": label_val(label, ksrc)}


def _decision_values(sid: str) -> dict[str, Item]:
    return {}


def _drug_ids(ctx: Ctx, sid: str) -> list[str]:
    """The ChEMBL parent, its salts / forms (chembl:parent_molecule) and eEML records naming it."""
    from reva.kg import query as kq

    ids = [sid]
    res = kq.neighbours(ctx.conn, sid, direction="both", properties=["chembl:parent_molecule", "eml:chembl_molecule"], limit=50, origin=ctx.origin)
    for e in res["edges"]:
        if e["property"]["pid"] == "chembl:parent_molecule" and e["direction"] == "out" and e["object"]:
            ids.insert(0, e["object"]["qid"])  # sid is a salt: its parent comes first
        elif e["direction"] == "in":
            ids.append(e["subject"]["qid"])
    seen: list[str] = []
    for i in ids:
        if i not in seen:
            seen.append(i)
    if seen[0] != sid and seen[0].startswith("chembl:"):
        res2 = kq.neighbours(ctx.conn, seen[0], direction="in", properties=["chembl:parent_molecule", "eml:chembl_molecule"], limit=50, origin=ctx.origin)
        for e in res2["edges"]:
            if e["subject"]["qid"] not in seen:
                seen.append(e["subject"]["qid"])
    return seen


def _conflict_countries(ctx: Ctx, sid: str) -> list[str]:
    from reva.kg import query as kq

    inv = {q: iso for iso, q in _country_qids().items()}
    res = kq.neighbours(ctx.conn, sid, direction="out", properties=["ucdp:location"], limit=200, origin=ctx.origin)
    out: list[str] = []
    for e in res["edges"]:
        iso = inv.get((e.get("object") or {}).get("qid") or "")
        if iso and iso not in out:
            out.append(iso)
    return out


# ============================================================================== generation
def _subst(v: Any, params: Mapping[str, Any]) -> Any:
    if isinstance(v, str) and v.startswith("$"):
        k = v[1:]
        if k not in params:
            raise TemplateError(f"unknown subject parameter {v}")
        return params[k]
    if isinstance(v, list):
        out: list[Any] = []
        for x in v:
            y = _subst(x, params)
            out.extend(y if isinstance(x, str) and x.startswith("$") and isinstance(y, list) else [y])
        return out
    if isinstance(v, dict):
        return {k: _subst(x, params) for k, x in v.items()}
    return v


def _run_block(ctx: Ctx, b: BlockT, params: Mapping[str, Any]) -> Result:
    fn = QUERIES.get(b.query)
    if fn is None:
        raise TemplateError(f"unknown query {b.query}")
    kw = _subst(b.params, params)
    t0 = time.perf_counter()
    try:
        return fn(ctx, **kw)
    except TemplateError:
        raise
    except Exception as e:  # noqa: BLE001 - a failing source is stated as missing, never hidden or invented
        log.warning("dossier query %s failed: %s", b.query, e)
        try:
            ctx.conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return Result(empty_reason=f"source indisponible lors du calcul ({type(e).__name__})")
    finally:
        ctx.timings[f"{b.id}:{b.query}"] = round((time.perf_counter() - t0) * 1000, 1)


def _render_block(
    r: Renderer, sec_id: str, b: BlockT, res: Result, not_covered: list[M.NotCovered], sec_title: str
) -> list[M.Block]:
    out: list[M.Block] = []
    base = f"{sec_id}.{b.id}"
    if res.empty_reason is not None and not res.values and not res.rows:
        out.append(M.MissingBlock(id=base, template_id=f"{base}.empty", what=b.empty.what, reason=res.empty_reason))
        not_covered.append(M.NotCovered(what=b.empty.what, reason=res.empty_reason, section=sec_title))
        return out
    sentences: list[M.Sentence] = []
    for s in b.sentences:
        sent, miss = r.sentence(f"{base}.{s.id}", s.text, res.values)
        if not miss:
            sentences.append(sent)
            continue
        reason = "; ".join(dict.fromkeys(m.reason for m in miss))
        what = s.what or b.empty.what
        if s.missing:
            ms, _ = r.sentence(f"{base}.{s.id}.missing", s.missing, {**res.values, **{k: v for k, v in res.values.items()}})
            sentences.append(ms)
        else:
            if sentences:
                out.append(M.ParagraphBlock(id=f"{base}.p{len(out)}", sentences=sentences))
                sentences = []
            out.append(M.MissingBlock(id=f"{base}.{s.id}", template_id=f"{base}.{s.id}", what=what, reason=reason))
        not_covered.append(M.NotCovered(what=what, reason=reason, section=sec_title))
    if sentences:
        out.append(M.ParagraphBlock(id=f"{base}.p{len(out)}", sentences=sentences))
    if b.chart and res.series:
        series = [
            M.ChartSeries(
                label=s.label,
                kind=s.kind,  # type: ignore[arg-type]
                fn=r.notes.fn(s.src),
                points=[M.ChartPoint(x=x, y=y, exact=ex, lo=lo, hi=hi) for x, y, ex, lo, hi in s.points],
            )
            for s in res.series
        ]
        xk = res.series[0].x_kind
        out.append(
            M.ChartBlock(
                id=f"{base}.chart",
                template_id=f"{base}.chart",
                chart=b.chart.type,  # type: ignore[arg-type]
                title=b.chart.title,
                unit=res.series[0].unit,
                x_kind=xk if xk in ("year", "month", "quarter", "category") else "year",  # type: ignore[arg-type]
                series=series,
                note=b.chart.note,
            )
        )
    if b.table and res.rows:
        rows = []
        for row in res.rows:
            cells = []
            for c in b.table.columns:
                parts, _ = r.parts(c.cell, row)
                cells.append(M.Cell(parts=parts))
            rows.append(cells)
        out.append(
            M.TableBlock(
                id=f"{base}.table",
                template_id=f"{base}.table",
                caption=b.table.caption,
                columns=[c.header for c in b.table.columns],
                numeric=[c.numeric for c in b.table.columns],
                rows=rows,
                note=b.table.note,
            )
        )
    if b.list_ and res.rows:
        items = []
        for row in res.rows:
            it, miss = r.sentence(f"{base}.item", b.list_.item, row)
            if miss and b.list_.item_missing:
                it, _ = r.sentence(f"{base}.item_missing", b.list_.item_missing, row)
            items.append(it)
        out.append(M.ListBlock(id=f"{base}.list", template_id=f"{base}.item", title=b.list_.title, items=items, more=res.more))
    if (b.table or b.list_) and not res.rows:
        reason = res.empty_reason or "aucune ligne"
        out.append(M.MissingBlock(id=f"{base}.rows", template_id=f"{base}.empty", what=b.empty.what, reason=reason))
        not_covered.append(M.NotCovered(what=b.empty.what, reason=reason, section=sec_title))
    if b.missing_list:
        for m in res.missing:
            out.append(M.MissingBlock(id=f"{base}.m{len(out)}", template_id=f"{base}.empty", what=m.what, reason=m.reason))
            not_covered.append(M.NotCovered(what=m.what, reason=m.reason, section=sec_title))
    elif res.missing:
        for m in res.missing:
            not_covered.append(M.NotCovered(what=m.what, reason=m.reason, section=sec_title))
    return out


def build_document(tpl: Template, ctx: Ctx, ref: M.SubjectRef, params: dict[str, Any], svals: dict[str, Item], t0: float) -> M.Document:
    t = tpl.t
    notes = Notes()
    r = Renderer(notes, svals, t.vocab)
    not_covered: list[M.NotCovered] = []
    sections: list[M.Section] = []
    for sec in t.sections:
        blocks: list[M.Block] = []
        for b in sec.blocks:
            if b.when and not params.get(b.when):
                continue
            res = _run_block(ctx, b, params)
            r.blocks[b.id] = res
            blocks.extend(_render_block(r, sec.id, b, res, not_covered, sec.title))
        sections.append(M.Section(id=sec.id, title=sec.title, lede=sec.lede, blocks=blocks))
    summary = []
    for s in t.summary:
        sent, miss = r.sentence(f"summary.{s.id}", s.text, {})
        if not miss:
            summary.append(sent)
        elif s.missing:
            summary.append(r.sentence(f"summary.{s.id}.missing", s.missing, {})[0])
    for nc in t.not_covered:
        not_covered.append(M.NotCovered(what=nc.what, reason=nc.reason, section=None))
    title, _ = r.sentence("title", t.title, {})
    subtitle, _ = r.sentence("subtitle", t.subtitle, {})
    fns = notes.list()
    lic: dict[tuple[str, str], dict[str, Any]] = {}
    for f in fns:
        if f.licence:
            lic.setdefault((f.source, f.licence), {"source": f.source, "title": f.title, "licence": f.licence, "restricted": f.restricted, "non_commercial": f.non_commercial, "url": f.licence_url})
    licences = sorted(lic.values(), key=lambda x: (not x["restricted"], x["title"], x["licence"]))
    n_sent = len(summary) + sum(len(b.sentences) for s in sections for b in s.blocks if isinstance(b, M.ParagraphBlock))
    n_sent += sum(len(b.items) for s in sections for b in s.blocks if isinstance(b, M.ListBlock))
    n_vals = _count_values(summary, sections)
    n_miss = sum(1 for s in sections for b in s.blocks if isinstance(b, M.MissingBlock))
    return M.Document(
        schema="reva-dossier/1",
        kind=t.kind,
        subject=ref,
        title=title.text,
        subtitle=subtitle.text,
        summary=summary,
        sections=sections,
        not_covered=_dedupe(not_covered),
        footnotes=fns,
        licences=licences,
        honesty=list(t.honesty),
        meta=M.DocumentMeta(
            template=f"registry/dossier/{tpl.path.name}",
            template_version=tpl.version,
            snapshot_id=ctx.snapshot_id,
            atlas_snapshot=ctx.atlas_sha,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            compute_ms=round((time.perf_counter() - t0) * 1000, 1),
            origin=ctx.origin,
            sentences=n_sent,
            values=n_vals,
            missing=n_miss,
            timings_ms=dict(ctx.timings),
        ),
    )


def _count_values(summary: list[M.Sentence], sections: list[M.Section]) -> int:
    n = sum(1 for s in summary for p in s.parts if p.t == "value")
    for sec in sections:
        for b in sec.blocks:
            if isinstance(b, M.ParagraphBlock):
                n += sum(1 for s in b.sentences for p in s.parts if p.t == "value")
            elif isinstance(b, M.ListBlock):
                n += sum(1 for s in b.items for p in s.parts if p.t == "value")
            elif isinstance(b, M.TableBlock):
                n += sum(1 for row in b.rows for c in row for p in c.parts if p.t == "value")
            elif isinstance(b, M.ChartBlock):
                n += sum(len(s.points) for s in b.series)
    return n


def _dedupe(xs: list[M.NotCovered]) -> list[M.NotCovered]:
    seen = set()
    out = []
    for x in xs:
        k = (x.what, x.reason)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


# ============================================================================== entry point
def generate(
    engine_connect: Callable[[], Any],
    store: Any,
    kind: str,
    sid: str,
    *,
    origin: str = "live",
    use_cache: bool = True,
    atlas: Any = "auto",
) -> M.Document:
    """Generate (or serve from cache) the brief ``kind``/``sid``. ``engine_connect`` opens a read connection."""
    t0 = time.perf_counter()
    tpl = load_template(kind)
    if tpl is None:
        raise UnknownSubject(f"aucun modèle de dossier pour le type {kind!r}")
    with engine_connect() as conn:
        atlas_sha, atlas_q = None, None
        if atlas == "auto":
            try:
                from reva.atlas.query import query_for, request_snapshot

                snap, _ = request_snapshot(conn, store, origin=origin)
                atlas_sha = snap.sha256
                atlas_q = query_for(snap, store, build=False)
            except Exception as e:  # noqa: BLE001 - without an atlas file the atlas sections are stated missing
                log.warning("dossier: atlas unavailable: %s", e)
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
        elif atlas is not None:
            atlas_q = atlas
            atlas_sha = getattr(getattr(atlas, "snapshot", None), "sha256", None)
        snap_id = data_snapshot_id(conn, atlas_sha, origin)
        ctx = Ctx(conn=conn, store=store, origin=origin, snapshot_id=snap_id, atlas_sha=atlas_sha, atlas_q=atlas_q)
        ref, params, svals = resolve_subject(tpl, sid, ctx)
        key = cache_key(kind, ref.id, tpl.version, snap_id, origin)
        if use_cache:
            hit = _cache_get(key)
            if hit is not None:
                doc = M.Document.model_validate(hit)
                doc.meta.cached = True
                doc.meta.compute_ms = round((time.perf_counter() - t0) * 1000, 1)
                return doc
        ctx.subject = params
        doc = build_document(tpl, ctx, ref, params, svals, t0)
    if use_cache:
        _cache_put(key, doc.model_dump(mode="json", by_alias=True))
    return doc


def cache_key(kind: str, sid: str, version: str, snap: str, origin: str) -> str:
    return hashlib.sha256(json.dumps([kind, sid, version, snap, origin]).encode()).hexdigest()[:24]


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = CACHE.get(key)
    if hit is not None:
        return hit  # type: ignore[no-any-return]
    p = DISK_CACHE / f"{key}.json"
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    CACHE.put(key, data)
    return data  # type: ignore[no-any-return]


def _cache_put(key: str, data: dict[str, Any]) -> None:
    CACHE.put(key, data)
    try:
        DISK_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = DISK_CACHE / f".{key}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(DISK_CACHE / f"{key}.json")
    except OSError:  # pragma: no cover - a read-only temp dir only costs recomputation
        pass


__all__ = ["generate", "build_document", "TemplateError", "UnknownSubject", "SentenceT"]
