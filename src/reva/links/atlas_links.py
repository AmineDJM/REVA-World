"""Country–country links from one atlas snapshot (DuckDB over the snapshot's pinned Parquet; agent LINKS2).

Two families, both declared in ``registry/links/link_types.yaml``:

* **atlas_dyadic** — a provider reports a quantity per (country, partner, year): IMF IMTS exports / imports,
  UN DESA migrant stock, CEPII BACI by HS section, Natural Earth shared boundary length; A-INFRA's physical
  networks (LINKS3): Wikidata submarine cables, OpenFlights June-2014 routes, GCAT payloads by launch-site state.
  One link per reported cell, weighted by the cell's own decimal string and unit; evidence = the cell's raw
  sha256 + selector (+ companion rows such as the cables' first service year, each cited). A missing cell gives
  no link; a reported zero gives no link either (counted as ``reported_zero``). A provider geography outside the
  crosswalk gives no link, except through an explicit mapping hop (:mod:`reva.links.geo_hops`) whose KG
  statement is then cited on the link. A type's ``caveat`` and ``licence`` are stored in every link's basis.
* **atlas_computed** — ``unga_vote_agreement``: the share of identical votes of two states over the roll calls
  both voted on (yes / no / abstain), per decade (stored) or per year (pair view), per issue. The index is
  computed by REVA (labelled ``derived_index``, formula and counts in ``basis``); its evidence is the raw
  artifact of the votes plus a selector that states the computation (pair, period, issue), so the number can
  be recomputed from the archived file. It is not an alliance, friendship or alignment of interests.

Reads are bounded: the materialiser keeps, per (country, year, facet) and direction, the top ``k`` partners
(the union of both endpoints' top-k, so every country's top-k is complete); the pair view reads every year of
one pair (row cap); the country view reads one country (row cap).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from reva.atlas.query import iso_date_sql
from reva.links.model import Evidence, Link, LinkError, Temporal
from reva.links.taxonomy import LinkType, Taxonomy, load_taxonomy

VOTE_METHOD = "links-vote-agreement/1"
VOTE_UNIT = "share of identical votes (0-1)"
VOTE_ISSUES = ("co", "hr", "me", "nu", "di", "ec")
VOTE_ISSUE_LABELS = {
    "all": "all roll calls",
    "co": "colonialism",
    "hr": "human rights",
    "me": "Palestinian conflict",
    "nu": "nuclear weapons and nuclear material",
    "di": "arms control and disarmament",
    "ec": "economic development",
}
VOTE_POSITIONS = ("yes", "no", "abstain")
MIN_COMMON_ROLLCALLS = 10
VOTE_FORMULA = (
    "n_identical / n_common; n_common = roll calls on which both cast yes, no or abstain (absent and "
    "non-member excluded); identical = same position (abstain = abstain counts)"
)
VOTE_CAVEAT = (
    "Computed by REVA from Voeten et al. roll-call votes; a share of identical votes, not an alliance, "
    "friendship or alignment of interests. Issue codes are Voeten's keyword-based classification; roll-call "
    "selection (amendments, paragraph votes) and absences shape the share."
)
#: VOTE-3 (review V3b): what the index is *not* — numbers are not comparable with the literature's agreement index.
VOTE_INDEX_NOTE = (
    "Strict share of identical positions: yes/yes, no/no and abstain/abstain count 1, any other pair of "
    "positions (including abstain against yes or no) counts 0. This is not Voeten/Bailey's 'agree' index, which "
    "gives half credit when one state abstains (1 identical, 0.5 one abstains, 0 opposite); REVA's values are "
    "therefore lower and not comparable with published 'agree' values."
)
#: link types stored in full (small tables; a top-k would drop real neighbours: China has 14+ land borders);
#: atlas specs may also say ``stored_in_full: true`` (LINKS3: cables, 2014 air routes, launch services)
STORED_IN_FULL = frozenset({"land_border"})
#: A-INFRA folds a partner outside the crosswalk as ``|partner_code=<provider code>`` (flags.partner_code)
PARTNER_CODE_DIM = "partner_code"
DEFAULT_FACETS = {"unga_vote_agreement": "issue=all", "bilateral_trade_baci": "section=TOTAL"}
#: flags keys dropped from a link's basis (long provider text repeated on every row)
_LONG_FLAG_KEYS = frozenset({"definition", "method", "notes", "typing", "data_type_meaning"})

NodeOf = Callable[[str], str]


def stored_in_full(lt: LinkType) -> bool:
    return lt.id in STORED_IN_FULL or bool((lt.atlas or {}).get("stored_in_full"))


def _int_or_str(v: Any) -> Any:
    t = str(v)
    return int(t) if t.lstrip("-").isdigit() else t


def _cable_note(flags: Mapping[str, Any]) -> str | None:
    """Evidence gloss of a Wikidata cable row: the cable items it counts (label, QID, revision)."""
    cs = flags.get("cables")
    if not isinstance(cs, list) or not cs:
        return None
    parts = [f"{c.get('label')} ({c.get('qid')}, rev {c.get('rev')})" for c in cs if isinstance(c, Mapping)]
    out = "Wikidata cable items: " + "; ".join(parts)
    total, listed = flags.get("cables_total"), flags.get("cables_listed")
    if isinstance(total, int) and isinstance(listed, int) and total > listed:
        out += f"; +{total - listed} more (every QID is in the selector)"
    return out if len(out) <= 1200 else out[:1190] + "…"


def _default_node(iso3: str) -> str:
    return f"iso3:{iso3}"


@dataclass
class AtlasDerivation:
    link_type: str
    links: list[Link] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    considered: int = 0
    #: rows attached to a country through a cited mapping hop, per provider code (LINKS3)
    mapped_via_hop: Counter[str] = field(default_factory=Counter)

    def to_json(self) -> dict[str, Any]:
        out = {
            "link_type": self.link_type,
            "count": len(self.links),
            "considered": self.considered,
            "skipped": dict(self.skipped),
        }
        if self.mapped_via_hop:
            out["mapped_via_hop"] = dict(self.mapped_via_hop)
        return out


def explain_basis(link_type: str, basis: Mapping[str, Any], taxonomy: Taxonomy | None = None) -> dict[str, Any]:
    """The constant wording a stored basis leaves out (kept once here, not on every row): the vote-agreement
    index, formula, issue label and caveat; the flow of a dyadic atlas kind."""
    out = dict(basis)
    if link_type == "unga_vote_agreement":
        out.setdefault("index", "share of identical votes")
        out.setdefault("index_note", VOTE_INDEX_NOTE)
        out.setdefault("formula", VOTE_FORMULA)
        out.setdefault("issue_label", VOTE_ISSUE_LABELS.get(str(basis.get("issue")), basis.get("issue")))
        out.setdefault("caveat", VOTE_CAVEAT)
    elif basis.get("method") == "atlas_dyadic":
        lt = (taxonomy or load_taxonomy()).link_types.get(link_type)
        if lt is not None and lt.atlas:
            out.setdefault("flow", lt.atlas.get("flow"))
    return out


def _short_flags(flags: Any) -> dict[str, Any]:
    if isinstance(flags, str):
        try:
            flags = json.loads(flags)
        except ValueError:
            return {}
    if not isinstance(flags, Mapping):
        return {}
    out: dict[str, Any] = {}
    for k, v in flags.items():
        if k in _LONG_FLAG_KEYS or v is None:
            continue
        if isinstance(v, str) and len(v) > 200:
            v = v[:200] + "…"
        out[k] = v
    return out


def _sql_str(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


class AtlasLinkReader:
    """Reads dyadic and vote links from one snapshot's DuckDB file.

    ``con`` is a DuckDB connection (read-only) to the file built by :func:`reva.atlas.query.build_snapshot_db`;
    ``snapshot_sha256`` / ``origin`` name the snapshot (evidence origin and basis)."""

    def __init__(
        self,
        rows: Callable[[str, Sequence[Any]], list[dict[str, Any]]],
        *,
        snapshot_sha256: str,
        origin: str,
        taxonomy: Taxonomy | None = None,
        node_of: NodeOf = _default_node,
        hop_evidence: Mapping[str, Evidence] | None = None,
    ) -> None:
        self._rows = rows
        self.snapshot_sha256 = snapshot_sha256
        self.origin = origin
        self.taxonomy = taxonomy or load_taxonomy()
        self.node_of = node_of
        #: provider code -> evidence (KG statement) of its mapping hop; a hop without evidence is never applied
        self.hop_evidence: Mapping[str, Evidence] = dict(hop_evidence or {})
        self._present: set[tuple[str, str]] | None = None
        self._vote_span: tuple[int, int] | None = None

    @classmethod
    def of_query(cls, q: Any, **kw: Any) -> AtlasLinkReader:
        """From a :class:`reva.atlas.query.AtlasQuery` (its thread-safe cursor helper)."""
        return cls(
            lambda sql, params: q._rows(sql, params), snapshot_sha256=q.snapshot.sha256, origin=q.snapshot.origin, **kw
        )

    # ------------------------------------------------------------------ catalogue
    def present(self) -> set[tuple[str, str]]:
        """(source, dataset) pairs present in the snapshot."""
        if self._present is None:
            self._present = {
                (r["source"], r["dataset"]) for r in self._rows("SELECT DISTINCT source, dataset FROM series", ())
            }
        return self._present

    def dyadic_types(self) -> list[LinkType]:
        return [lt for lt in self.taxonomy.link_types.values() if lt.method == "atlas_dyadic" and lt.atlas]

    def available(self, lt: LinkType) -> bool:
        if lt.method == "atlas_computed":
            return ("unga", "votes") in self.present() and ("unga", "rollcalls") in self.present()
        spec = lt.atlas
        if not spec:
            return False
        return any((spec["source"], d) in self.present() for d in spec["datasets"])

    # ------------------------------------------------------------------ dyadic
    def _hops(self, lt: LinkType) -> tuple[dict[str, Any], list[str]]:
        """(provider code -> hop applied for this type, registry codes applicable but without KG evidence)."""
        spec = lt.atlas or {}
        if not spec.get("mapping_hops"):
            return {}, []
        from reva.links.geo_hops import load_hops

        active, missing = {}, []
        for h in load_hops():
            if not h.applies(spec["source"], spec["datasets"]):
                continue
            if h.provider_code in self.hop_evidence:
                active[h.provider_code] = h
            else:
                missing.append(h.provider_code)
        return active, sorted(missing)

    def _dyadic_base(self, lt: LinkType) -> str:
        """CTE ``r``: every cell of the type's series with aliases row_iso3, partner, year, value, facet, v
        (+ row_geo, partner_code, hop, and comp_<key>[_sha|_sel] for companion rows)."""
        spec = lt.atlas
        assert spec is not None
        partner_key = spec["partner"].split(".", 1)[1]
        datasets = ", ".join(_sql_str(d) for d in spec["datasets"])
        prefix = spec["indicator_prefix"]
        name = prefix.split("|", 1)[0]
        active, _missing = self._hops(lt)
        # partners outside the crosswalk folded as |partner_code=<code> (A-INFRA): read too, so that they are
        # counted as partner_unmapped (or mapped through a cited hop), never silently left out of the report
        sel = (
            f"(starts_with(indicator, {_sql_str(prefix)})"
            f" OR starts_with(indicator, {_sql_str(name + '|' + PARTNER_CODE_DIM + '=')}))"
        )
        req = ""
        for k, v in sorted((spec.get("require_dims") or {}).items()):
            pat = "\\|" + k + "=" + v + "(\\||$)"
            req += f" AND regexp_matches(indicator, {_sql_str(pat)})"
        facet_parts = []
        for dim in spec.get("facet_dims") or ():
            pat = "\\|" + dim + "=([^|]+)"
            facet_parts.append(f"'{dim}=' || coalesce(regexp_extract(s.indicator, {_sql_str(pat)}, 1), '')")
        facet = " || ',' || ".join(facet_parts) if facet_parts else "''"
        raw_partner = f"json_extract_string(c.flags, '$.{partner_key}')"
        pcode = f"json_extract_string(c.flags, '$.{PARTNER_CODE_DIM}')"
        row_iso, partner, hop = "c.iso3", raw_partner, "CAST(NULL AS VARCHAR)"
        if active:
            geo_case = " ".join(f"WHEN {_sql_str(c)} THEN {_sql_str(h.iso3)}" for c, h in sorted(active.items()))
            in_codes = ", ".join(_sql_str(c) for c in sorted(active))
            row_iso = f"coalesce(c.iso3, CASE c.provider_geo {geo_case} END)"
            partner = f"coalesce({raw_partner}, CASE {pcode} {geo_case} END)"
            hop = (
                f"CASE WHEN c.iso3 IS NULL AND c.provider_geo IN ({in_codes}) THEN 'row:' || c.provider_geo "
                f"WHEN {raw_partner} IS NULL AND {pcode} IN ({in_codes}) THEN 'partner:' || {pcode} END"
            )
        if spec.get("year_from") == "period_start":
            year = "CAST(year(c.period_start) AS INTEGER)"
        else:
            year = """coalesce(c.year, CASE WHEN datediff('day', c.period_start, c.period_end) <= 1
                                           THEN CAST(year(c.period_start) AS INTEGER) END)"""
        comps = sorted((spec.get("companions") or {}).items())
        comp_cte, comp_cols, comp_join = "", "", ""
        if comps:
            starts = " OR ".join(f"starts_with(indicator, {_sql_str(cn + '|')})" for _, cn in comps)
            comp_cte = f""",
        ks AS (SELECT sid, dataset, indicator FROM series
                WHERE source = {_sql_str(spec["source"])} AND dataset IN ({datasets}) AND ({starts})),
        kc AS (SELECT ks.dataset, ks.indicator, c.provider_geo, c.period_start, c.value, c.raw_sha256, c.selector
                 FROM cells_s c JOIN ks USING (sid))"""
            for i, (key, cn) in enumerate(comps):
                a = f"k{i}"
                comp_cols += (
                    f", {a}.value AS comp_{key}, {a}.raw_sha256 AS comp_{key}_sha, {a}.selector AS comp_{key}_sel"
                )
                comp_join += (
                    f" LEFT JOIN kc {a} ON {a}.provider_geo = c.provider_geo AND {a}.dataset = s.dataset"
                    f" AND {a}.period_start = c.period_start"
                    f" AND {a}.indicator = {_sql_str(cn)} || substr(s.indicator, {len(name) + 1})"
                )
        return f"""
        s AS (SELECT sid, dataset, indicator FROM series
               WHERE source = {_sql_str(spec["source"])} AND dataset IN ({datasets})
                 AND {sel}{req}){comp_cte},
        r AS (SELECT {row_iso} AS row_iso3, {partner} AS partner,
                     c.provider_geo AS row_geo, {pcode} AS partner_code, {hop} AS hop,
                     {year} AS year,
                     c.value, c.unit, c.flags, c.raw_sha256, c.selector, c.table_sha, s.dataset, s.indicator,
                     {facet} AS facet, TRY_CAST(c.value AS DOUBLE) AS v{comp_cols}
                FROM cells_s c JOIN s USING (sid){comp_join})"""

    def _dyadic_sql(self, lt: LinkType, where: str, top_k: int | None, cap: int, order: str = "pair") -> str:
        spec = lt.atlas
        assert spec is not None
        subj, obj = ("row_iso3", "partner") if spec["row_role"] == "subject" else ("partner", "row_iso3")
        rank, keep = "", ""
        if top_k is not None:
            rank = f""", row_number() OVER (PARTITION BY {subj}, year, facet ORDER BY v DESC, {obj}) AS rk_s
                       , row_number() OVER (PARTITION BY {obj}, year, facet ORDER BY v DESC, {subj}) AS rk_o"""
            keep = f"WHERE rk_s <= {int(top_k)} OR rk_o <= {int(top_k)}"
        return f"""
        WITH {self._dyadic_base(lt).strip()},
        f AS (SELECT * FROM r
               WHERE year IS NOT NULL AND value IS NOT NULL AND row_iso3 IS NOT NULL AND partner IS NOT NULL
                 AND length(partner) = 3 AND partner <> row_iso3 AND v IS NOT NULL AND v <> 0 AND ({where})),
        k AS (SELECT *, {subj} AS subj, {obj} AS obj {rank} FROM f)
        SELECT * FROM k {keep}
        ORDER BY {"year DESC, v DESC, subj, obj, facet" if order == "weight" else "subj, obj, year, facet"}
        LIMIT {int(cap) + 1}"""

    def _dyadic_counts(self, lt: LinkType, where: str) -> Counter[str]:
        _active, missing = self._hops(lt)
        no_ev = "0"
        if missing:
            codes = ", ".join(_sql_str(c) for c in missing)
            no_ev = f"count(*) FILTER (WHERE (row_iso3 IS NULL AND row_geo IN ({codes})) OR (partner IS NULL AND partner_code IN ({codes})))"
        rows = self._rows(
            f"""WITH {self._dyadic_base(lt).strip()}
                SELECT count(*) AS cells,
                       count(*) FILTER (WHERE value IS NULL) AS missing_value,
                       count(*) FILTER (WHERE row_iso3 IS NULL) AS iso3_unmapped,
                       count(*) FILTER (WHERE partner IS NULL) AS partner_unmapped,
                       count(*) FILTER (WHERE v = 0) AS reported_zero,
                       count(*) FILTER (WHERE year IS NULL) AS no_calendar_year,
                       {no_ev} AS mapping_hop_without_kg_evidence
                  FROM r WHERE ({where.replace("partner", "coalesce(partner, '')").replace("row_iso3", "coalesce(row_iso3, '')")})""",
            (),
        )
        return Counter({k: int(v) for k, v in rows[0].items() if v}) if rows else Counter()

    def _row_link(self, lt: LinkType, r: Mapping[str, Any]) -> Link:
        spec = lt.atlas
        assert spec is not None
        sel = r["selector"] if isinstance(r["selector"], str) else json.dumps(r["selector"], sort_keys=True)
        fl = _short_flags(r["flags"])
        ev = Evidence(
            kind="atlas_row",
            source=spec["source"],
            raw_sha256=r["raw_sha256"],
            selector=sel,
            subject=r["row_iso3"],
            object=r["partner"],
            pid=r["indicator"],
            origin=self.origin,
            note=_cable_note(fl),
        )
        evs = [ev]
        basis: dict[str, Any] = {
            "method": "atlas_dyadic",
            "source": spec["source"],
            "dataset": r["dataset"],
            "indicator": r["indicator"],
            "reported_by": r["row_iso3"],
            "iso3": [r["subj"], r["obj"]],
            "table_artifact_sha256": r["table_sha"],
        }
        if fl:
            basis["source_flags"] = fl
        if r.get("facet"):
            basis["facet"] = r["facet"]
        name = spec["indicator_prefix"].split("|", 1)[0]
        for key, cn in sorted((spec.get("companions") or {}).items()):
            v = r.get(f"comp_{key}")
            if v is None or not r.get(f"comp_{key}_sha") or not r.get(f"comp_{key}_sel"):
                continue
            basis[key] = _int_or_str(v)
            csel = r[f"comp_{key}_sel"]
            evs.append(
                Evidence(
                    kind="atlas_row",
                    source=spec["source"],
                    raw_sha256=r[f"comp_{key}_sha"],
                    selector=csel if isinstance(csel, str) else json.dumps(csel, sort_keys=True),
                    subject=r["row_iso3"],
                    object=r["partner"],
                    pid=cn + str(r["indicator"])[len(name) :],
                    origin=self.origin,
                )
            )
        if r.get("hop"):
            code = str(r["hop"]).split(":", 1)[1]
            active, _missing = self._hops(lt)
            hev = self.hop_evidence[code]
            evs.append(hev)
            basis["mapping_hop"] = {**active[code].basis(hev), "side": str(r["hop"]).split(":", 1)[0]}
        if spec.get("caveat"):
            basis["caveat"] = dict(spec["caveat"])
        if spec.get("licence"):
            basis["licence"] = spec["licence"]
        return Link(
            self.node_of(r["subj"]),
            self.node_of(r["obj"]),
            lt.id,
            lt.directed,
            Temporal("snapshot", year=int(r["year"])),
            tuple(evs),
            weight=str(r["value"]),
            unit=r["unit"],
            basis=basis,
        )

    @staticmethod
    def _variant(r: Mapping[str, Any]) -> frozenset[str]:
        """The provider pair a row reports (geography codes as the provider wrote them)."""
        return frozenset({str(r["row_geo"]), str(r.get("partner_code") or r["partner"])})

    def _union(
        self, lt: LinkType, rs: Sequence[Mapping[str, Any]], links: Sequence[Link], d: AtlasDerivation
    ) -> tuple[str, dict[str, Any]] | None:
        """Rows reaching one pair from different provider geographies (through a mapping hop): the union of the
        items their selectors list (``union_selector_key``); None when a row's list is not complete."""
        spec = lt.atlas or {}
        key = spec["union_selector_key"]
        items: set[str] = set()
        for r in rs:
            try:
                lst = json.loads(r["selector"] if isinstance(r["selector"], str) else json.dumps(r["selector"]))[key]
            except (ValueError, KeyError, TypeError):
                lst = None
            if not isinstance(lst, list) or len(lst) != int(Decimal(str(r["value"]))):
                d.skipped["union_selector_list_incomplete"] += 1
                return None
            items |= {str(x) for x in lst}
        extra: dict[str, Any] = {
            "combined_from": sorted(sorted(v) for v in {self._variant(r) for r in rs}),
            "combination": f"union of the items listed by the rows' selectors ({key}); "
            f"{len(items)} distinct of {sum(int(Decimal(str(r['value']))) for r in rs)} listed",
            "items": sorted(items),
        }
        for ck in spec.get("companions") or {}:
            vals = [lk.basis[ck] for lk in links if isinstance(lk.basis.get(ck), int)]
            if ck.endswith("_year") and vals:
                extra[ck] = min(vals)  # the earliest year of the union is the earliest of the rows' earliest
        return str(len(items)), extra

    def _merge_undirected(self, lt: LinkType, rows: Iterable[Mapping[str, Any]], d: AtlasDerivation) -> None:
        """Both directions of an undirected measure (NE border length, shared cables) → one link with both rows as
        evidence; rows that disagree on the value give no link (``directions_disagree``). With a mapping hop, rows
        of different provider geographies may reach one pair: they are combined by the union of their listed items
        (``union_selector_key``), never summed; a type without that key gives no link then (counted)."""
        groups: dict[tuple[str, str, int, str], list[Mapping[str, Any]]] = defaultdict(list)
        for r in rows:
            a, b = sorted((r["row_iso3"], r["partner"]))
            groups[(a, b, int(r["year"]), r.get("facet") or "")].append(r)
        union_key = (lt.atlas or {}).get("union_selector_key")
        for (a, b, _y, _f), rs in sorted(groups.items()):
            links = [self._row_link(lt, r) for r in rs]
            first = links[0]
            basis = {k: v for k, v in first.basis.items() if k != "mapping_hop"}
            weight = first.weight
            # several provider pairs reach one country pair only through a mapping hop
            variants = {self._variant(r) for r in rs} if any(r.get("hop") for r in rs) else {frozenset()}
            if len(variants) > 1:
                if not union_key:
                    d.skipped["mapping_hop_collides_with_direct_row"] += 1
                    continue
                u = self._union(lt, rs, links, d)
                if u is None:
                    continue
                weight, extra = u
                basis.update(extra)
            elif len({Decimal(str(r["value"])) for r in rs}) > 1:
                d.skipped["directions_disagree"] += 1
                continue
            seen: set[str] = set()
            ev = []
            for lk in links:
                for e in lk.evidence:
                    k = json.dumps(e.to_json(), sort_keys=True)
                    if k not in seen:
                        seen.add(k)
                        ev.append(e)
            hops = [lk.basis["mapping_hop"] for lk in links if "mapping_hop" in lk.basis]
            if hops:
                basis["mapping_hop"] = {k: v for k, v in hops[0].items() if k != "side"}
            na, nb = sorted((first.subject, first.object))
            basis["iso3"] = [a, b]
            basis["reported_by"] = sorted({r["row_iso3"] for r in rs})
            d.links.append(
                Link(na, nb, lt.id, False, first.temporal, tuple(ev), weight=weight, unit=first.unit, basis=basis)
            )

    def dyadic(
        self,
        lt: LinkType,
        *,
        where: str = "TRUE",
        top_k: int | None = None,
        cap: int = 2_000_000,
        order: str = "pair",
    ) -> AtlasDerivation:
        """``order="weight"`` (bounded country reads, TRUNC-1): rows are read latest year first and largest value
        first, so a row cap cuts the oldest and smallest rows, never the largest partners of the latest year;
        ``skipped["row_cap_reached"]`` says the read was capped."""
        d = AtlasDerivation(lt.id)
        if not self.available(lt):
            d.skipped["source_not_in_snapshot"] += 1
            return d
        rows = self._rows(self._dyadic_sql(lt, where, None if stored_in_full(lt) else top_k, cap, order), ())
        if len(rows) > cap:
            d.skipped["row_cap_reached"] += 1
            rows = rows[:cap]
        for k, n in self._dyadic_counts(lt, where).items():
            if k == "cells":
                d.considered = n
            else:
                d.skipped[k] += n
        for r in rows:
            if r.get("hop"):
                d.mapped_via_hop[str(r["hop"]).split(":", 1)[1]] += 1
        if not lt.directed:
            self._merge_undirected(lt, rows, d)
            return d
        if any(r.get("hop") for r in rows):  # a hop must never add a second value to a reported pair
            seen: Counter[tuple[Any, ...]] = Counter((r["subj"], r["obj"], r["year"], r["facet"]) for r in rows)
            dup = {k for k, n in seen.items() if n > 1}
            if dup:
                d.skipped["mapping_hop_collides_with_direct_row"] += sum(seen[k] for k in dup)
                rows = [r for r in rows if (r["subj"], r["obj"], r["year"], r["facet"]) not in dup]
        for r in rows:
            try:
                d.links.append(self._row_link(lt, r))
            except LinkError:
                d.skipped["malformed_row"] += 1
        return d

    # ------------------------------------------------------------------ votes
    def _votes_cte(self, nodes: Sequence[str] | None) -> str:
        filt = ""
        if nodes is not None:
            filt = (
                " AND coalesce(c.iso3, c.entity_qid, c.provider_geo) IN (" + ", ".join(_sql_str(n) for n in nodes) + ")"
            )
        return f"""
        v AS (SELECT CAST(substr(s.indicator, 11) AS INTEGER) AS rcid,
                     coalesce(c.iso3, c.entity_qid, c.provider_geo) AS node, c.iso3,
                     CASE c.value_text WHEN 'yes' THEN 1 WHEN 'no' THEN 2 ELSE 3 END AS pos,
                     CAST(year(c.period_start) AS INTEGER) AS y, c.raw_sha256, c.table_sha
                FROM cells c JOIN series s USING (sid)
               WHERE s.source = 'unga' AND s.dataset = 'votes' AND starts_with(s.indicator, 'vote|rcid=')
                 AND c.value_text IN ('yes', 'no', 'abstain'){filt}),
        rc AS (SELECT CAST(substr(s.indicator, 15) AS INTEGER) AS rcid,
                      from_json(json_extract(c.flags, '$.issue_codes'), '["VARCHAR"]') AS issues
                 FROM cells c JOIN series s USING (sid)
                WHERE s.source = 'unga' AND s.dataset = 'rollcalls' AND starts_with(s.indicator, 'rollcall|rcid=')),
        rci AS (SELECT rcid, unnest(issues) AS issue FROM rc
                UNION ALL SELECT rcid, 'all' AS issue FROM rc)"""

    def _vote_groups(
        self,
        *,
        grain: str,
        pair: Sequence[str] | None,
        node: str | None,
        issues: Sequence[str] | None,
        year_lo: int | None,
        year_hi: int | None,
    ) -> str:
        """CTEs up to ``g``: per (na < nb, period, issue) counts of common and identical positions."""
        per = "CAST(floor(a.y / 10.0) * 10 AS INTEGER)" if grain == "decade" else "a.y"
        if pair is not None:
            nodes: Sequence[str] | None = sorted(pair)
            join = "a.node < b.node"
        elif node is not None:
            nodes = None
            join = f"a.node = {_sql_str(node)} AND b.node <> a.node"
        else:
            nodes, join = None, "a.node < b.node"
        cond = ""
        if issues:
            cond += " AND i.issue IN (" + ", ".join(_sql_str(x) for x in issues) + ")"
        if year_lo is not None:
            cond += f" AND a.y >= {int(year_lo)}"
        if year_hi is not None:
            cond += f" AND a.y <= {int(year_hi)}"
        return f"""{self._votes_cte(nodes).strip()},
        p AS (SELECT a.rcid, a.y, a.raw_sha256, a.table_sha, a.pos AS pa, b.pos AS pb,
                     least(a.node, b.node) AS na, greatest(a.node, b.node) AS nb,
                     CASE WHEN a.node < b.node THEN a.iso3 ELSE b.iso3 END AS iso3_a,
                     CASE WHEN a.node < b.node THEN b.iso3 ELSE a.iso3 END AS iso3_b
                FROM v a JOIN v b ON a.rcid = b.rcid AND {join}),
        g AS MATERIALIZED (SELECT na, nb, any_value(iso3_a) AS iso3_a, any_value(iso3_b) AS iso3_b,
                     {per.replace("a.y", "y")} AS period, i.issue, count(*) AS n_common,
                     sum(CASE WHEN pa = pb THEN 1 ELSE 0 END) AS n_identical,
                     sum(CASE WHEN pa = 1 AND pb = 1 THEN 1 ELSE 0 END) AS both_yes,
                     sum(CASE WHEN pa = 2 AND pb = 2 THEN 1 ELSE 0 END) AS both_no,
                     sum(CASE WHEN pa = 3 AND pb = 3 THEN 1 ELSE 0 END) AS both_abstain,
                     min(y) AS first_year, max(y) AS last_year,
                     any_value(raw_sha256) AS raw_sha256, any_value(table_sha) AS table_sha,
                     count(DISTINCT raw_sha256) AS n_raw
                FROM p JOIN rci i ON i.rcid = p.rcid
               WHERE TRUE{cond.replace("a.y", "y")}
               GROUP BY na, nb, period, i.issue)"""

    def rollcall_span(self) -> tuple[int, int] | None:
        """(first, last) calendar year of the roll calls in the snapshot (the data's real coverage, VOTE-2)."""
        if self._vote_span is None:
            r = self._rows(
                """SELECT min(CAST(year(c.period_start) AS INTEGER)) AS lo, max(CAST(year(c.period_start) AS INTEGER)) AS hi
                     FROM cells c JOIN series s USING (sid)
                    WHERE s.source = 'unga' AND s.dataset = 'rollcalls' AND starts_with(s.indicator, 'rollcall|rcid=')""",
                (),
            )
            if not r or r[0]["lo"] is None:
                return None
            self._vote_span = (int(r[0]["lo"]), int(r[0]["hi"]))
        return self._vote_span

    def _vote_node(self, node: str, iso3: str | None) -> str:
        if iso3:
            return self.node_of(iso3)
        return node

    def _vote_link(
        self, r: Mapping[str, Any], grain: str, year_lo: int | None = None, year_hi: int | None = None
    ) -> Link:
        decade_lo = int(r["period"])
        decade_hi = decade_lo + 9 if grain == "decade" else decade_lo
        # VOTE-1/VOTE-2: the years the value was computed on = the decade cut by the requested window and by the
        # data's real coverage (roll calls 1946-2024): the label, the temporal and the cited selector all say them
        span = self.rollcall_span() or (decade_lo, decade_hi)
        lo = max(decade_lo, year_lo if year_lo is not None else decade_lo, span[0])
        hi = min(decade_hi, year_hi if year_hi is not None else decade_hi, span[1])
        if hi < lo:  # cannot happen for a group with votes (its years are inside both); keep the decade
            lo, hi = decade_lo, decade_hi
        n, k = int(r["n_common"]), int(r["n_identical"])
        share = (Decimal(k) / Decimal(n)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
        selector = json.dumps(
            {
                "computed_from": ["unga.votes", "unga.rollcalls"],
                "method": VOTE_METHOD,
                "pair": [r["na"], r["nb"]],
                "years": [lo, hi],
                "issue": r["issue"],
                "positions": list(VOTE_POSITIONS),
                "min_common": MIN_COMMON_ROLLCALLS,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        ev = Evidence(
            kind="atlas_row",
            source="unga",
            raw_sha256=r["raw_sha256"],
            selector=selector,
            subject=r["na"],
            object=r["nb"],
            pid=f"vote_agreement|issue={r['issue']}|grain={grain}",
            origin=self.origin,
        )
        a = self._vote_node(r["na"], r["iso3_a"])
        b = self._vote_node(r["nb"], r["iso3_b"])
        if b < a:
            a, b = b, a
        from reva.links.model import PartialDate

        basis = {
            "method": "derived",
            "method_version": VOTE_METHOD,
            "issue": r["issue"],
            "grain": grain,
            "n_common": n,
            "n_identical": k,
            "both_yes": int(r["both_yes"]),
            "both_no": int(r["both_no"]),
            "both_abstain": int(r["both_abstain"]),
            "roll_call_years": [int(r["first_year"]), int(r["last_year"])],
            "voters": {r["na"]: r["iso3_a"], r["nb"]: r["iso3_b"]},
            "table_artifact_sha256": r["table_sha"],
        }
        if grain == "decade":
            basis["decade"] = [decade_lo, decade_lo + 9]
            basis["years_used"] = [lo, hi]
            if (lo, hi) != (decade_lo, decade_hi):
                basis["partial_period"] = True
                basis["partial_reason"] = (
                    "requested year window"
                    if (year_lo is not None and year_lo > max(decade_lo, span[0]))
                    or (year_hi is not None and year_hi < min(decade_hi, span[1]))
                    else "roll-call data coverage"
                )
        return Link(
            a,
            b,
            "unga_vote_agreement",
            False,
            Temporal("interval", PartialDate(lo), PartialDate(hi))
            if grain == "decade"
            else Temporal("snapshot", year=lo),
            (ev,),
            weight=format(share, "f"),
            unit=VOTE_UNIT,
            basis=basis,
        )

    def votes(
        self,
        *,
        grain: str = "decade",
        pair: tuple[str, str] | None = None,
        node: str | None = None,
        top_k: int | None = None,
        issues: Sequence[str] | None = None,
        year_lo: int | None = None,
        year_hi: int | None = None,
        cap: int = 2_000_000,
        order: str = "pair",
    ) -> AtlasDerivation:
        """Vote-agreement links: every pair (``top_k`` per node, period and issue), one ``pair`` of voter keys
        (ISO3 / QID / provider code) or every partner of one ``node``. ``order="weight"``: latest period and
        highest share first (a row cap then cuts the oldest, lowest rows; TRUNC-1)."""
        d = AtlasDerivation("unga_vote_agreement")
        lt = self.taxonomy.link("unga_vote_agreement")
        if not self.available(lt):
            d.skipped["source_not_in_snapshot"] += 1
            return d
        if grain not in ("decade", "year"):
            raise ValueError("grain must be decade or year")
        kw: dict[str, Any] = dict(grain=grain, pair=pair, node=node, issues=issues, year_lo=year_lo, year_hi=year_hi)
        rank_sel, rank_where = "", ""
        if top_k is not None:
            rank_sel = """, row_number() OVER (PARTITION BY na, period, issue ORDER BY share DESC, n_common DESC, nb) AS rk_a
                          , row_number() OVER (PARTITION BY nb, period, issue ORDER BY share DESC, n_common DESC, na) AS rk_b"""
            rank_where = f"WHERE rk_a <= {int(top_k)} OR rk_b <= {int(top_k)}"
        sql = f"""WITH {self._vote_groups(**kw).strip()},
            h AS (SELECT *, n_identical / n_common AS share {rank_sel} FROM g WHERE n_common >= {MIN_COMMON_ROLLCALLS})
            SELECT *, (SELECT count(*) FROM g WHERE n_common < {MIN_COMMON_ROLLCALLS}) AS too_few
              FROM h {rank_where}
             ORDER BY {"period DESC, share DESC, n_common DESC, na, nb, issue" if order == "weight" else "na, nb, period, issue"}
             LIMIT {int(cap) + 1}"""
        rows = self._rows(sql, ())
        if len(rows) > cap:
            d.skipped["row_cap_reached"] += 1
            rows = rows[:cap]
        d.considered = len(rows)
        if rows:
            too_few = int(rows[0]["too_few"])
        else:  # nothing passed the minimum: count what was below it
            cnt = self._rows(
                f"WITH {self._vote_groups(**kw).strip()} "
                f"SELECT count(*) AS k FROM g WHERE n_common < {MIN_COMMON_ROLLCALLS}",
                (),
            )
            too_few = int(cnt[0]["k"]) if cnt else 0
        if too_few:
            d.skipped["fewer_than_min_common_rollcalls"] += too_few
        for r in rows:
            if int(r["n_raw"]) != 1:
                d.skipped["several_raw_artifacts"] += 1
                continue
            d.links.append(self._vote_link(r, grain, year_lo, year_hi))
        return d

    def vote_detail(
        self, a: str, b: str, *, year_lo: int, year_hi: int, issue: str = "all", limit: int = 200
    ) -> dict[str, Any]:
        """Per roll call positions of two voters (the evidence behind one vote-agreement value)."""
        iss = "" if issue == "all" else f" AND list_contains(rc.issues, {_sql_str(issue)})"
        rows = self._rows(
            f"""WITH {self._votes_cte(sorted((a, b))).strip()},
                d AS (SELECT CAST(substr(s.indicator, 15) AS INTEGER) AS rcid, c.value_text AS descr,
                             json_extract_string(c.flags, '$.unres') AS unres, c.period_start AS day,
                             {iso_date_sql("c.period_start")} AS date,
                             c.selector AS rc_selector
                        FROM cells c JOIN series s USING (sid)
                       WHERE s.source = 'unga' AND s.dataset = 'rollcalls')
                SELECT x.rcid, d.date, d.unres, d.descr, d.rc_selector, x.pos AS pos_a, y.pos AS pos_b
                  FROM v x JOIN v y ON x.rcid = y.rcid AND x.node = {_sql_str(a)} AND y.node = {_sql_str(b)}
                  JOIN rc ON rc.rcid = x.rcid JOIN d ON d.rcid = x.rcid
                 WHERE x.y BETWEEN {int(year_lo)} AND {int(year_hi)}{iss}
                 ORDER BY d.day, x.rcid
                 LIMIT {int(limit) + 1}""",
            (),
        )
        names = {1: "yes", 2: "no", 3: "abstain"}
        out = [
            {
                "rcid": int(r["rcid"]),
                "date": r["date"],  # astronomical ISO text (BCE-safe)
                "resolution": r["unres"],
                "description": (r["descr"] or "")[:300],
                a: names[int(r["pos_a"])],
                b: names[int(r["pos_b"])],
                "identical": r["pos_a"] == r["pos_b"],
                "rollcall_selector": json.loads(r["rc_selector"]) if r["rc_selector"] else None,
            }
            for r in rows[:limit]
        ]
        return {"rollcalls": out, "truncated": len(rows) > limit}
